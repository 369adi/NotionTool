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

# Shared 'session finished' rule with recover and the send checks. No import cycle: notion.py never imports turn_watch.
from .notion import FINISHED_STATUSES, session_state

RESUME_TEXT = ('Continue: your previous turn was interrupted before it finished (automatic resume by NotionTool). '
               'Pick up exactly where you stopped and finish the task.')
# Idle mode (setting idle_mode, default on, button in the status strip): a Main chat whose turn ended
# shortly before NotionTool restarted (e.g. a build swap) gets one automatic 'continue' after the restart,
# so the user never has to type 'weiter' after an update.
RESTART_TEXT = ('Weiter (automatisch nach dem Neustart von NotionTool, Idle-Modus): '
                'mach mit den offenen Punkten weiter, ohne nachzufragen.')
RESTART_WINDOW = 900  # the turn ended at most 15 min before the restart
RESTART_SETTLE = 20   # seconds after the start before the first restart continue
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
IDLE = FINISHED_STATUSES  # same constant as notion.session_state: a missing status is 'unknown', never idle
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
    state = session_state(transcript)  # shared rule with recover and the send checks (notion.py)
    if state == 'unknown':
        return None  # no session status: unknown is never 'interrupted', so no automatic continue is sent
    entities = sorted((p.get('entity') or {} for p in (transcript.get('patches') or []) if p.get('op') == 'put'),
                      key=lambda e: e.get('sequence') or 0)  # None sequences broke sorting (TypeError since 18:03)
    stalled = False
    if state == 'running':
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


def restart_continue(transcript, started):
    """{'key', 'user_text'} when the newest turn finished with a reply shortly before the app start, else None."""
    if session_state(transcript) != 'finished':
        return None
    entities = sorted((p.get('entity') or {} for p in (transcript.get('patches') or []) if p.get('op') == 'put'),
                      key=lambda e: e.get('sequence') or 0)
    relevant = [e for e in entities if _role(e)]
    if not relevant or _role(relevant[-1]) != 'assistant_message':
        return None
    last = relevant[-1]
    when = stamp(last)
    if not when or when >= started or started - when > RESTART_WINDOW:  # strictly before the start
        return None
    key = str(last.get('id') or last.get('sequence') or '')
    if not key:
        return None
    visible = [e for e in relevant if _role(e) == 'user_message' and not _is_resume(e)]
    user_text = text_of(visible[-1].get('content', visible[-1].get('text', ''))) if visible else ''
    return dict(key='restart:' + key, user_text=user_text)


# A person types at most a few messages between two context checks; more means a counting artefact.
MAX_MAIN_TURNS_PER_CHECK = 3


def charge_main_usage(store, worker, before, after, now=None):
    """Charge the Main's own turns to the 6-hour token budget (token_budget.py). Never raises.

    The Main is typed in the desktop window, so NotionTool never sends its turns itself. Each new user
    turn makes Notion read the whole chat again (the context before it) and the chat grew by the new
    text. The first observation of a thread only sets the baseline (old history is not charged).
    """
    try:
        if not isinstance(before, dict) or not isinstance(after, dict):
            return 0
        b_tokens, a_tokens = int(before.get('estimated_tokens') or 0), int(after.get('estimated_tokens') or 0)
        b_turns, a_turns = int(before.get('turns') or 0), int(after.get('turns') or 0)
        if a_turns < b_turns or a_tokens < b_tokens:
            # The transcript window shifted (restart, compression, shorter read): a new baseline, never a charge.
            # Before this, every drop and rise again was charged as dozens of new turns (live: 44.7M for 9 turns).
            return 0
        grown = a_tokens - b_tokens
        turns = min(a_turns - b_turns, MAX_MAIN_TURNS_PER_CHECK)
        from .token_budget import context_cap
        cap = context_cap(store)  # a turn reads at most one model context window
        tokens = min(grown, cap) + turns * min(int(before.get('estimated_tokens') or 0), cap)
        if not tokens:
            return 0
        from .token_budget import charge
        charge(store, dict(worker, purpose=worker.get('purpose') or OPERATOR), tokens, 0, turn=bool(turns), now=now)
        return tokens
    except Exception:
        return 0


def budget_used_up(store, now=None):
    try:
        from .token_budget import exhausted
        return bool(exhausted(store, now))
    except Exception:
        return False


