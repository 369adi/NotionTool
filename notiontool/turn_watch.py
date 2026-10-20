"""Auto-resume of interrupted turns (Phase 4, Fix H).

A turn counts as interrupted when its chat is idle and the newest relevant entry is a tool step,
an error or an unanswered user message instead of an assistant reply - e.g. a turn that stopped
right after a tool result, as happens when a mobile client re-attaches to a running answer.
The watcher then sends one short continue message into that chat:
- only harness chats: the Main chat of the selected account, Coworkers with an active task and
  (setting main_auto_continue) the operator Main chat
  (chats the user renamed, disabled or unconfirmed chats are never touched),
- once per interrupted entry, also across restarts (the handled key is stored on the worker),
- after a grace period, so the user can continue by hand first,
- at most MAX_CHAIN automatic resumes in a row (operator Main: MAX_CHAIN_OPERATOR),
- never for an operator chat whose newest user message asks to stop,
- through the shared request budget (I11); checks run at background priority.
The watch runs while the setting turn_watch is on (default True), independent of background_remote.

Operator usage: for operator workers only, at most every main_context_check_seconds (default 300)
the already fetched transcript is added to ChatContext and the usage (tokens, user turns) is stored
as worker['context_usage']; main_switch.check() then decides whether to ask the user about a switch.
"""
import inspect
import re
import threading
import time

RESUME_TEXT = ('Continue: your previous turn was interrupted before it finished (automatic resume by NotionTool). '
               'Pick up exactly where you stopped and finish the task.')
CHECK_SECONDS = 30
GRACE_SECONDS = 150  # a Main often thinks or waits on a slow tool for more than a minute between steps
PENDING_TOOL_SECONDS = 420  # newest step is a tool call without result; every NotionTool tool times out well before this
STALL_SECONDS = 600  # chat still says 'running' but nothing new for 10 min: a hung turn (e.g. a broken tool call)
MAX_CHAIN = 5
MAX_CHAIN_OPERATOR = 20  # the operator Main may work in idle mode for hours
CHAIN_WINDOW = 1800   # without a visible user message, a resume within 30 min continues the chain
WINDOW = 40           # newest transcript entries read per check
USAGE_SECONDS = 300   # default of main_context_check_seconds
USAGE_FIRST_WINDOW = 1000  # one larger read per thread, so the old history counts
USAGE_ERROR_SECONDS = 3600  # at most one operator_usage_failed event per thread per hour
IDLE = (None, 'idle', 'completed')
EXCLUDED_STATES = ('missing', 'disabled', 'retired')
ACTIVE_TASK_STATES = ('running', 'uncertain')
OPERATOR = 'operator'
STOP_WORDS = re.compile(r'\b(stop|halt|pause|warte|aufh(?:ö|oe)ren)\b', re.IGNORECASE)
# No paused-turn marker is documented in notion.py transcript() (it returns getThreadTranscript as is).
# These defensive markers make a limit/pause entry count as an interruption if Notion ever writes one.
PAUSE_MARKERS = ('interrupt', 'timeout')
# Notion's own step/time-limit pause waits for the user's 'Continue' click: never resumed automatically.
LIMIT_MARKERS = ('step_limit', 'limit_reached', 'max_steps', 'time_limit', 'paused', 'pause', 'confirm')


def stamp(entity):
    value = entity.get('created_at') or entity.get('createdAt') or entity.get('created_time') or 0
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value / 1000 if value > 1e11 else value


