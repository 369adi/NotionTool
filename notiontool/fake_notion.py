"""Simulated Notion for the test app: no network, no real accounts.

The fake sits at the transport layer, so the real NotionClient / NotionAdapter code
(call() status mapping, threads() fallback, transcript() 'Response too large' retry,
run()/recover() stream parsing, rename_thread, delete_thread) runs unchanged:

- FakeState     JSON state file (env NOTIONTOOL_FAKE_STATE, default <data-dir>/fake_notion.json),
                re-read on every call and written atomically, so it survives an app restart.
- FakeTransport request(path, body, headers=None, timeout=45) -> (status, text) and
                stream(path, body, headers=None, timeout=60, cancel=None) -> NDJSON lines,
                with _base, retry_after, model_catalog, close() and a .browser stub.
- FakeClient    a NotionClient on a FakeTransport: headers, call, identity, model, threads,
                transcript (inherited, same return shapes) plus helpers rename_thread,
                delete_thread, thread_records, send and wait.
- install(adapter) makes adapter.client(account_id, anonymous=False) return FakeClient
                objects. Nothing else is patched.
- FakeNotionAdapter(NotionAdapter) the real adapter with only client(), edge_transport(),
                browser_login() and adopt_session_cookies() replaced (refuses to start unless
                NOTIONTOOL_TEST_INSTANCE=1 or force=True).
- seed_scenario(store) writes the simulated accounts, chat workers and Main chats as store records.
- main(argv)    command line: python -m notiontool.fake_notion [--state F | --data-dir D]
                reset | seed | set-fault TARGET NAME [key=value ...] | clear-fault TARGET [NAME] | show [TARGET]
                TARGET is a thread id, an account key ('ready', 'noai') or 'account/title' ('ready/Coworker 1').

Seed: two accounts, 'ready' (Notion AI on) and 'noai' (Notion AI disabled), each with a
Main chat, three Coworker chats and two private chats. Answers are deterministic and end
with 'RESULT: DONE'.

Unknown endpoints answer HTTP 400 'fake: unhandled endpoint X' (never a silent success) and log a
fake_unhandled_endpoint event (state['events'] and, via FakeNotionAdapter/install, the store events).

Faults per thread, in state['faults'][thread_id] = {name: params} (set_fault() or by hand);
clear_fault(target, name=None) removes one or all:
  too_large      {'max_limit': N}    getThreadTranscript with limit > N fails with 'Response too large'
  hang           {'seconds': S}      the send commits the user step, then only keep-alive lines until
                                     cancel is set or min(timeout, S) passes (then TimeoutError)
  interrupted    {'cut': True}       the reply stops mid-answer without the final line; the stream is
                                     cut (OSError) unless cut is False
  missing        {}                  HTTP 404 on transcript/send, not listed, record alive=False
  deleted_delay  {'seconds': K}      already deleted (404, alive=False) but still listed for K seconds
                                     after deleted_at
  rate_limit     {'retry_after': S,  HTTP 429 on every request/send that touches the thread; the transport's
                  'times': N}        retry_after is S, so call() raises RateLimited(S) and the stream raises
                                     NotionHttpError(429, retry_after=S). Nothing is committed. With times the
                                     fault clears itself after N refusals.
  busy           {}                  another turn is running: transcript session 'running' + session_pending,
                                     record latest_user_or_trigger_time > completed_time; a send is refused
                                     with NotionHttpError(409) and commits nothing
  empty_stream   {}                  empty_stream_committed: the stream ends after patch-start with no text,
                                     but Notion commits the user step, the full reply and turn_completed
  lost_stream    {}                  cut_stream_lost: the stream is cut (OSError) before Notion committed
                                     anything; the transcript does not contain the step
  error_step     {'message': T}      the stream yields an 'error' step; the transcript gets an agent_error
                                     entry and turn_completed without reply text (turn_state done_error)
  slow           {'seconds': S}      the reply arrives spread over S seconds (keep-alive lines between the
                                     pieces); past the call timeout TimeoutError, the reply is committed anyway
  chat_list_unavailable {}           account fault (on the account key or any of its threads): the space
                                     record is missing, so notion.threads() raises ChatListUnavailable
  welcome_not_listed {'seconds': K}  the chat is alive and readable but not in the space thread_ids (for K
                                     seconds from 'since', forever without seconds). On an account key every
                                     chat created later (createThread) inherits the fault.
"""
import contextlib
import copy
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from .notion import MODEL_NAMES, NotionAdapter, NotionClient, text_content
from .vendor.transport import NotionHttpError

FAKE_BASE = 'https://fake.notion.invalid'
STATE_ENV = 'NOTIONTOOL_FAKE_STATE'
TEST_INSTANCE_ENV = 'NOTIONTOOL_TEST_INSTANCE'
STATE_FILE = 'fake_notion.json'
STATE_VERSION = 1
RESULT_LINE = 'RESULT: DONE'
FAULTS = ('too_large', 'hang', 'interrupted', 'missing', 'deleted_delay',
          'rate_limit', 'busy', 'empty_stream', 'lost_stream', 'error_step', 'slow',
          'chat_list_unavailable', 'welcome_not_listed')
ACCOUNT_FAULTS = ('chat_list_unavailable', 'welcome_not_listed')  # may target an account key
DEFAULT_DELETED_DELAY = 30
DEFAULT_RETRY_AFTER = 30
DEFAULT_SLOW_SECONDS = 2.0
ERROR_STEP_TEXT = 'fake: simulated error step (Notion reported an error for this turn)'
CALL_LOG_KEEP = 500
EVENT_LOG_KEEP = 200
UNHANDLED_EVENT = 'fake_unhandled_endpoint'
WELCOME_TITLE = 'Welcome to Notion'
KIND_PURPOSE = {'main': 'main', 'coworker': 'worker', 'private': 'private'}

# (key, email, user name, workspace name, Notion AI enabled)
ACCOUNTS = (
    ('ready', 'main1@fake-notion.test', 'Fake Main One', 'Fake Workspace One', True),
    ('noai', 'noai1@fake-notion.test', 'Fake No AI', 'Fake Workspace No AI', False),
)
# (kind, title) per account
CHATS = (
    ('main', 'Main'),
    ('coworker', 'Coworker 1'),
    ('coworker', 'Coworker 2'),
    ('coworker', 'Coworker 3'),
    ('private', 'Project notes'),
    ('private', 'Holiday plans'),
)

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class SimulationRefused(RuntimeError):
    """FakeNotionAdapter was asked to start outside the test app."""


