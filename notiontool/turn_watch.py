"""Auto-resume of interrupted turns (Phase 4, Fix H).

A turn counts as interrupted when its chat is idle and the newest relevant entry is a tool step,
an error or an unanswered user message instead of an assistant reply - e.g. a turn that stopped
right after a tool result, as happens when a mobile client re-attaches to a running answer.
The watcher then sends one short continue message into that chat:
- only harness chats: the Main chat of the selected account and Coworkers with an active task
  (chats the user renamed, disabled or unconfirmed chats are never touched),
- once per interrupted entry, also across restarts (the handled key is stored on the worker),
- after a grace period, so the user can continue by hand first,
- at most MAX_CHAIN automatic resumes in a row,
- through the shared request budget (I11); checks run at background priority.
"""
import threading
import time

RESUME_TEXT = ('Continue: your previous turn was interrupted before it finished (automatic resume by NotionTool). '
               'Pick up exactly where you stopped and finish the task.')
CHECK_SECONDS = 30
GRACE_SECONDS = 60
MAX_CHAIN = 5
CHAIN_WINDOW = 1800   # without a visible user message, a resume within 30 min continues the chain
WINDOW = 40           # newest transcript entries read per check
IDLE = (None, 'idle', 'completed')
EXCLUDED_STATES = ('missing', 'disabled', 'retired')
ACTIVE_TASK_STATES = ('running', 'uncertain')


def stamp(entity):
    value = entity.get('created_at') or entity.get('createdAt') or entity.get('created_time') or 0
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value / 1000 if value > 1e11 else value


def _role(entity):
    kind = str(entity.get('kind') or entity.get('type') or '').lower()
    if kind in ('assistant_message', 'user_message'):
        return kind
    if 'survey' in repr(entity).lower():
        return 'assistant_message'  # an open question to the user waits for the user; it is no interruption
    if entity.get('tool_use_id') or any(word in kind for word in ('tool', 'execution', 'computer')):
        return 'tool'
    if 'error' in kind:
        return 'error'
    return ''  # metadata such as turn_completed never decides


def interrupted_turn(transcript):
    """{'key', 'kind', 'stamp', 'user_ids'} when the newest turn ended without a reply, else None."""
    session = transcript.get('session') or {}
    if transcript.get('session_pending') or session.get('status') not in IDLE:
        return None
    entities = sorted((p.get('entity') or {} for p in transcript.get('patches', []) if p.get('op') == 'put'),
                      key=lambda e: e.get('sequence', 0))
    relevant = [e for e in entities if _role(e)]
    if not relevant or _role(relevant[-1]) == 'assistant_message':
        return None
    last = relevant[-1]
    key = str(last.get('id') or last.get('sequence') or '')
    if not key:
        return None
    users = [e for e in relevant if _role(e) == 'user_message']
    user = users[-1] if users else {}
    return dict(key=key, kind=str(last.get('kind') or last.get('type') or ''), stamp=stamp(last),
                user_ids=[str(x) for x in (user.get('client_message_id'), user.get('id')) if x])


def _active(task):
    return task.get('state') in ACTIVE_TASK_STATES or bool(task.get('remote_unknown'))


