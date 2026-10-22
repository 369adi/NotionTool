"""Coworker upkeep: detect missing/deleted coworker chats, retire them and refill the pool.

Every INTERVAL seconds for the selected account: refresh the thread list (notion.py refresh marks
unlisted chats 'missing'), retire coworkers that stayed missing for MISSING_GRACE seconds (a single
listing glitch never retires a chat), give their queued tasks back to the queue and create new
Welcome coworkers until the pool reaches the target (setting coworker_target, default 10).
"""

import json

from .context_archive import prune_ids

INTERVAL = 180
PRUNE_INTERVAL = 3600  # thread_context pruning runs at most once per hour
PRUNE_BATCH = 10
PRUNE_PAUSE = 0.05
DEFAULT_KEEP_HOURS = 6
VACUUM_MB = 50  # VACUUM only after more than this many MB were deleted
VACUUM_INTERVAL = 86400
MISSING_GRACE = 300
MAX_NEW_PER_CYCLE = 2
DEFAULT_TARGET = 10
FAIL_BACKOFF = 900
GONE = ('missing', 'retired', 'disabled')
QUEUED_TASK_STATES = ('queued', 'ready', 'retry')
BUSY_TASK_STATES = ('running', 'executing', 'fixing', 'uncertain', 'recovering')


def _coworker(worker):
    return worker.get('purpose') == 'worker' and bool(worker.get('welcome') or worker.get('enabled')
                                                       or worker.get('was_coworker'))


def plan(workers, tasks, account_id, target, now):
    """Pure decision: {'mark': [ids], 'retire': [ids], 'requeue': [task ids], 'create': n, 'alive': n}."""
    mine = [w for w in workers if w.get('account_id') == account_id and w.get('purpose') == 'worker']
    busy = {t.get('worker_id') for t in tasks if t.get('state') in BUSY_TASK_STATES}
    mark, retire = [], []
    for w in mine:
        if w.get('state') != 'missing' or not _coworker(w):
            continue
        if w.get('lease') or w['id'] in busy:
            continue  # the engine recovers a running task first (engine thread-missing path)
        since = w.get('missing_since')
        if not since:
            mark.append(w['id'])
        elif now - since >= MISSING_GRACE:
            retire.append(w['id'])
    requeue = [t['id'] for t in tasks if t.get('worker_id') in set(retire) and t.get('state') in QUEUED_TASK_STATES]
    alive = sum(1 for w in mine if _coworker(w) and w.get('state') not in GONE and w['id'] not in retire)
    create = max(0, min(MAX_NEW_PER_CYCLE, int(target) - alive))
    return dict(mark=mark, retire=retire, requeue=requeue, create=create, alive=alive)


