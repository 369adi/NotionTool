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
REGREET_AFTER_S = 240
REGREET_MAX = 2
REGREET_GAP_S = 4
RETIRED_STATES = ('missing', 'retired')


def usable_count(workers, account_id):
    """Joined, enabled Welcome Coworkers of one account that are not missing or retired."""
    return sum(1 for w in workers if w.get('account_id') == account_id
               and w.get('purpose', 'worker') == 'worker' and w.get('join_state') == 'joined'
               and w.get('enabled') and w.get('state') not in RETIRED_STATES and not w.get('retired'))


def has_main(workers, account_id):
    """The account has a Main (or an operator Main) that is not missing."""
    return any(w.get('account_id') == account_id and w.get('purpose') in ('main', 'operator')
               and w.get('state') not in RETIRED_STATES for w in workers)


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
            main_ok = has_main(workers, ident)
            reason = ''
            with self.lock:
                if (usable >= target and main_ok) or ident in self.growing:
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
            title = ('Grow Welcome Coworkers to %d' % target) if main_ok else ('Create the Main and %d Welcome Coworkers' % target)
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
        try:
            self.regreet(now)
        except Exception as exc:  # never breaks the growth check
            self.store.event('welcome_resend_failed', message=str(exc)[:200])
        return started

    def pending_count(self, ident):
        """Managed Welcome Coworkers that exist but have not joined yet (shown as 'being prepared')."""
        return sum(1 for w in self.store.all('workers') if w.get('account_id') == ident
                   and w.get('purpose', 'worker') == 'worker' and w.get('join_state') == 'pending'
                   and w.get('pool_admitted') and w.get('state') not in RETIRED_STATES)

    def regreet_due(self, account, workers, now):
        """Thread ids of managed Welcome Coworkers that are listed but still not joined."""
        ledger = (account or {}).get('team_threads') or {}
        ids = {item.get('id') for role, item in ledger.items()
               if role != 'main' and isinstance(item, dict) and not item.get('pending')}
        return [w['thread_id'] for w in workers if w.get('account_id') == account.get('id')
                and w.get('thread_id') in ids and w.get('purpose', 'worker') == 'worker'
                and w.get('join_state') == 'pending' and w.get('state') not in RETIRED_STATES
                and not w.get('lease') and int(w.get('regreets') or 0) < REGREET_MAX
                and now >= float(w.get('regreet_at') or 0)
                and now - float(w.get('welcome_at') or 0) >= REGREET_AFTER_S]

    def regreet(self, now=None):
        """Start one job per account that resends the greeting to Coworkers stuck in 'pending'."""
        now = self.clock() if now is None else now
        if not callable(getattr(self.adapter, 'regreet_welcome', None)) or self.budget_reason():
            return []
        workers = self.store.all('workers')
        started = []
        for account in self.store.all('accounts'):
            ident = account.get('id')
            if account.get('state') != 'ready' or not ident:
                continue
            due = self.regreet_due(account, workers, now)
            with self.lock:
                if not due or ident in self.growing:
                    continue
                self.growing.add(ident)
            try:
                self.start_job('Resend the Welcome greeting to %d Coworkers' % len(due),
                               lambda ident=ident, due=due: self._regreet(ident, due), ident)
            except ValueError:
                with self.lock:
                    self.growing.discard(ident)
                continue
            started.append(ident)
        return started

    def _retire_pending(self, ident, tid):
        """Retire every pending worker whose thread_id is tid (chat was deleted or gone)."""
        for w in self.store.all('workers'):
            if w.get('account_id') != ident or w.get('thread_id') != tid:
                continue
            if w.get('join_state') != 'pending' or w.get('state') in RETIRED_STATES:
                continue
            w.update(state='retired', enabled=False, lease='', retired_reason='regreet: chat not in Notion chat list')
            self.store.put('workers', w)
            self.store.event('coworker_retired', worker_id=w['id'], thread_id=tid,
                             message='Pending Coworker chat gone from Notion (regreet failed); retired')

    def _regreet(self, ident, due):
        sent = 0
        try:
            for index, tid in enumerate(due):
                if index:
                    time.sleep(REGREET_GAP_S)
                try:
                    sent += bool(self.adapter.regreet_welcome(ident, tid))
                except Exception as exc:
                    msg = str(exc)
                    self.store.event('welcome_resend_failed', message=(tid + ': ' + msg)[:200], account_id=ident)
                    if 'not in the chat list' in msg or 'chat list' in msg.lower():
                        self._retire_pending(ident, tid)
                        continue
                self._restore_name(ident, tid)
            try:
                self.adapter.refresh(ident)  # picks up the joins
            except Exception:
                pass
        finally:
            with self.lock:
                self.growing.discard(ident)
        return dict(resent=sent, tried=len(due))

    def _restore_name(self, ident, tid):
        """The resent greeting sets the title 'Welcome to Notion' again; give the Coworker its name back (I41)."""
        worker = next((w for w in self.store.all('workers') if w.get('account_id') == ident
                       and w.get('thread_id') == tid), None) or {}
        name = worker.get('harness_title')
        rename = getattr(self.adapter, 'rename_thread', None)
        client_fn = getattr(self.adapter, 'client', None)
        if not name or not callable(rename) or not callable(client_fn):
            return False
        try:
            rename(client_fn(ident), tid, name)
        except Exception as exc:
            self.store.event('coworker_rename_failed', message=(name + ' ' + tid + ': ' + str(exc))[:200], account_id=ident)
            return False
        self.store.event('coworker_name_restored', message=name + ' ' + tid, account_id=ident)
        return True

    def _grow(self, ident, target, usable):
        try:
            # At least one Main always: ensure_team creates it only when the account has no usable Main.
            result = self.adapter.ensure_team(ident, create_main=True)
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