def _lock_for(path):
    key = os.path.normcase(os.path.abspath(str(path)))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def fake_uuid(*parts):
    """Deterministic UUIDv8-shaped id (same shape as Notion's space and thread ids)."""
    digest = hashlib.sha256('|'.join(str(p) for p in parts).encode('utf-8')).hexdigest()[:32]
    digest = digest[:12] + '8' + digest[13:16] + '8' + digest[17:]
    return str(uuid.UUID(digest))


def _ms(seconds):
    return int(seconds * 1000)


def resolve_state_path(path=None, data_dir=None):
    """Explicit path, else env NOTIONTOOL_FAKE_STATE, else <data-dir>/fake_notion.json."""
    if path:
        return Path(path)
    env = os.environ.get(STATE_ENV, '').strip()
    if env:
        return Path(env)
    return Path(data_dir if data_dir else Path.cwd() / 'data') / STATE_FILE


def answer_for(title, prompt, turn):
    """Deterministic reply: same chat title, prompt and turn number give the same text."""
    digest = hashlib.sha256(str(prompt).encode('utf-8')).hexdigest()[:8]
    first = (str(prompt).strip().splitlines() or [''])[0][:120]
    return ('Simulated answer %d from %s (request %s).\n'
            'Handled: %s\n'
            'Simulated Notion: no real account was used.\n'
            '%s') % (turn, title, digest, first, RESULT_LINE)


def _entity(kind, ident, sequence, at, text=None, **extra):
    entity = dict(kind=kind, id=ident, sequence=sequence, created_at=_ms(at), **extra)
    if text is not None:
        entity['content'] = [{'type': 'text', 'content': text}]
    return entity


def seed_state(now=None):
    """Fresh simulated world: 2 accounts x (Main, 3 Coworkers, 2 private chats), one finished turn each."""
    now = time.time() if now is None else now
    state = dict(version=STATE_VERSION, created_at=now, accounts={}, threads={}, messages={}, faults={}, calls=[],
                 events=[])
    for key, email, name, space_name, ai in ACCOUNTS:
        space_id = fake_uuid('space', key)
        account = dict(key=key, email=email, name=name, user_id=fake_uuid('user', key), space_id=space_id,
                       space_view_id=fake_uuid('space_view', key), space_name=space_name, ai_enabled=ai,
                       thread_order=[])
        state['accounts'][key] = account
        for index, (kind, title) in enumerate(CHATS):
            tid = fake_uuid('thread', key, index)
            created = now - 3600 + index
            step = fake_uuid('step', key, index, 0)
            prompt = 'Hello %s' % title
            entries = [_entity('user_message', step, 1, created, prompt, client_message_id=step),
                       _entity('assistant_message', fake_uuid('reply', key, index, 0), 2, created + 1,
                               answer_for(title, prompt, 1)),
                       _entity('turn_completed', fake_uuid('done', key, index, 0), 3, created + 1)]
            state['messages'][step] = dict(id=step, parent_id=tid, space_id=space_id, alive=True,
                                           step={'id': step, 'type': 'user', 'value': [[prompt]]})
            state['threads'][tid] = dict(id=tid, account=key, space_id=space_id, kind=kind, title=title, alive=True,
                                         deleted_at=None, created_at=created, sequence=3, session_status='idle',
                                         turns=1, entries=entries, messages=[step],
                                         latest_user_or_trigger_time=_ms(created),
                                         completed_time=_ms(created + 1), data={})
            account['thread_order'].append(tid)
    return state