class CoworkerUpkeep:
    def __init__(self, store, adapter, engine, clock=None):
        import time
        self.store, self.adapter, self.engine = store, adapter, engine
        self.clock = clock or time.time
        self.blocked_until = 0
        self.last_prune = None  # clock time of the last thread_context pruning pass
        self.last_vacuum = None
        self.pending_mb = 0.0  # MB deleted since the last VACUUM

    def account(self):
        from .harness_account import selected_account
        ident = selected_account(self.store)
        if isinstance(ident, dict):
            ident = ident.get('id', '')
        account = self.store.get('accounts', ident) if ident else None
        if not account:
            ready = [a for a in self.store.all('accounts') if a.get('state') == 'ready']
            account = ready[0] if len(ready) == 1 else None
        return account if account and account.get('state') == 'ready' else None

    def once(self):
        settings = self.store.settings()
        if not settings.get('coworker_upkeep', True):
            return None
        account = self.account()
        if not account:
            return None
        result = self.adapter.refresh(account['id'])
        if not (result or {}).get('threads'):
            return None  # an empty listing is a glitch, never proof that every chat is gone
        now = self.clock()
        with self.engine.lock:
            workers, tasks = self.store.all('workers'), self.store.all('tasks')
            # Chats that are listed again lose their missing mark (refresh revived them to ready).
            for w in workers:
                if w.get('missing_since') and w.get('state') != 'missing':
                    w.pop('missing_since', None)
                    self.store.put('workers', w)
            target = int(settings.get('coworker_target', DEFAULT_TARGET) or 0)
            decision = plan(workers, tasks, account['id'], target, now)
            for ident in decision['mark']:
                w = self.store.get('workers', ident)
                w.update(missing_since=now, was_coworker=True)
                self.store.put('workers', w)
            for ident in decision['retire']:
                w = self.store.get('workers', ident)
                w.update(state='retired', enabled=False, lease='', retired_reason='chat no longer in Notion')
                self.store.put('workers', w)
                self.store.event('coworker_retired', worker_id=ident, thread_id=w.get('thread_id'),
                                 message='Coworker chat is gone from Notion (deleted or missing); retired')
            for ident in decision['requeue']:
                t = self.store.get('tasks', ident)
                t['worker_id'] = ''
                self.store.put('tasks', t)
        created = 0
        if decision['create'] and now >= self.blocked_until:
            try:
                created = (self.adapter.provision(account['id'], decision['create']) or {}).get('created', 0)
                self.store.event('coworker_refill', message='Created %d new coworker chat(s); %d alive before, target %d'
                                 % (created, decision['alive'], target))
                if not created:
                    self.blocked_until = now + FAIL_BACKOFF
            except Exception as exc:
                self.blocked_until = now + FAIL_BACKOFF
                self.store.event('coworker_refill_failed', message=type(exc).__name__ + ': ' + str(exc)[:200])
        self.admit_refill_chats(account['id'])
        return dict(decision, created=created)

    def admit_refill_chats(self, account_id):
        """Refill (provision) chats are pool coworkers too: give them pool_admitted so they get a 'Coworker N' name."""
        fixed = 0
        with self.engine.lock:
            for w in self.store.all('workers'):
                if (w.get('account_id') == account_id and w.get('purpose', 'worker') == 'worker'
                        and w.get('welcome') and w.get('join_state') == 'pending' and not w.get('pool_admitted')
                        and w.get('state') not in ('retired', 'missing') and not w.get('retired')):
                    w['pool_admitted'] = True
                    self.store.put('workers', w)
                    fixed += 1
        if fixed:
            self.store.event('coworker_refill_admitted', message='%d refill chat(s) admitted to the pool for naming' % fixed)
        return fixed

    def context_rows(self):
        """Light rows (id, updated) and byte sizes of every thread_context record, without loading the documents."""
        if hasattr(self.store, 'db') and hasattr(self.store, 'lock'):
            with self.store.lock:
                rows = self.store.db.execute("SELECT id, json_extract(doc, '$.updated'), length(CAST(doc AS BLOB)) "
                                             "FROM objects WHERE kind='thread_context'").fetchall()
            return [dict(id=i, updated=u) for i, u, _ in rows], {i: n or 0 for i, _, n in rows}
        records = self.store.all('thread_context')
        return ([dict(id=r.get('id'), updated=r.get('updated')) for r in records],
                {r.get('id'): len(json.dumps(r, ensure_ascii=False).encode('utf-8')) for r in records})

    def vacuum(self, now):
        """VACUUM once the freed space is large enough, at most once per day and never while a task runs."""
        if self.pending_mb <= VACUUM_MB or not hasattr(self.store, 'db'):
            return False
        if self.last_vacuum is not None and now - self.last_vacuum < VACUUM_INTERVAL:
            return False
        with self.engine.lock:
            if any(t.get('state') in BUSY_TASK_STATES for t in self.store.all('tasks')):
                return False
            with self.store.lock:
                self.store.db.commit()
                self.store.db.execute('VACUUM')
                # In WAL mode VACUUM only writes the WAL; the checkpoint makes the file itself shrink.
                self.store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.last_vacuum, self.pending_mb = now, 0.0
        return True

    def prune_contexts(self):
        """Delete thread_context records nobody needs (context_archive.prune_ids); at most once per hour."""
        now = self.clock()
        if self.last_prune is not None and now - self.last_prune < PRUNE_INTERVAL:
            return None
        self.last_prune = now
        try:
            hours = float(self.store.settings().get('thread_context_keep_hours', DEFAULT_KEEP_HOURS))
        except (TypeError, ValueError):
            hours = DEFAULT_KEEP_HOURS
        if hours != hours or hours < 0:
            hours = DEFAULT_KEEP_HOURS
        records, sizes = self.context_rows()
        with self.engine.lock:
            workers, chats, tasks = self.store.all('workers'), self.store.all('chats'), self.store.all('tasks')
        doomed = prune_ids(records, workers, chats, tasks, now, hours * 3600)
        deleted, freed = 0, 0
        for start in range(0, len(doomed), PRUNE_BATCH):
            if start and self.engine.stop_event.wait(PRUNE_PAUSE):
                break
            for ident in doomed[start:start + PRUNE_BATCH]:
                self.store.delete('thread_context', ident)
                deleted += 1
                freed += sizes.get(ident, 0)
        mb = freed / 1048576
        self.pending_mb += mb
        try:
            vacuumed = self.vacuum(now)
        except Exception:
            vacuumed = False
        result = dict(count=deleted, mb=round(mb, 2), vacuumed=vacuumed)
        if deleted:
            self.store.event('thread_context_pruned', count=deleted, mb=result['mb'], vacuumed=vacuumed,
                             keep_hours=hours)
        return result

    def loop(self):
        last = ''
        while not self.engine.stop_event.wait(INTERVAL):
            try:
                self.once()
                last = ''
            except Exception as exc:
                if str(exc) != last:
                    last = str(exc)
                    try:
                        self.store.event('coworker_upkeep_failed', message=last[:300])
                    except Exception:
                        return
            try:
                self.prune_contexts()
            except Exception as exc:
                try:
                    self.store.event('thread_context_prune_failed', message=(type(exc).__name__ + ': ' + str(exc))[:300])
                except Exception:
                    return
