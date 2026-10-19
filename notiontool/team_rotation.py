"""Automatic age rotation of Welcome Coworker chats (default every 23 hours).

Every managed Coworker records created_at (UTC epoch seconds). Missing values are backfilled from the
Notion thread record (created_time) or, without one, from the first time the harness saw the chat.

When a managed Coworker (ledger role workerK) is older than coworker_max_age_hours (default 23, 1..72)
and idle, ONE background job for its account creates a fresh Welcome chat with the same flow as
ensure_team (pending ledger entry first, create_welcome, confirmation from Notion's chat list), points
the role at the new thread and retires the old worker (enabled False, purpose 'retired_worker'). The
old thread is kept; nothing is deleted in Notion. Busy chats are checked again at every pass and rotate
as soon as they are idle.

Safety (I5, never resend):
- an unconfirmed creation stays pending in the ledger and is only reconciled (chat list read), never
  created again; any other pending ledger entry blocks the account;
- never while a context rotation ('rotations') or a handoff is open for the worker;
- never while the shared network budget is paused or cooling down;
- at most one job per account (own set plus the Application.job per-account lock);
- after a failure the account waits BACKOFF_S seconds.
Events: team_rotation_started, team_rotation_done, team_rotation_failed.
"""
import datetime
import threading
import time
import uuid

from .team_size import team_roles

CHECK_S = 300
BACKOFF_S = 900
DEFAULT_MAX_AGE_HOURS = 23
MIN_AGE_HOURS, MAX_AGE_HOURS = 1, 72
RETIRED_PURPOSE = 'retired_worker'
TERMINAL_TASK_STATES = ('done', 'failed', 'blocked', 'cancelled')


def max_age_hours(settings):
    try:
        value = int((settings or {}).get('coworker_max_age_hours', DEFAULT_MAX_AGE_HOURS))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_AGE_HOURS
    return max(MIN_AGE_HOURS, min(MAX_AGE_HOURS, value))


def epoch(value):
    """A Notion time (ms or s number, or ISO text) as UTC epoch seconds; 0.0 when unknown."""
    if isinstance(value, bool) or value in (None, ''):
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 1e11 else number
    try:
        stamp = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


def age_seconds(worker, now):
    """Seconds since the chat was created, or None while created_at is unknown."""
    created = epoch((worker or {}).get('created_at'))
    return max(0.0, now - created) if created else None


def busy_reason(worker, tasks, rotations):
    """'' when the worker is idle and may rotate, else why it has to wait."""
    if worker.get('lease'):
        return 'holds a lease'
    if worker.get('state') != 'ready':
        return 'state ' + str(worker.get('state') or 'unknown')
    if worker.get('join_state') != 'joined':
        return 'not joined yet'
    if any(t.get('worker_id') == worker['id'] and (t.get('state') not in TERMINAL_TASK_STATES or t.get('remote_unknown'))
           for t in tasks):
        return 'task assigned'
    if any(r.get('id') == worker['id'] for r in rotations):
        return 'context checkpoint open'
    if worker.get('handoff') and not worker.get('handoff_delivered'):
        return 'handoff pending'
    return ''


def pending_roles(account):
    ledger = (account or {}).get('team_threads') or {}
    return sorted(role for role, item in ledger.items() if isinstance(item, dict) and item.get('pending'))