class FakeState:
    """The simulated Notion world in one JSON file; every access re-reads it (restart-safe)."""

    def __init__(self, path=None, data_dir=None, clock=time.time):
        self.path = resolve_state_path(path, data_dir)
        self.clock = clock
        self.lock = _lock_for(self.path)

    def load(self):
        with self.lock:
            if self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding='utf-8'))
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get('version') == STATE_VERSION:
                    for table in ('accounts', 'threads', 'messages', 'faults'):
                        data.setdefault(table, {})
                    data.setdefault('calls', [])
                    data.setdefault('events', [])
                    return data
                with contextlib.suppress(OSError):  # keep an unreadable file for inspection
                    os.replace(str(self.path), str(self.path) + '.corrupt')
            data = seed_state(self.clock())
            self._write(data)
            return data

    def save(self, data):
        with self.lock:
            self._write(data)

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name('%s.%d.%d.tmp' % (self.path.name, os.getpid(), threading.get_ident()))
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding='utf-8')
        os.replace(str(tmp), str(self.path))

    @contextlib.contextmanager
    def edit(self):
        with self.lock:
            data = self.load()
            yield data
            self._write(data)

    def reset(self):
        with self.lock:
            data = seed_state(self.clock())
            self._write(data)
            return data

    # --- lookups --------------------------------------------------------------------------
    def account(self, key):
        return copy.deepcopy(self.load()['accounts'].get(key))

    def account_key_for(self, account_id='', email=''):
        accounts = self.load()['accounts']
        if account_id in accounts:
            return account_id
        email = str(email or '').strip().lower()
        for key, account in accounts.items():
            if email and account['email'].lower() == email:
                return key
        raise ValueError('Sign in to this Notion account first')

    def threads_of(self, key, kind=None):
        data = self.load()
        order = data['accounts'].get(key, {}).get('thread_order', [])
        return [copy.deepcopy(data['threads'][t]) for t in order
                if t in data['threads'] and (kind is None or data['threads'][t]['kind'] == kind)]

    def thread(self, thread_id):
        return copy.deepcopy(self.load()['threads'].get(thread_id))

    def calls(self, path=None):
        return [c for c in self.load()['calls'] if path is None or c.get('path') == path]

    def events(self, kind=None):
        return [e for e in self.load()['events'] if kind is None or e.get('kind') == kind]

    def faults(self, target):
        return copy.deepcopy(_faults(self.load(), target))

    def resolve_target(self, target):
        """Thread id, account key or 'account/title' (e.g. 'ready/Coworker 1') -> thread id or account key."""
        data = self.load()
        target = str(target or '').strip()
        if target in data['threads'] or target in data['accounts']:
            return target
        if '/' in target:
            key, title = target.split('/', 1)
            for tid in data['accounts'].get(key, {}).get('thread_order', []):
                thread = data['threads'].get(tid)
                if thread and thread['title'] == title:
                    return tid
        raise KeyError('fake: no simulated thread or account %r' % target)

    def summary(self, target=None):
        """What `show` prints: accounts, their chats (alive, listed, session, faults) and the last events."""
        data = self.load()
        now = self.clock()
        accounts = {}
        for key, account in data['accounts'].items():
            threads = []
            for tid in account['thread_order']:
                thread = data['threads'].get(tid)
                if thread is None or (target and target not in (key, tid)):
                    continue
                threads.append(dict(id=tid, title=thread['title'], kind=thread['kind'],
                                    alive=not _gone(data, thread, now), listed=_listed(data, thread, now),
                                    session=thread['session_status'], turns=thread.get('turns', 0),
                                    faults=copy.deepcopy(_faults(data, tid))))
            if target and not threads and target != key:
                continue
            accounts[key] = dict(email=account['email'], ai_enabled=account['ai_enabled'],
                                 faults=copy.deepcopy(_faults(data, key)), threads=threads)
        return dict(path=str(self.path), accounts=accounts, calls=len(data['calls']), events=data['events'][-20:])

    # --- faults ---------------------------------------------------------------------------
    def set_fault(self, thread_id, name, **params):
        """Set a fault on a thread id (account faults also on an account key); see the module header."""
        if name not in FAULTS:
            raise ValueError('unknown fault %r; known: %s' % (name, ', '.join(FAULTS)))
        with self.edit() as data:
            on_account = name in ACCOUNT_FAULTS and thread_id in data['accounts']
            if thread_id not in data['threads'] and not on_account:
                raise KeyError(thread_id)
            if name == 'deleted_delay':
                params.setdefault('seconds', DEFAULT_DELETED_DELAY)
                thread = data['threads'][thread_id]
                if thread.get('deleted_at') is None:
                    thread['deleted_at'] = self.clock()
                thread['alive'] = False
            elif name == 'rate_limit':
                params.setdefault('retry_after', DEFAULT_RETRY_AFTER)
            elif name == 'slow':
                params.setdefault('seconds', DEFAULT_SLOW_SECONDS)
            elif name == 'welcome_not_listed' and not on_account:
                params.setdefault('since', self.clock())
            data['faults'].setdefault(thread_id, {})[name] = params

    def clear_fault(self, thread_id, name=None):
        with self.edit() as data:
            faults = data['faults'].get(thread_id, {})
            if name is None:
                faults.clear()
            else:
                faults.pop(name, None)
            if not faults:
                data['faults'].pop(thread_id, None)


def _faults(data, thread_id):
    faults = data['faults'].get(thread_id)
    return faults if isinstance(faults, dict) else {}


def _param(faults, name):
    value = faults.get(name)
    return value if isinstance(value, dict) else {}


def _account_fault(data, key, name):
    """Account-level fault: set on the account key or on any chat of that account."""
    if name in _faults(data, key):
        return True
    return any(name in _faults(data, t) for t in data['accounts'].get(key, {}).get('thread_order', []))


def _take_rate_limit(data, thread_ids):
    """retry_after of the first rate-limited thread (counting down 'times'), else None."""
    for tid in thread_ids:
        faults = _faults(data, tid)
        if 'rate_limit' not in faults:
            continue
        params = _param(faults, 'rate_limit')
        retry = params.get('retry_after', DEFAULT_RETRY_AFTER)
        if params.get('times') is not None:
            left = int(params['times']) - 1
            if left <= 0:
                faults.pop('rate_limit', None)
                if not faults:
                    data['faults'].pop(tid, None)
            else:
                faults['rate_limit'] = dict(params, times=left)
        return retry
    return None


def _gone(data, thread, now):
    """True when the thread is deleted or missing for reads and sends."""
    if thread is None:
        return True
    faults = _faults(data, thread['id'])
    return (not thread.get('alive', True)) or 'missing' in faults or 'deleted_delay' in faults


def _listed(data, thread, now):
    faults = _faults(data, thread['id'])
    if 'missing' in faults:
        return False
    if 'deleted_delay' in faults:
        params = faults['deleted_delay'] if isinstance(faults['deleted_delay'], dict) else {}
        deleted_at = params.get('deleted_at') or thread.get('deleted_at')
        if deleted_at is None:
            deleted_at = thread['deleted_at'] = now  # hand-written fault: the delay starts at first read
        return now - deleted_at < float(params.get('seconds', DEFAULT_DELETED_DELAY))
    if 'welcome_not_listed' in faults and thread.get('alive', True):
        if not isinstance(faults['welcome_not_listed'], dict):
            faults['welcome_not_listed'] = {}
        params = faults['welcome_not_listed']
        if params.get('since') is None:
            params['since'] = now  # hand-written fault: the delay starts at first read
        seconds = params.get('seconds')
        if seconds is None or now - float(params['since']) < float(seconds):
            return False
    return bool(thread.get('alive', True))


def _thread_record(data, thread):
    """syncRecordValues 'thread' value: the fields notion.py reads (alive, title, messages, busy times)."""
    outcome = {'completed_time': thread.get('completed_time') or 0}
    alive = not _gone(data, thread, 0)
    latest = thread.get('latest_user_or_trigger_time') or 0
    if 'busy' in _faults(data, thread['id']):
        latest = max(int(latest), int(outcome['completed_time']) + 1)  # a turn is running
    return dict(id=thread['id'], space_id=thread['space_id'], alive=alive, messages=list(thread.get('messages', [])),
                created_time=_ms(thread['created_at']), latest_user_or_trigger_time=latest,
                last_turn_outcome=outcome,
                data=dict(thread.get('data') or {}, title=thread['title'], last_turn_outcome=outcome))


def _wrap(value):
    return {'value': {'value': value, 'role': 'editor'}}


def _request_thread_ids(body):
    ids = [str(body.get('threadId'))] if body.get('threadId') else []
    for request in body.get('requests') or []:
        pointer = (request or {}).get('pointer') or {}
        if pointer.get('table') == 'thread' and pointer.get('id'):
            ids.append(str(pointer['id']))
    return ids