class TurnWatch:
    def __init__(self, store, adapter, engine, clock=time.time):
        self.store, self.adapter, self.engine, self.clock = store, adapter, engine, clock
        self.first_seen, self.sending, self.blocked = {}, set(), False
        self.spawn = lambda fn, *args: threading.Thread(target=fn, args=args, daemon=True, name='turn-resume').start()

    def workers(self):
        from .harness_account import main_chat, selected_account
        account_id = selected_account(self.store)
        if not account_id or self.store.get('accounts', account_id, {}).get('state') != 'ready':
            return []
        main = (main_chat(self.store, account_id) or {}).get('main_worker')
        busy = {t.get('worker_id') for t in self.store.all('tasks') if _active(t)}
        return [w for w in self.store.all('workers')
                if w.get('account_id') == account_id and w.get('thread_id')
                and w.get('state') not in EXCLUDED_STATES and w.get('join_state') != 'pending'
                and (w['id'] == main or w['id'] in busy)]

    def loop(self):
        from .network_budget import set_thread_priority
        set_thread_priority('background')
        while not self.engine.stop_event.wait(CHECK_SECONDS):
            failed = None
            try:
                workers = self.workers()
            except Exception as exc:
                workers, failed = [], exc
            for worker in workers:
                if self.engine.stop_event.is_set():
                    return
                try:
                    self.check(worker)
                except Exception as exc:  # one unreadable chat never stops the others
                    failed = failed or exc
            if failed and not self.blocked:
                self.blocked = True
                from .telemetry import classify
                self.store.event('turn_watch_waiting', error=type(failed).__name__, error_class=classify(failed),
                                 message='Interrupted-turn watch deferred; it retries automatically')
            elif not failed and self.blocked:
                self.blocked = False
                self.store.event('turn_watch_resumed', message='Interrupted-turn watch works again')

    def check(self, worker):
        if worker['id'] in self.sending:
            return None
        client = self.adapter.client(worker['account_id'])
        found = interrupted_turn(client.transcript(worker['thread_id'], limit=WINDOW))
        if not found:
            return None
        if (self.store.get('workers', worker['id'], {}).get('resume') or {}).get('key') == found['key']:
            return None  # already handled, also across restarts
        now = self.clock()
        since = found['stamp'] or self.first_seen.setdefault(worker['id'] + ':' + found['key'], now)
        if now - since < GRACE_SECONDS:
            return None
        with self.engine.lock:
            fresh = self.store.get('workers', worker['id'])
            if not fresh or fresh.get('thread_id') != worker['thread_id']:
                return None
            state = dict(fresh.get('resume') or {})
            if state.get('key') == found['key']:
                return None
            steps = list(state.get('steps') or [])
            ours = bool(set(found['user_ids']) & set(steps))
            unknown = not found['user_ids'] and now - state.get('at', 0) < CHAIN_WINDOW
            chain = state.get('chain', 0) + 1 if (ours or unknown) else 1
            if chain > MAX_CHAIN:
                if state.get('exhausted') != found['key']:
                    state['exhausted'] = found['key']
                    fresh['resume'] = state
                    self.store.put('workers', fresh)
                    self.store.event('turn_resume_exhausted', worker_id=worker['id'], thread_id=worker['thread_id'],
                                     chain=chain - 1, message='Automatic resume stopped after %d attempts in a row; '
                                                               'continue this chat by hand' % (chain - 1))
                return None
            step = self.adapter.new_id(client.info['space_id'], '00aa')
            state.update(key=found['key'], previous_key=state.get('key', ''), chain=chain, at=now,
                         steps=(steps + [step])[-10:])
            fresh['resume'] = state
            self.store.put('workers', fresh)
            for task in self.store.all('tasks'):
                if task.get('worker_id') == worker['id'] and _active(task):
                    # The recovery keeps reading this turn past our continue message (notion.py).
                    task['resume_steps'] = (list(task.get('resume_steps') or []) + [step])[-10:]
                    self.store.put('tasks', task)
            self.sending.add(worker['id'])
        self.store.event('turn_interrupted', worker_id=worker['id'], thread_id=worker['thread_id'], after=found['kind'],
                         chain=chain, message='Interrupted turn detected; NotionTool sends an automatic continue message')
        self.spawn(self.send, worker, step, found['key'])
        return step

    def send(self, worker, step, key):
        from .network_budget import request_priority
        try:
            with request_priority('rest'):
                sent = self.adapter.continue_turn(worker, RESUME_TEXT, step)
            if not sent:
                self.revert(worker, key, 'turn_resume_skipped', 'The chat became busy again; nothing was sent')
        except Exception as exc:
            self.revert(worker, key, 'turn_resume_failed', type(exc).__name__ + ': ' + str(exc)[:200])
        finally:
            self.sending.discard(worker['id'])

    def revert(self, worker, key, kind, reason):
        # Not sent or not confirmed: a later check may resume again; the chain cap still applies.
        with self.engine.lock:
            fresh = self.store.get('workers', worker['id'])
            if fresh and (fresh.get('resume') or {}).get('key') == key:
                fresh['resume'] = dict(fresh['resume'], key=fresh['resume'].get('previous_key', ''))
                self.store.put('workers', fresh)
        self.store.event(kind, worker_id=worker['id'], thread_id=worker['thread_id'], message=reason)
