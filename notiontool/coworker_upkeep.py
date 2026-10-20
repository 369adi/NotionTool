"""Coworker upkeep: detect missing/deleted coworker chats, retire them and refill the pool.

Every INTERVAL seconds for the selected account: refresh the thread list (notion.py refresh marks
unlisted chats 'missing'), retire coworkers that stayed missing for MISSING_GRACE seconds (a single
listing glitch never retires a chat), give their queued tasks back to the queue and create new
Welcome coworkers until the pool reaches the target (setting coworker_target, default 10).
"""

INTERVAL = 180
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
        return dict(decision, created=created)

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