class FakeTransport:
    """Answers the Notion endpoints notion.py uses from FakeState; never opens a socket."""
    simulated = True

    def __init__(self, state, account_key, retry_after=30, keepalive=0.05):
        self.state = state
        self.account_key = account_key
        self._base = FAKE_BASE
        self.retry_after = retry_after
        self.model_catalog = {}
        self.keepalive = keepalive
        self.closed = False
        self.ready_process = None
        self.event_sink = None  # optional callable(kind, **fields), e.g. Store.event

    def close(self):
        self.closed = True

    @property
    def browser(self):
        raise RuntimeError('fake: the Edge browser is not available in simulation')

    # --- helpers --------------------------------------------------------------------------
    def _log(self, data, path, body):
        body = body if isinstance(body, dict) else {}
        data['calls'].append(dict(path=path, account=self.account_key, thread_id=body.get('threadId', ''),
                                  step_id=body.get('submittedUserStepId', ''), at=self.state.clock()))
        del data['calls'][:-CALL_LOG_KEEP]

    def _note_unhandled(self, data, path):
        data.setdefault('events', []).append(dict(kind=UNHANDLED_EVENT, path=str(path), account=self.account_key,
                                                  at=self.state.clock()))
        del data['events'][:-EVENT_LOG_KEEP]

    def _emit_unhandled(self, path):
        sink = self.event_sink
        if callable(sink):
            with contextlib.suppress(Exception):
                sink(UNHANDLED_EVENT, message='fake: unhandled endpoint %s' % path, path=str(path),
                     account_id=self.account_key, source='fake_notion')

    def _account(self, data):
        account = data['accounts'].get(self.account_key)
        if account is None:
            raise ValueError('fake: unknown account %s' % self.account_key)
        return account

    @staticmethod
    def _ok(payload):
        return 200, json.dumps(payload)

    @staticmethod
    def _error(status, message, name='ValidationError'):
        return status, json.dumps({'errorId': fake_uuid('error', status, message), 'name': name, 'message': message})

    # --- request ----------------------------------------------------------------------------
    def request(self, path, body, headers=None, timeout=45, ndjson=False):
        body = body if isinstance(body, dict) else {}
        unhandled = False
        with self.state.edit() as data:
            self._log(data, path, body)
            account = self._account(data)
            retry = _take_rate_limit(data, _request_thread_ids(body))
            handler = getattr(self, '_ep_' + str(path), None)
            if retry is not None:
                self.retry_after = retry
                result = self._error(429, 'fake: rate limited, retry after %s s' % retry, 'RateLimited')
            elif handler is None:
                unhandled = True
                self._note_unhandled(data, path)
                result = self._error(400, 'fake: unhandled endpoint %s' % path)
            else:
                result = handler(data, account, body)
        if unhandled:
            self._emit_unhandled(path)
        return result

    def _ep_loadUserContent(self, data, account, body):
        ai = bool(account['ai_enabled'])
        settings = dict(enable_ai_feature=ai, disable_ai_feature=not ai, notion_admin_disabled_ai_feature=False)
        return self._ok({'recordMap': {
            'notion_user': {account['user_id']: _wrap(dict(id=account['user_id'], email=account['email'],
                                                           name=account['name'], onboarding_completed=True))},
            'space': {account['space_id']: _wrap(dict(id=account['space_id'], name=account['space_name'], alive=True,
                                                      settings=settings, plan_type='business'))},
            'space_view': {account['space_view_id']: _wrap(dict(id=account['space_view_id'], space_id=account['space_id'],
                                                                alive=True, notion_user_id=account['user_id']))},
        }})

    def _ep_getAvailableModels(self, data, account, body):
        from .model_routing import MODELS
        ids = {short: model_id for model_id, short in MODEL_NAMES.items()}
        models = [dict(model=ids.get(short, 'fake-' + short), modelMessage=label, modelProvider='anthropic',
                       isDisabled=False, modelConfiguration=dict(supportedReasoningEfforts=['low', 'medium', 'high'],
                                                                 defaultReasoningEffort='medium'))
                  for short, label in MODELS.items()]
        return self._ok({'models': models})

    def _ep_getInferenceTranscriptsForUser(self, data, account, body):
        # Live Notion removed this endpoint (v23.13.20261017); notion.threads() then reads the space record.
        return 404, 'Cannot POST /api/v3/getInferenceTranscriptsForUser'

    def _ep_syncRecordValuesSpaceInitial(self, data, account, body):
        now = self.state.clock()
        record_map = {}
        for request in body.get('requests') or []:
            pointer = (request or {}).get('pointer') or {}
            table, ident = pointer.get('table'), pointer.get('id')
            if table == 'space' and ident == account['space_id']:
                if _account_fault(data, self.account_key, 'chat_list_unavailable'):
                    continue  # no space record: notion.threads() raises ChatListUnavailable
                listed = [t for t in account['thread_order'] if t in data['threads']
                          and _listed(data, data['threads'][t], now)]
                record_map.setdefault('space', {})[ident] = _wrap(dict(
                    id=ident, name=account['space_name'], alive=True, data={'thread_ids': listed}))
            elif table == 'thread':
                thread = data['threads'].get(ident)
                if thread is not None and thread['account'] == self.account_key:
                    record_map.setdefault('thread', {})[ident] = _wrap(_thread_record(data, thread))
            elif table == 'thread_message':
                message = data['messages'].get(ident)
                thread = data['threads'].get((message or {}).get('parent_id'))
                if message and thread and thread['account'] == self.account_key:
                    record_map.setdefault('thread_message', {})[ident] = _wrap(copy.deepcopy(message))
        return self._ok({'recordMap': record_map})

    def _ep_getThreadTranscript(self, data, account, body):
        tid = body.get('threadId')
        thread = data['threads'].get(tid)
        if thread is None or thread['account'] != self.account_key or _gone(data, thread, self.state.clock()):
            return self._error(404, 'Thread not found', 'NotFound')
        limit = max(1, int(body.get('limit') or 100))
        too_large = _faults(data, tid).get('too_large')
        if isinstance(too_large, dict) and limit > int(too_large.get('max_limit', 0)):
            return self._error(500, 'NotionStreamError: Edge request interrupted (Error: Response too large)',
                               'ResponseTooLarge')
        window = thread['entries'][-limit:]
        status = 'running' if 'busy' in _faults(data, tid) else thread['session_status']
        result = {'patches': [{'op': 'put', 'entity': copy.deepcopy(e)} for e in window],
                  'session': {'status': status, 'sequence': thread['sequence']}}
        if status not in ('idle', 'completed'):
            result['session_pending'] = True
        return self._ok(result)

    def _ep_saveTransactionsFanout(self, data, account, body):
        for transaction in body.get('transactions') or []:
            for op in (transaction or {}).get('operations') or []:
                pointer = op.get('pointer') or {}
                if pointer.get('table') != 'thread':
                    return self._error(400, 'fake: unhandled transaction table %s' % pointer.get('table'))
                thread = data['threads'].get(pointer.get('id'))
                if thread is None or thread['account'] != self.account_key:
                    return self._error(400, 'fake: unknown thread %s' % pointer.get('id'))
                path, command, args = list(op.get('path') or []), op.get('command'), op.get('args')
                if path == ['data', 'title'] and command == 'set':
                    thread['title'] = str(args)
                elif path == [] and command == 'update' and isinstance(args, dict):
                    if args.get('alive') is False and thread.get('alive', True):
                        thread['deleted_at'] = self.state.clock()
                    if 'alive' in args:
                        thread['alive'] = bool(args['alive'])
                elif len(path) == 2 and path[0] == 'data' and command == 'set':
                    thread.setdefault('data', {})[path[1]] = args
                else:
                    return self._error(400, 'fake: unhandled thread operation %s %s' % (command, path))
        return self._ok({})

    def _ep_recordCookieConsent(self, data, account, body):
        return self._ok({})

    def _ep_completeCurrentUserOnboarding(self, data, account, body):
        return self._ok({})

    def _ep_createSpace(self, data, account, body):
        return self._ok({'spaceId': account['space_id']})

    # --- stream -----------------------------------------------------------------------------
    def stream(self, path, body, headers=None, timeout=60, cancel=None):
        body = body if isinstance(body, dict) else {}
        if path != 'runInferenceTranscript':
            with self.state.edit() as data:
                self._log(data, path, body)
                self._note_unhandled(data, path)
            self._emit_unhandled(path)
            raise NotionHttpError(400, 'fake: unhandled endpoint %s' % path)
        return self._run(body, float(timeout or 60), cancel)

    def _stream_fault(self, body):
        """rate_limit / busy / lost_stream refuse the turn before Notion commits anything (the call is logged)."""
        tid = str(body.get('threadId') or '')
        with self.state.edit() as data:
            thread = data['threads'].get(tid)
            if thread is None or thread['account'] != self.account_key:
                return None
            faults = _faults(data, tid)
            retry = _take_rate_limit(data, [tid])
            if retry is not None:
                self.retry_after = retry
                failure = NotionHttpError(429, 'fake: rate limited, retry after %s s' % retry, retry_after=retry)
            elif 'busy' in faults:
                failure = NotionHttpError(409, 'fake: another turn is still running in this chat')
            elif 'lost_stream' in faults:
                failure = OSError('fake: stream lost before Notion committed the turn')
            else:
                return None
            self._log(data, 'runInferenceTranscript', body)
            return failure

    def _commit_user(self, body):
        """Commit the user step (or raise NotionHttpError); returns (thread snapshot, prompt, faults, turn)."""
        now = self.state.clock()
        with self.state.edit() as data:
            self._log(data, 'runInferenceTranscript', body)
            account = self._account(data)
            if not account['ai_enabled']:
                raise NotionHttpError(403)
            tid = str(body.get('threadId') or '')
            thread = data['threads'].get(tid)
            if thread is None and body.get('createThread') and tid:
                thread = dict(id=tid, account=self.account_key, space_id=account['space_id'], kind='created',
                              title=WELCOME_TITLE, alive=True, deleted_at=None, created_at=now, sequence=0,
                              session_status='idle', turns=0, entries=[], messages=[],
                              latest_user_or_trigger_time=0, completed_time=0, data={})
                data['threads'][tid] = thread
                account['thread_order'].append(tid)
                inherited = _faults(data, self.account_key).get('welcome_not_listed')
                if inherited is not None:
                    params = dict(inherited if isinstance(inherited, dict) else {}, since=now)
                    data['faults'].setdefault(tid, {})['welcome_not_listed'] = params
            if thread is None or thread['account'] != self.account_key or _gone(data, thread, now):
                raise NotionHttpError(404)
            steps = [s for s in body.get('transcript') or [] if isinstance(s, dict)]
            users = [s for s in steps if s.get('type') == 'user']
            step_id = str(body.get('submittedUserStepId') or (users[-1].get('id') if users else '') or uuid.uuid4())
            user = next((s for s in users if s.get('id') == step_id), users[-1] if users else {'value': [['']]})
            prompt = text_content(user.get('value'))
            for step in steps:
                keep = step.get('type') in ('user', 'updated-config') or (step.get('type') == 'config' and not thread['messages'])
                if not keep:
                    continue
                ident = step_id if step is user else str(step.get('id') or fake_uuid('step', tid, thread['sequence'], step.get('type')))
                data['messages'][ident] = dict(id=ident, parent_id=tid, space_id=thread['space_id'], alive=True,
                                               step=copy.deepcopy(dict(step, id=ident)))
                if ident not in thread['messages']:
                    thread['messages'].append(ident)
            thread['sequence'] += 1
            thread['entries'].append(_entity('user_message', step_id, thread['sequence'], now, prompt,
                                             client_message_id=step_id))
            thread['session_status'] = 'running'
            thread['latest_user_or_trigger_time'] = _ms(now)
            thread['turns'] = int(thread.get('turns', 0)) + 1
            return copy.deepcopy(thread), prompt, copy.deepcopy(_faults(data, tid)), thread['turns']

    def _commit_reply(self, tid, text, completed):
        now = self.state.clock()
        with self.state.edit() as data:
            thread = data['threads'].get(tid)
            if thread is None:
                return
            if text:
                thread['sequence'] += 1
                thread['entries'].append(_entity('assistant_message', fake_uuid('reply', tid, thread['sequence']),
                                                 thread['sequence'], now, text))
            if completed:
                thread['sequence'] += 1
                thread['entries'].append(_entity('turn_completed', fake_uuid('done', tid, thread['sequence']),
                                                 thread['sequence'], now))
            if not completed:
                return  # an interrupted turn: Notion keeps the session 'running' and sets no completed_time
            thread['session_status'] = 'idle'
            thread['completed_time'] = max(_ms(now), int(thread.get('latest_user_or_trigger_time') or 0))

    def _commit_error(self, tid, text):
        """error_step: Notion ends the turn with an error entry and no reply text."""
        now = self.state.clock()
        with self.state.edit() as data:
            thread = data['threads'].get(tid)
            if thread is None:
                return
            thread['sequence'] += 1
            thread['entries'].append(_entity('agent_error', fake_uuid('error', tid, thread['sequence']),
                                             thread['sequence'], now, error={'message': text}))
            thread['sequence'] += 1
            thread['entries'].append(_entity('turn_completed', fake_uuid('done', tid, thread['sequence']),
                                             thread['sequence'], now))
            thread['session_status'] = 'idle'
            thread['completed_time'] = max(_ms(now), int(thread.get('latest_user_or_trigger_time') or 0))

    def _run(self, body, timeout, cancel):
        failure = self._stream_fault(body)
        if failure is not None:
            raise failure
        thread, prompt, faults, turn = self._commit_user(body)
        tid = thread['id']
        answer = answer_for(thread['title'], prompt, turn)
        inference = fake_uuid('inference', tid, thread['sequence'])
        if 'empty_stream' in faults:
            # Notion commits the user step, the full reply and turn_completed, but the stream dies before the
            # first patch-start line: the adapter sees an empty stream (unknown / REMOTE_UNKNOWN) and reconciles.
            self._commit_reply(tid, answer, completed=True)
            raise OSError('fake empty stream')
        yield json.dumps({'type': 'patch-start', 'data': {'s': [{'id': inference, 'type': 'agent-inference', 'value': []}]}})

        def chunk(text):
            return json.dumps({'type': 'patch', 'v': [{'o': 'a', 'p': '/s/0/value/-',
                                                       'v': {'type': 'text', 'content': text}}]})
        if 'hang' in faults:
            params = faults['hang'] if isinstance(faults['hang'], dict) else {}
            limit = min(timeout, float(params.get('seconds', timeout)))
            started = time.monotonic()
            while not (cancel is not None and cancel.is_set()):
                if time.monotonic() - started >= limit:
                    raise TimeoutError('fake: the send did not finish (hang)')
                yield ''  # keep-alive only; the turn stays running
                time.sleep(self.keepalive)
            return
        lines = answer.splitlines(True)
        if 'interrupted' in faults:
            params = faults['interrupted'] if isinstance(faults['interrupted'], dict) else {}
            partial = lines[:max(1, len(lines) // 2)]
            for piece in partial:
                yield chunk(piece)
            self._commit_reply(tid, ''.join(partial).rstrip('\n'), completed=False)
            if params.get('cut', True):
                raise OSError('fake: stream interrupted mid-answer')
            return
        if 'error_step' in faults:
            text = str(_param(faults, 'error_step').get('message') or ERROR_STEP_TEXT)
            yield json.dumps({'type': 'patch', 'v': [{'o': 'a', 'p': '/s/-', 'v': {
                'id': fake_uuid('error-step', tid, thread['sequence']), 'type': 'error', 'message': text}}]})
            self._commit_error(tid, text)
            return
        if 'slow' in faults:
            seconds = float(_param(faults, 'slow').get('seconds', DEFAULT_SLOW_SECONDS))
            started = time.monotonic()
            for index, piece in enumerate(lines):
                due = started + seconds * (index + 1) / max(1, len(lines))
                while time.monotonic() < due:
                    if cancel is not None and cancel.is_set():
                        self._commit_reply(tid, answer, completed=True)  # Notion finishes on its side
                        return
                    if time.monotonic() - started >= timeout:
                        self._commit_reply(tid, answer, completed=True)
                        raise TimeoutError('fake: the slow reply did not finish within the call timeout')
                    yield ''  # keep-alive between the pieces
                    time.sleep(max(0.0, min(self.keepalive, due - time.monotonic())))
                yield chunk(piece)
            self._commit_reply(tid, answer, completed=True)
            return
        for piece in lines:
            yield chunk(piece)
        self._commit_reply(tid, answer, completed=True)


class FakeClient(NotionClient):
    """NotionClient on a FakeTransport: same methods and return shapes as the real client.

    headers(), call(), identity(), model(), threads() and transcript() are the real NotionClient
    methods, so status mapping (429 RateLimited, 404 ThreadMissing, other -> ValueError) and the
    'Response too large' retry behave as in the real app.
    """
    simulated = True

    def __init__(self, account='ready', identity=None, state=None, state_path=None, data_dir=None,
                 transport=None, clock=time.time):
        self.state = state or FakeState(state_path, data_dir, clock)
        self.account_key = account
        super().__init__(transport or FakeTransport(self.state, account), identity)

    # Same requests as NotionAdapter.transaction / rename_thread / delete_thread / thread_records.
    def transaction(self, action, operations):
        return self.call('saveTransactionsFanout', {'requestId': str(uuid.uuid4()), 'transactions': [{
            'id': str(uuid.uuid4()), 'spaceId': self.info['space_id'],
            'debug': {'userAction': action, 'clientCommitTimeMs': int(time.time() * 1000)}, 'operations': operations}]})

    def rename_thread(self, tid, title):
        self.transaction('agentThreads.rename', [{'pointer': {'table': 'thread', 'id': tid, 'spaceId': self.info['space_id']},
                                                  'path': ['data', 'title'], 'command': 'set', 'args': title}])

    def thread_records(self, ids):
        from .notion import unwrap
        result = self.call('syncRecordValuesSpaceInitial', {'requests': [
            {'pointer': {'table': 'thread', 'id': t, 'spaceId': self.info['space_id']}, 'version': -1} for t in ids]})
        return {k: unwrap(v) for k, v in result.get('recordMap', {}).get('thread', {}).items()}

    def delete_thread(self, tid):
        self.transaction('agentThreads.delete', [{'pointer': {'table': 'thread', 'id': tid, 'spaceId': self.info['space_id']},
                                                  'path': [], 'command': 'update', 'args': {'alive': False}}])
        if (self.thread_records([tid]).get(tid) or {}).get('alive') is not False:
            raise ValueError('Notion did not confirm the deletion of chat ' + tid)
        return True

    def send(self, thread_id, prompt, step_id=None, create=False, timeout=60, cancel=None):
        """One runInferenceTranscript turn like NotionAdapter.run(); returns (stream text, step_id)."""
        from .notion import apply_patch, stream_text
        step_id = step_id or str(uuid.uuid4())
        body = {'spaceId': self.info['space_id'], 'threadId': thread_id, 'submittedUserStepId': step_id,
                'createThread': bool(create), 'generateTitle': bool(create),
                'transcript': [{'id': step_id, 'type': 'user', 'value': [[prompt]]}]}
        steps = []
        for line in self.transport.stream('runInferenceTranscript', body, self.headers(), timeout=timeout, cancel=cancel):
            if not str(line).strip():
                continue
            event = json.loads(line)
            if event.get('type') in ('patch-start', 'patch-sync'):
                steps = (event.get('data') or {}).get('s') or steps
            elif event.get('type') == 'patch':
                steps = apply_patch(steps, event.get('v'))
        return stream_text(steps), step_id

    def wait(self, thread_id, step_id, timeout=10, interval=0.05, limit=30):
        """Poll the committed transcript until the own turn finished; returns notion.turn_state(...)."""
        from .notion import turn_state
        deadline = time.monotonic() + timeout
        while True:
            state = turn_state(self.transcript(thread_id, limit=limit), {'step_id': step_id, 'before_seq': 0})
            if state['finished'] or time.monotonic() >= deadline:
                return state
            time.sleep(interval)


def install(adapter, state=None):
    """Make adapter.client(account_id, anonymous=False) return FakeClient objects; nothing else is patched.

    The store account is matched to a simulated account by its id ('ready'/'noai') or its email. One
    FakeTransport per account is kept, like the adapter's own transport cache (model catalog cache).
    Unhandled endpoints are also reported to store.event when the store has one. Returns the FakeState.
    """
    store = getattr(adapter, 'store', None)
    state = state or FakeState(data_dir=getattr(store, 'directory', None))
    sink = getattr(store, 'event', None)
    transports = {}
    lock = threading.Lock()

    def client(account_id, anonymous=False):
        account = {}
        if store is not None:
            try:
                account = store.get('accounts', account_id, {}) or {}
            except Exception:
                account = {}
        key = state.account_key_for(account_id, account.get('email', ''))
        with lock:
            transport = transports.get(key)
            if transport is None:
                transport = transports[key] = FakeTransport(state, key)
                if callable(sink):
                    transport.event_sink = sink
        return FakeClient(key, account.get('identity'), state=state, transport=transport)

    adapter.client = client
    return state


class FakeNotionAdapter(NotionAdapter):
    """The real NotionAdapter; only the way it reaches Notion is simulated.

    Overrides exactly client(), edge_transport(), browser_login() and adopt_session_cookies(); the
    real NetworkBudget, run/recover, chat pool and refresh code stay in use. Refuses to start unless
    NOTIONTOOL_TEST_INSTANCE=1 (the test app) or force=True (unit tests).
    """
    simulated = True
    refresh_on_login = True

    def __init__(self, store, state=None, force=False):
        if not force and os.environ.get(TEST_INSTANCE_ENV) != '1':
            raise SimulationRefused('FakeNotionAdapter only runs in the test app (%s=1)' % TEST_INSTANCE_ENV)
        self.fake_state = state or FakeState(data_dir=getattr(store, 'directory', None))
        self.fake_transports = {}
        self.fake_lock = threading.Lock()
        super().__init__(store)

    def _fake_key(self, account_id):
        try:
            account = self.store.get('accounts', account_id, {}) or {}
        except Exception:
            account = {}
        return self.fake_state.account_key_for(account_id, account.get('email', '')), account

    def _fake_transport(self, key):
        with self.fake_lock:
            transport = self.fake_transports.get(key)
            if transport is None:
                transport = self.fake_transports[key] = FakeTransport(self.fake_state, key)
                transport.event_sink = self.store.event
            return transport

    def client(self, account_id, anonymous=False):
        key, account = self._fake_key(account_id)
        return FakeClient(key, account.get('identity'), state=self.fake_state, transport=self._fake_transport(key))

    def edge_transport(self, account_id):
        key, _ = self._fake_key(account_id)
        transport = self._fake_transport(key)
        self.edge_transports[account_id] = transport
        return transport

    def browser_login(self, timeout=1200):
        """No Edge window: signs in the simulated 'ready' account."""
        return self.adopt_session_cookies([{'name': 'token_v2', 'value': 'fake:ready', 'domain': '.fake.notion.invalid'}],
                                          source='browser_login')

    def adopt_session_cookies(self, cookies, source='browser_login', select=False):
        """token_v2 'fake:<account key>' (default 'ready') marks that simulated account as signed in."""
        token = next((str(c.get('value')) for c in cookies or []
                      if isinstance(c, dict) and c.get('name') == 'token_v2' and c.get('value')), '')
        if not token:
            raise ValueError('No Notion sign-in was detected (token_v2 cookie missing)')
        key = token.split(':', 1)[1] if token.startswith('fake:') else 'ready'
        if self.fake_state.account(key) is None:
            raise ValueError('fake: unknown simulated account %s' % key)
        info = FakeClient(key, state=self.fake_state, transport=self._fake_transport(key)).identity()
        email = info['email'].lower()
        accounts = self.store.all('accounts')
        existing = next((a for a in accounts if str(a.get('email', '')).lower() == email), None)
        account = existing or self.create_account(email)
        account = dict(self.store.get('accounts', account['id'], account) or account, identity=info,
                       state='signed_in', simulated=True, login_source=source)
        self.store.put('accounts', account)
        self.clear_account_pause(account['id'], verified=True)
        if self.refresh_on_login:
            try:
                self.refresh(account['id'])
            except Exception as exc:
                self.store.event('fake_login_refresh_failed', message=str(exc)[:300], account_id=account['id'])
        if select or not accounts:
            from .harness_account import select_account
            with contextlib.suppress(Exception):
                select_account(self.store, account['id'])
        account = self.store.get('accounts', account['id'], account) or account
        return {'account_id': account['id'], 'email': account['email'], 'state': account.get('state', '')}


def seed_scenario(store, state=None, name='default'):
    """Write the simulated accounts, chat workers and Main chats as store records (idempotent).

    Same record shapes as the real refresh()/create_account() (accounts id = account key, workers
    '<key>:<thread id>', chats 'c_fake_<key>'). Returns the counts of records added now.
    """
    if name != 'default':
        raise ValueError('unknown scenario %r; known: default' % name)
    state = state or FakeState(data_dir=getattr(store, 'directory', None))
    now = time.time()
    added = dict(accounts=0, workers=0, chats=0)
    for slot, (key, email, _name, _space, _ai) in enumerate(ACCOUNTS, 1):
        if store.get('accounts', key) is None:
            client = FakeClient(key, state=state)
            info = client.identity()
            access = NotionAdapter.workspace_ai_access(info)
            ready = access.get('state') == 'enabled'
            store.put('accounts', dict(id=key, slot=slot, email=email, label='Slot %d' % slot,
                                       state='ready' if ready else 'ai_disabled', identity=info, ai_access=access,
                                       model=client.model() if ready else '', bridge_connected=False,
                                       cooldown_until=0, simulated=True))
            added['accounts'] += 1
        for thread in state.threads_of(key):
            wid = key + ':' + thread['id']
            purpose = KIND_PURPOSE.get(thread['kind'], 'private')
            if store.get('workers', wid) is None:
                record = dict(id=wid, account_id=key, thread_id=thread['id'], purpose=purpose,
                              enabled=purpose in ('main', 'worker'), state='ready', lease='',
                              last_used=0, label=thread['title'], welcome=purpose in ('main', 'worker'),
                              simulated=True)
                if purpose == 'worker':
                    # Same fields the real team setup leaves on a joined Welcome Coworker (notion.py ensure_team).
                    record.update(join_state='joined', pool_admitted=True)
                store.put('workers', record)
                added['workers'] += 1
            chat_id = 'c_fake_' + key
            if purpose == 'main' and store.get('chats', chat_id) is None:
                store.put('chats', dict(id=chat_id, title=thread['title'], main_worker=wid, account_id=key,
                                        folder_id='', project_path='', created=now, main_number=1,
                                        state='active', predecessor=''))
                added['chats'] += 1
        # Team ledger in the shape the real team setup writes (notion.py ensure_team / apply_chat_pool):
        # main and worker1..workerN -> {'id': thread id, 'pending': False}. Private chats are never listed.
        account = store.get('accounts', key)
        current = account.get('team_threads') or {}
        ledger = dict(current)
        mains = state.threads_of(key, 'main')
        if mains and not (ledger.get('main') or {}).get('id'):
            ledger['main'] = {'id': mains[0]['id'], 'pending': False}
        for number, thread in enumerate(state.threads_of(key, 'coworker'), 1):
            role = 'worker%d' % number
            if not (ledger.get(role) or {}).get('id'):
                ledger[role] = {'id': thread['id'], 'pending': False}
        if ledger != current:
            account['team_threads'] = ledger
            store.put('accounts', account)
    store.raise_counter('main', 1)
    settings = store.get('settings', 'app', {}) or {}
    wanted = {}
    if 'harness_account_id' not in settings:
        wanted.update(harness_account_id='ready', coworker_target=3)
    if 'per_account' not in settings:
        wanted['per_account'] = 3  # team size 3: team setup finds all roles filled (default would be 10)
    if wanted:
        store.put('settings', dict(settings, id='app', **wanted))
    return dict(added, scenario=name, state=str(state.path))


def _parse_params(items):
    params = {}
    for item in items or []:
        if '=' not in item:
            raise ValueError('fault parameter %r must look like key=value' % item)
        key, value = item.split('=', 1)
        try:
            params[key] = json.loads(value)
        except ValueError:
            params[key] = value
    return params


def main(argv=None):
    """Command line for the simulated Notion state; prints JSON, returns 0 (or 2 on a bad target/fault)."""
    import argparse
    parser = argparse.ArgumentParser(prog='python -m notiontool.fake_notion',
                                     description='Inspect and change the simulated Notion of the test app.')
    parser.add_argument('--state', help='state file (default: env %s, else <data-dir>/%s)' % (STATE_ENV, STATE_FILE))
    parser.add_argument('--data-dir', help='test app data folder (holds notiontool.db and %s)' % STATE_FILE)
    sub = parser.add_subparsers(dest='command')
    sub.required = True
    sub.add_parser('reset', help='re-seed the simulated world (drops faults and new chats)')
    sub.add_parser('seed', help='write the simulated accounts and chats into the store of --data-dir')
    p = sub.add_parser('set-fault', help='set a fault: TARGET NAME [key=value ...]')
    p.add_argument('target')
    p.add_argument('name')
    p.add_argument('params', nargs='*')
    p = sub.add_parser('clear-fault', help='clear one fault or all faults of TARGET')
    p.add_argument('target')
    p.add_argument('name', nargs='?')
    p = sub.add_parser('show', help='accounts, chats, faults and the last events')
    p.add_argument('target', nargs='?')
    args = parser.parse_args(argv)
    state = FakeState(args.state, args.data_dir)
    try:
        if args.command == 'reset':
            state.reset()
            out = dict(reset=True, path=str(state.path))
        elif args.command == 'seed':
            if not args.data_dir:
                raise ValueError('seed needs --data-dir (the folder with notiontool.db)')
            from .store import Store
            store = Store(Path(args.data_dir))
            try:
                out = seed_scenario(store, state)
            finally:
                store.close()
        elif args.command == 'set-fault':
            target = state.resolve_target(args.target)
            state.set_fault(target, args.name, **_parse_params(args.params))
            out = dict(target=target, faults=state.faults(target))
        elif args.command == 'clear-fault':
            target = state.resolve_target(args.target)
            state.clear_fault(target, args.name)
            out = dict(target=target, faults=state.faults(target))
        else:
            out = state.summary(state.resolve_target(args.target) if args.target else None)
    except (KeyError, ValueError) as exc:
        print(json.dumps({'error': str(exc).strip('"\'')}), file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
