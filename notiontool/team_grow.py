"""Automatic Coworker team growth.

The harness checks by itself (on startup, then every CHECK_S seconds from the engine loop) whether a
ready account has fewer usable Welcome Coworkers than team_size(settings). Usable = purpose 'worker',
joined, enabled, not missing/retired. When one is short, it starts ONE ensure_team job for that account.

Safety (I5, never resend):
- never while the account ledger has a pending (unconfirmed) creation;
- never while a context rotation (23h / threshold, table 'rotations') is open for one of its workers,
  because the rotation creates its own Welcome chat for that worker;
- never while the shared network budget is paused or in a cooldown;
- at most one job per account (own set plus Application.job's per-account lock);
- after a failure the account waits BACKOFF_S seconds.
Events: team_grow_started, team_grow_done, team_grow_failed. public() feeds state.team.
"""
import threading
import time

from .team_size import team_size

CHECK_S = 300
BACKOFF_S = 900
RETIRED_STATES = ('missing', 'retired')


def usable_count(workers, account_id):
    """Joined, enabled Welcome Coworkers of one account that are not missing or retired."""
    return sum(1 for w in workers if w.get('account_id') == account_id
               and w.get('purpose', 'worker') == 'worker' and w.get('join_state') == 'joined'
               and w.get('enabled') and w.get('state') not in RETIRED_STATES and not w.get('retired'))


def pending_roles(account):
    """Ledger roles whose Welcome creation is still unconfirmed."""
    ledger = (account or {}).get('team_threads') or {}
    return sorted(role for role, item in ledger.items() if isinstance(item, dict) and item.get('pending'))


class TeamGrower:
    def __init__(self, store, adapter, start_job, clock=time.time):
        # start_job(title, fn, account_id) runs fn in the background; raises ValueError when the account is busy.
        self.store = store
        self.adapter = adapter
        self.start_job = start_job
        self.clock = clock
        self.lock = threading.Lock()
        self.next_check = None  # None: the first engine tick checks at once (startup)
        self.growing = set()
        self.failed_until = {}
        self.last_error = {}
        self.blocked = {}

    def maybe_check(self):
        """Called from the engine loop; runs check() at startup and then every CHECK_S seconds."""
        now = self.clock()
        if self.next_check is not None and now < self.next_check:
            return []
        self.next_check = now + CHECK_S
        return self.check()

    def budget_reason(self):
        budget = getattr(self.adapter, 'network_budget', None)
        status = getattr(budget, 'status', None)
        if not callable(status):
            return ''
        try:
            info = status() or {}
        except Exception:
            return 'network budget unavailable'
        if info.get('paused'):
            return 'remote calls paused'
        if (info.get('cooldown_seconds') or 0) > 0:
            return 'network cooldown'
        return ''

    def rotating(self, account_id, workers):
        mine = {w['id'] for w in workers if w.get('account_id') == account_id}
        return any(r.get('id') in mine for r in self.store.all('rotations'))

    def check(self):
        """Start ensure_team for every ready account that is short. Returns the started account ids."""
        now = self.clock()
        target = team_size(self.store.settings())
        workers = self.store.all('workers')
        started = []
        for account in self.store.all('accounts'):
            ident = account.get('id')
            if account.get('state') != 'ready' or not ident:
                continue
            usable = usable_count(workers, ident)
            reason = ''
            with self.lock:
                if usable >= target or ident in self.growing:
                    self.blocked.pop(ident, None)
                    continue
                if now < self.failed_until.get(ident, 0):
                    reason = 'waiting after a failure'
                elif pending_roles(account):
                    reason = 'a previous creation is unconfirmed'
                elif self.rotating(ident, workers):
                    reason = 'a chat rotation is in progress'
                else:
                    reason = self.budget_reason()
                if not reason:
                    self.growing.add(ident)
                self.blocked[ident] = reason
            if reason:
                continue
            title = 'Grow Welcome Coworkers to %d' % target
            try:
                self.start_job(title, lambda ident=ident, usable=usable: self._grow(ident, target, usable), ident)
            except ValueError:
                with self.lock:  # another operation runs for this account; try again at the next check
                    self.growing.discard(ident)
                    self.blocked[ident] = 'another account operation is running'
                continue
            self.store.event('team_grow_started', message='Creating missing Welcome Coworkers (%d of %d usable)' % (usable, target),
                             account_id=ident, target=target, usable=usable)
            started.append(ident)
        return started

    def _grow(self, ident, target, usable):
        try:
            result = self.adapter.ensure_team(ident)
        except Exception as exc:
            error = (type(exc).__name__ + ': ' + str(exc))[:300]
            with self.lock:
                self.failed_until[ident] = self.clock() + BACKOFF_S
                self.last_error[ident] = error
                self.growing.discard(ident)
            self.store.event('team_grow_failed', message='Team growth failed; next try in 15 minutes: ' + error[:200],
                             account_id=ident, target=target, usable=usable)
            raise
        with self.lock:
            self.last_error.pop(ident, None)
            self.failed_until.pop(ident, None)
            self.growing.discard(ident)
        self.store.event('team_grow_done', message='Welcome Coworkers ensured (team size %d)' % target,
                         account_id=ident, target=target, usable=usable_count(self.store.all('workers'), ident))
        return result

    def public(self, account_id=''):
        """state.team: {account, target, usable, growing, last_error} for one account (default: first ready one)."""
        accounts = [a for a in self.store.all('accounts') if a.get('state') == 'ready']
        if account_id:
            accounts = [a for a in accounts if a.get('id') == account_id] or accounts
        if not accounts:
            return None
        ident = accounts[0]['id']
        with self.lock:
            return dict(account=ident, target=team_size(self.store.settings()),
                        usable=usable_count(self.store.all('workers'), ident), growing=ident in self.growing,
                        last_error=self.last_error.get(ident, ''), waiting=self.blocked.get(ident, ''),
                        retry_at=self.failed_until.get(ident, 0))