def text_of(value):
    """Plain text of a transcript value (string, rich-text list or {'type': 'text'} dict)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ' '.join(t for t in (text_of(x) for x in value) if t)
    if isinstance(value, dict):
        return text_of(value.get('content', value.get('text', value.get('value', ''))))
    return ''


def _reason(entity):
    return str(entity.get('stop_reason') or entity.get('stopReason') or entity.get('reason') or '').lower()


def _limit_pause(entity, kind):
    reason = _reason(entity)
    return any(marker in kind or marker in reason for marker in LIMIT_MARKERS)


def _paused(entity, kind):
    reason = _reason(entity)
    return any(marker in kind or marker in reason for marker in PAUSE_MARKERS)


def _role(entity):
    kind = str(entity.get('kind') or entity.get('type') or '').lower()
    if kind in ('assistant_message', 'user_message'):
        return kind
    if 'survey' in repr(entity).lower():
        return 'assistant_message'  # an open question to the user waits for the user; it is no interruption
    if _limit_pause(entity, kind):
        return 'assistant_message'  # waits for the user's Continue click, like an open question
    if _paused(entity, kind):
        return 'error'  # a turn Notion stopped for a step/time limit is unfinished
    if entity.get('tool_use_id') or any(word in kind for word in ('tool', 'execution', 'computer')):
        return 'tool'
    if 'error' in kind:
        return 'error'
    return ''  # metadata such as turn_completed never decides


def _is_resume(entity):
    return RESUME_TEXT[:40] in text_of(entity.get('content', entity.get('text', '')))


def user_turns(entities):
    """User messages (kind/type user_message or role user), without our own resume messages."""
    count = 0
    for entity in entities:
        kind = str(entity.get('kind') or entity.get('type') or '').lower()
        if (kind == 'user_message' or str(entity.get('role') or '').lower() == 'user') and not _is_resume(entity):
            count += 1
    return count


def _pending_tool(last, entities):
    """True when the newest step is a tool call whose result has not arrived yet (the tool still runs)."""
    if _role(last) != 'tool':
        return False
    kind = str(last.get('kind') or last.get('type') or '').lower()
    if any(word in kind for word in ('result', 'output', 'response', 'error')):
        return False
    call_id = last.get('tool_use_id')
    for entity in entities:
        other = str(entity.get('kind') or entity.get('type') or '').lower()
        if entity is not last and call_id and entity.get('tool_use_id') == call_id and 'result' in other:
            return False
    return True


def interrupted_turn(transcript, now=None):
    """{'key', 'kind', 'stamp', 'user_ids', 'user_text'} when the newest turn ended without a reply, else None."""
    session = transcript.get('session') or {}
    entities = sorted((p.get('entity') or {} for p in transcript.get('patches', []) if p.get('op') == 'put'),
                      key=lambda e: e.get('sequence', 0))
    stalled = False
    if transcript.get('session_pending') or session.get('status') not in IDLE:
        # A turn that still says 'running' but wrote nothing for STALL_SECONDS is hung, not working.
        newest_any = max([stamp(e) for e in entities] or [0])
        if now is None or not newest_any or now - newest_any < STALL_SECONDS:
            return None
        stalled = True
    relevant = [e for e in entities if _role(e)]
    if not relevant or (_role(relevant[-1]) == 'assistant_message' and not stalled):
        return None
    last = relevant[-1]
    key = str(last.get('id') or last.get('sequence') or '')
    if not key:
        return None
    users = [e for e in relevant if _role(e) == 'user_message']
    user = users[-1] if users else {}
    visible = [e for e in users if not _is_resume(e)]
    user_text = text_of(visible[-1].get('content', visible[-1].get('text', ''))) if visible else ''
    # Any newer entry (also metadata) is activity: quiet time counts from the newest entry, not the last step.
    newest = max([stamp(e) for e in entities] + [stamp(last)])
    return dict(key=key, kind=str(last.get('kind') or last.get('type') or ''), stamp=newest,
                pending=False if stalled else _pending_tool(last, entities), stalled=stalled,
                user_ids=[str(x) for x in (user.get('client_message_id'), user.get('id')) if x],
                user_text=user_text)


def stop_requested(text):
    return bool(STOP_WORDS.search(text or ''))


def _active(task):
    return task.get('state') in ACTIVE_TASK_STATES or bool(task.get('remote_unknown'))


def _takes_limit(fn):
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return 'limit' in params or any(p.kind == p.VAR_KEYWORD for p in params.values())


class TurnWatch:
    def __init__(self, store, adapter, engine, clock=time.time):
        self.store, self.adapter, self.engine, self.clock = store, adapter, engine, clock
        self.first_seen, self.sending, self.blocked = {}, set(), False
        self.usage_at, self.usage_failed, self.full_read = {}, {}, set()
        self.spawn = lambda fn, *args: threading.Thread(target=fn, args=args, daemon=True, name='turn-resume').start()

    def operator_workers(self, account_id):
        """Worker ids of the account's operator Main chats that are not released."""
        if not self.store.settings().get('main_auto_continue', True):
            return set()
        chats = {c.get('main_worker') for c in self.store.all('chats')
                 if c.get('operator') and c.get('state') != 'released' and c.get('main_worker')}
        return {w['id'] for w in self.store.all('workers')
                if w.get('account_id') == account_id and w.get('purpose') == OPERATOR and w['id'] in chats}

    def workers(self):
        from .harness_account import main_chat, selected_account
        account_id = selected_account(self.store)
        if not account_id or self.store.get('accounts', account_id, {}).get('state') != 'ready':
            return []
        main = (main_chat(self.store, account_id) or {}).get('main_worker')
        busy = {t.get('worker_id') for t in self.store.all('tasks') if _active(t)}
        operators = self.operator_workers(account_id)
        return [w for w in self.store.all('workers')
                if w.get('account_id') == account_id and w.get('thread_id')
                and w.get('state') not in EXCLUDED_STATES and w.get('join_state') != 'pending'
                and (w['id'] == main or w['id'] in busy or w['id'] in operators)]

    def loop(self):
        from .network_budget import set_thread_priority
        set_thread_priority('background')
        while not self.engine.stop_event.wait(CHECK_SECONDS):
            if not self.store.settings().get('turn_watch', True):
                continue
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

    def measure_usage(self, worker, client, transcript):
        """Operator workers only, throttled: store context_usage and run main_switch.check(). Never raises."""
        if worker.get('purpose') != OPERATOR:
            return None
        now = self.clock()
        ident = str(worker.get('account_id', '')) + ':' + str(worker.get('thread_id', ''))
        try:
            settings = dict(self.store.settings() or {})
            try:
                every = float(settings.get('main_context_check_seconds', USAGE_SECONDS))
            except (TypeError, ValueError):
                every = USAGE_SECONDS
            last = self.usage_at.get(worker['id'])
            if last is not None and now - last < every:
                return None
            self.usage_at[worker['id']] = now
            if self.store.get('thread_context', ident) is None and ident not in self.full_read:
                self.full_read.add(ident)  # never more than one larger read per thread
                if _takes_limit(client.transcript):
                    transcript = client.transcript(worker['thread_id'], limit=USAGE_FIRST_WINDOW)
            from .chat_context import ChatContext
            from . import main_switch
            record = ChatContext(self.store).observe(worker, transcript)
            usage = dict(estimated_tokens=int(record.get('estimated_tokens') or 0),
                         turns=user_turns((record.get('entities') or {}).values()))
            with self.engine.lock:
                fresh = self.store.get('workers', worker['id'])
                if not fresh or fresh.get('thread_id') != worker.get('thread_id'):
                    return None
                previous = fresh.get('context_ask')
                if fresh.get('context_usage') != usage:
                    fresh['context_usage'] = usage
                    self.store.put('workers', fresh)
                notice = main_switch.check(self.store, fresh, usage, settings, now=now)
            if notice and not previous:
                self.store.event('main_context_ask', worker_id=worker['id'], thread_id=worker['thread_id'],
                                 tokens=usage['estimated_tokens'], turns=usage['turns'],
                                 message='Operator Main is large (%d tokens, %d user turns); ask the user about a switch'
                                         % (usage['estimated_tokens'], usage['turns']))
            return usage
        except Exception as exc:
            if now - self.usage_failed.get(ident, float('-inf')) >= USAGE_ERROR_SECONDS:
                self.usage_failed[ident] = now
                try:
                    self.store.event('operator_usage_failed', worker_id=worker.get('id', ''),
                                     thread_id=worker.get('thread_id', ''), error=type(exc).__name__,
                                     message='Operator Main usage check failed; it retries later: ' + str(exc)[:200])
                except Exception:
                    pass
            return None

    def check(self, worker):
        if worker['id'] in self.sending:
            return None
        client = self.adapter.client(worker['account_id'])
        transcript = client.transcript(worker['thread_id'], limit=WINDOW)
        self.measure_usage(worker, client, transcript)
        found = interrupted_turn(transcript, now=self.clock())
        if not found:
            return None
        if (self.store.get('workers', worker['id'], {}).get('resume') or {}).get('key') == found['key']:
            return None  # already handled, also across restarts
        operator = worker.get('purpose') == OPERATOR
        limit = MAX_CHAIN_OPERATOR if operator else MAX_CHAIN
        now = self.clock()
        since = found['stamp'] or self.first_seen.setdefault(worker['id'] + ':' + found['key'], now)
        need = PENDING_TOOL_SECONDS if found.get('pending') else GRACE_SECONDS
        if now - since < need:
            return None
        with self.engine.lock:
            fresh = self.store.get('workers', worker['id'])
            if not fresh or fresh.get('thread_id') != worker['thread_id']:
                return None
            state = dict(fresh.get('resume') or {})
            if state.get('key') == found['key']:
                return None
            if operator and stop_requested(found.get('user_text', '')):
                if state.get('stopped') != found['key']:
                    state['stopped'] = found['key']
                    fresh['resume'] = state
                    self.store.put('workers', fresh)
                    self.store.event('turn_resume_stopped', worker_id=worker['id'], thread_id=worker['thread_id'],
                                     message='The user asked to stop; no automatic continue for this chat')
                return None
            steps = list(state.get('steps') or [])
            ours = bool(set(found['user_ids']) & set(steps))
            unknown = not found['user_ids'] and now - state.get('at', 0) < CHAIN_WINDOW
            chain = state.get('chain', 0) + 1 if (ours or unknown) else 1
            if chain > limit:
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
        self.store.event('turn_stalled' if found.get('stalled') else 'turn_interrupted', worker_id=worker['id'],
                         thread_id=worker['thread_id'], after=found['kind'],
                         chain=chain, message='Interrupted turn detected; NotionTool sends an automatic continue message')
        self.spawn(self.send, worker, step, found['key'])
        return step

    def send(self, worker, step, key):
        from .network_budget import request_priority
        try:
            # The resume uses the task's own model when the adapter supports it (notion.py continue_turn).
            task = next((t for t in self.store.all('tasks') if t.get('worker_id') == worker['id'] and _active(t)), None)
            with request_priority('rest'):
                if task is not None and self.resume_takes_task():
                    sent = self.adapter.continue_turn(worker, RESUME_TEXT, step, task=task)
                else:
                    sent = self.adapter.continue_turn(worker, RESUME_TEXT, step)
            if not sent:
                self.revert(worker, key, 'turn_resume_skipped', 'The chat became busy again; nothing was sent')
        except Exception as exc:
            self.revert(worker, key, 'turn_resume_failed', type(exc).__name__ + ': ' + str(exc)[:200])
        finally:
            self.sending.discard(worker['id'])

    def resume_takes_task(self):
        cached = getattr(self, '_resume_takes_task', None)
        if cached is None:
            try:
                params = inspect.signature(self.adapter.continue_turn).parameters
                cached = 'task' in params or any(p.kind == p.VAR_KEYWORD for p in params.values())
            except (TypeError, ValueError):
                cached = False
            self._resume_takes_task = cached
        return cached

    def revert(self, worker, key, kind, reason):
        # Not sent or not confirmed: a later check may resume again; the chain cap still applies.
        with self.engine.lock:
            fresh = self.store.get('workers', worker['id'])
            if fresh and (fresh.get('resume') or {}).get('key') == key:
                fresh['resume'] = dict(fresh['resume'], key=fresh['resume'].get('previous_key', ''))
                self.store.put('workers', fresh)
        self.store.event(kind, worker_id=worker['id'], thread_id=worker['thread_id'], message=reason)