class TeamRotator:
    def __init__(self, store, adapter, start_job, clock=time.time):
        # start_job(title, fn, account_id) runs fn in the background; raises ValueError when the account is busy.
        self.store = store
        self.adapter = adapter
        self.start_job = start_job
        self.clock = clock
        self.lock = threading.Lock()
        self.next_check = None
        self.rotating = set()
        self.failed_until = {}
        self.last_error = {}
        self.waiting = {}

    def maybe_check(self):
        """Called from the engine loop: at startup, then at most every CHECK_S seconds. Never raises."""
        now = self.clock()
        if self.next_check is not None and now < self.next_check:
            return []
        self.next_check = now + CHECK_S
        try:
            return self.check()
        except Exception as exc:
            self.store.event('team_rotation_failed', message='Rotation check failed: ' + type(exc).__name__)
            return []

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

    # created_at -------------------------------------------------------------------------------

    def backfill(self, account_id, now):
        """Stamp created_at on this account's Coworkers: thread record created_time, else first seen."""
        workers = [w for w in self.store.all('workers') if w.get('account_id') == account_id
                   and w.get('purpose', 'worker') == 'worker' and not epoch(w.get('created_at'))]
        if not workers:
            return 0
        for worker in workers:
            if not epoch(worker.get('first_seen_at')):
                worker['first_seen_at'] = now
        records = None
        if not self.budget_reason():
            try:
                client = self.adapter.client(account_id)
                records = self.adapter.thread_records(client, [w['thread_id'] for w in workers])
            except Exception:
                records = None  # unreadable now: keep first_seen_at and try again at the next check
        for worker in workers:
            if records is not None:
                record = records.get(worker['thread_id']) or {}
                created = epoch(record.get('created_time') or record.get('createdTime') or record.get('created_at'))
                if created:
                    worker.update(created_at=created, created_at_source='notion')
                else:
                    worker.update(created_at=worker['first_seen_at'], created_at_source='first_seen')
            self.store.put('workers', worker)
        return len(workers)

    # selection --------------------------------------------------------------------------------

    def candidate(self, account, now):
        """(role, worker, waiting) for the oldest due idle Coworker; waiting maps worker id -> reason."""
        settings = self.store.settings()
        limit = max_age_hours(settings) * 3600
        ledger = account.get('team_threads') or {}
        by_thread = {w['thread_id']: w for w in self.store.all('workers') if w.get('account_id') == account['id']}
        tasks = self.store.all('tasks')
        rotations = self.store.all('rotations')
        due, waiting = [], {}
        for role in team_roles(settings):
            item = ledger.get(role)
            worker = by_thread.get((item or {}).get('id')) if isinstance(item, dict) else None
            if not worker or worker.get('purpose', 'worker') != 'worker' or worker.get('state') == 'missing':
                continue
            age = age_seconds(worker, now)
            if age is None or age < limit:
                continue
            reason = busy_reason(worker, tasks, rotations)
            if reason:
                waiting[worker['id']] = reason
                continue
            due.append((-age, role, worker))
        if not due:
            return None, None, waiting
        _, role, worker = min(due, key=lambda entry: (entry[0], entry[1]))
        return role, worker, waiting

    def check(self):
        """Start at most one rotation job per ready account. Returns the started account ids."""
        now = self.clock()
        started = []
        for account in self.store.all('accounts'):
            ident = account.get('id')
            if account.get('state') != 'ready' or not ident:
                continue
            with self.lock:
                if ident in self.rotating or now < self.failed_until.get(ident, 0):
                    continue
            self.backfill(ident, now)
            account = self.store.get('accounts', ident) or account
            pending = pending_roles(account)
            ledger = account.get('team_threads') or {}
            resume = next((r for r in pending if (ledger.get(r) or {}).get('rotates')), None)
            if pending and not resume:
                continue  # someone else's creation is unconfirmed: never create next to it (I5)
            if self.budget_reason():
                continue
            if resume:
                role, worker_id = resume, ledger[resume]['rotates']
                worker = self.store.get('workers', worker_id) or {}
                waiting = {}
            else:
                role, worker, waiting = self.candidate(account, now)
                worker_id = (worker or {}).get('id')
            with self.lock:
                self.waiting[ident] = waiting
                if not role or not worker_id:
                    continue
                self.rotating.add(ident)
            title = 'Rotate %s Welcome chat (older than %d h)' % (role, max_age_hours(self.store.settings()))
            try:
                self.start_job(title, lambda i=ident, r=role, w=worker_id: self._rotate(i, r, w), ident)
            except ValueError:
                with self.lock:
                    self.rotating.discard(ident)
                continue
            self.store.event('team_rotation_started', message='Rotating ' + role + ': ' + str(worker.get('label') or worker_id),
                             account_id=ident, role=role, worker_id=worker_id,
                             age_hours=round((age_seconds(worker, now) or 0) / 3600, 1))
            started.append(ident)
        return started

    # rotation job -----------------------------------------------------------------------------

    def _rotate(self, ident, role, worker_id):
        try:
            result = self._rotate_once(ident, role, worker_id)
        except Exception as exc:
            error = (type(exc).__name__ + ': ' + str(exc))[:300]
            with self.lock:
                self.failed_until[ident] = self.clock() + BACKOFF_S
                self.last_error[ident] = error
            self.store.event('team_rotation_failed', message=role + ' rotation failed; next check in 15 minutes: ' + error[:200],
                             account_id=ident, role=role, worker_id=worker_id)
            raise
        finally:
            with self.lock:
                self.rotating.discard(ident)
        with self.lock:
            self.last_error.pop(ident, None)
        if result.get('postponed'):
            return result
        self.store.event('team_rotation_done', message=role + ' moved to a fresh Welcome chat; the old chat is retired and kept',
                         account_id=ident, role=role, worker_id=worker_id, thread_id=result.get('thread_id', ''))
        return result

    def _set_enabled(self, worker_id, enabled):
        worker = self.store.get('workers', worker_id)
        if worker:
            worker['enabled'] = enabled
            worker.pop('rotation_reserved', None) if enabled else worker.update(rotation_reserved=True)
            self.store.put('workers', worker)
        return worker

    def _rotate_once(self, ident, role, worker_id):
        adapter = self.adapter
        account = self.store.get('accounts', ident)
        ledger = account.get('team_threads') or {}
        item = dict(ledger.get(role) or {})
        old = self.store.get('workers', worker_id)
        if not old:
            raise ValueError('The worker to rotate is gone')
        reason = busy_reason(old, self.store.all('tasks'), self.store.all('rotations'))
        if reason and not (item.get('pending') and old.get('rotation_reserved')):
            return {'postponed': reason}
        # Reserve the old chat so the engine assigns nothing new while the replacement is created.
        old = self._set_enabled(worker_id, False)
        if old.get('lease') or old.get('state') != 'ready':
            self._set_enabled(worker_id, True)
            return {'postponed': 'became busy'}
        client = adapter.client(ident)
        if item.get('pending'):
            if item.get('rotates') != worker_id:
                raise ValueError('Another Welcome creation is unconfirmed for ' + role)
            tid = item['id']
            listed = any(adapter.thread_key(t) == str(uuid.UUID(tid)) for t in client.threads())
            if not listed:
                self._set_enabled(worker_id, True)
                raise ValueError('The previous replacement chat is still unconfirmed; it is never created again')
        else:
            if item.get('id') != old['thread_id']:
                self._set_enabled(worker_id, True)
                raise ValueError(role + ' no longer points at this chat')
            model = client.model()
            tid = adapter.new_id(client.info['space_id'], '00a9')
            ledger[role] = {'id': tid, 'pending': True, 'rotates': worker_id, 'previous': old['thread_id']}
            account['team_threads'] = ledger
            self.store.put('accounts', account)
            listed, clean = adapter.create_welcome(client, tid, model)
            if not listed:
                self._set_enabled(worker_id, True)  # the old chat keeps working; the ledger stays pending (I5)
                raise ValueError('Welcome creation is unconfirmed; refresh to reconcile before creating again')
            if not clean:
                self.store.event('welcome_warning', message=role + ' replacement exists, but its greeting returned an upstream error',
                                 slot=account.get('slot'))
        now = self.clock()
        new_key = str(uuid.UUID(tid))
        account = self.store.get('accounts', ident)
        ledger = account.get('team_threads') or {}
        ledger[role] = {'id': tid, 'pending': False}
        account['team_threads'] = ledger
        self.store.put('accounts', account)
        old = self.store.get('workers', worker_id)
        old.pop('rotation_reserved', None)
        old.update(enabled=False, purpose=RETIRED_PURPOSE, retired=True, retired_at=now, retired_reason='age',
                   retired_role=role, successor=new_key)
        self.store.put('workers', old)
        adapter.refresh(ident)
        for worker in self.store.all('workers'):
            if worker.get('account_id') == ident and worker.get('thread_id') == new_key:
                worker.update(join_state='pending', enabled=False, welcome=True, purpose='worker', pool_admitted=True,
                              created_at=now, created_at_source='created')
                self.store.put('workers', worker)
        adapter.apply_chat_pool(ident, client)
        return {'role': role, 'thread_id': new_key, 'retired': worker_id}

    def public(self, account_id=''):
        with self.lock:
            return dict(max_age_hours=max_age_hours(self.store.settings()), rotating=account_id in self.rotating,
                        waiting=dict(self.waiting.get(account_id, {})), last_error=self.last_error.get(account_id, ''),
                        retry_at=self.failed_until.get(account_id, 0))


class LoopChecks:
    """Runs several periodic checks from the engine's single team_grower slot (grower first)."""

    def __init__(self, *checks):
        self.checks = [c for c in checks if c is not None]

    def maybe_check(self):
        started, error = [], None
        for check in self.checks:
            try:
                started.extend(check.maybe_check() or [])
            except Exception as exc:
                error = error or exc
        if error is not None:
            raise error
        return started

    def __getattr__(self, name):
        checks = self.__dict__.get('checks') or []
        if not checks:
            raise AttributeError(name)
        return getattr(checks[0], name)