def _where(exc):
    """Short 'file:line Error: text' of an exception for the event log."""
    try:
        import traceback
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        name = frame.filename.replace(chr(92), '/').rsplit('/', 1)[-1]
        return '%s:%d %s: %s' % (name, frame.lineno, type(exc).__name__, str(exc)[:160])
    except Exception:
        return type(exc).__name__


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
        self.started = clock()

    def operator_workers(self, account_id):
        """Worker ids of the account's operator Main chats that are not released."""
        if not self.store.settings().get('main_auto_continue', True):
            return set()
        chats = {c.get('main_worker') for c in self.store.all('chats')
                 if c.get('operator') and c.get('state') != 'released' and c.get('main_worker')}
        return {w['id'] for w in self.store.all('workers')
                if w.get('account_id') == account_id and w.get('purpose') == OPERATOR and w['id'] in chats}

    def workers(self):
        """Watched chats over ALL ready accounts (not only the selected one): every Main chat that is
        not released, the operator Mains (setting main_auto_continue) and coworkers with an active task.
        A hung Main on any account is resumed; nothing is tied to one account."""
        from .harness_account import main_chat
        ready = {a['id'] for a in self.store.all('accounts') if a.get('state') == 'ready'}
        if not ready:
            return []
        auto = self.store.settings().get('main_auto_continue', True)
        mains = set()
        for chat in self.store.all('chats'):
            if chat.get('state') == 'released' or not chat.get('main_worker'):
                continue
            if chat.get('operator') and not auto:
                continue
            mains.add(chat['main_worker'])
        operators = set()
        for account_id in ready:
            mains.add((main_chat(self.store, account_id) or {}).get('main_worker'))
            operators |= self.operator_workers(account_id)
        mains.discard(None)
        busy = {t.get('worker_id') for t in self.store.all('tasks') if _active(t)}
        return [w for w in self.store.all('workers')
                if w.get('account_id') in ready and w.get('thread_id')
                and w.get('state') not in EXCLUDED_STATES and w.get('join_state') != 'pending'
                and (w['id'] in mains or w['id'] in busy or w['id'] in operators)]

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
                                 detail=_where(failed), message='Interrupted-turn watch deferred; it retries automatically')
            elif not failed and self.blocked:
                self.blocked = False
                self.store.event('turn_watch_resumed', message='Interrupted-turn watch works again')

    def measure_usage(self, worker, client, transcript):
        """Operator workers only, throttled: store context_usage and run main_switch.check(). Never raises."""
        if worker.get('purpose') not in (OPERATOR, 'main'):  # the selected account's Main counts for the 6 h budget
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
                try:
                    charge_main_usage(self.store, fresh, fresh.get('context_usage'), usage, now)
                except Exception as exc:  # the 6 h budget never blocks the context measurement
                    if now - getattr(self, 'budget_failed_at', float('-inf')) >= USAGE_ERROR_SECONDS:
                        self.budget_failed_at = now
                        try:
                            self.store.event('token_budget_failed', worker_id=worker.get('id', ''), error=type(exc).__name__,
                                             message='Token budget charge failed; measurement continues: ' + str(exc)[:200])
                        except Exception:
                            pass
                if fresh.get('context_usage') != usage:
                    fresh['context_usage'] = usage
                    self.store.put('workers', fresh)
                notice = main_switch.check(self.store, fresh, usage, settings, now=now) if fresh.get('purpose') == OPERATOR else None
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
            return self.check_restart(worker, transcript)
        if (self.store.get('workers', worker['id'], {}).get('resume') or {}).get('key') == found['key']:
            return None  # already handled, also across restarts
        # Idle mode: every Main chat (purpose 'main' or the operator Main) may keep working on its own
        # for hours; coworker turns keep the short chain. Not tied to one account.
        operator = worker.get('purpose') in (OPERATOR, 'main')
        limit = MAX_CHAIN_OPERATOR if operator else MAX_CHAIN
        now = self.clock()
        since = found['stamp'] or self.first_seen.setdefault(worker['id'] + ':' + found['key'], now)
        need = PENDING_TOOL_SECONDS if found.get('pending') else GRACE_SECONDS
        if now - since < need:
            return None
        if budget_used_up(self.store, now):
            return None  # 6-hour token budget used up: no automatic continue until the reset
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

    def check_restart(self, worker, transcript):
        """Idle mode: one automatic continue for a Main whose turn ended shortly before the restart."""
        if worker.get('purpose') not in (OPERATOR, 'main'):
            return None
        if not self.store.settings().get('idle_mode', True):
            return None
        now = self.clock()
        if now - self.started < RESTART_SETTLE:
            return None
        hit = restart_continue(transcript, self.started)
        if not hit or stop_requested(hit.get('user_text', '')):
            return None
        if budget_used_up(self.store, now):
            return None
        client = self.adapter.client(worker['account_id'])
        with self.engine.lock:
            fresh = self.store.get('workers', worker['id'])
            if not fresh or fresh.get('thread_id') != worker['thread_id']:
                return None
            if fresh.get('restart_resume') == hit['key']:
                return None
            fresh['restart_resume'] = hit['key']
            self.store.put('workers', fresh)
            step = self.adapter.new_id(client.info['space_id'], '00aa')
            self.sending.add(worker['id'])
        self.store.event('turn_restart_continue', worker_id=worker['id'], thread_id=worker['thread_id'],
                         message='Idle mode: NotionTool restarted after this Main finished; it continues automatically')
        self.spawn(self.send, worker, step, hit['key'], RESTART_TEXT)
        return step

    def send(self, worker, step, key, text=RESUME_TEXT):
        from .network_budget import request_priority
        try:
            # The resume uses the task's own model when the adapter supports it (notion.py continue_turn).
            task = next((t for t in self.store.all('tasks') if t.get('worker_id') == worker['id'] and _active(t)), None)
            with request_priority('rest'):
                if task is not None and self.resume_takes_task():
                    sent = self.adapter.continue_turn(worker, text, step, task=task)
                else:
                    sent = self.adapter.continue_turn(worker, text, step)
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
