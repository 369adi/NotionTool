"""Simulated Notion for the test app: no network, no real accounts.

The fake sits at the transport layer, so the real NotionClient / NotionAdapter code
(call() status mapping, threads() fallback, transcript() 'Response too large' retry,
run()/recover() stream parsing, rename_thread, delete_thread) runs unchanged:

- FakeState     JSON state file (env NOTIONTOOL_FAKE_STATE, default <data-dir>/fake_notion.json),
                re-read on every call and written atomically, so it survives an app restart.
- FakeTransport request(path, body, headers=None, timeout=45) -> (status, text) and
                stream(path, body, headers=None, timeout=60, cancel=None) -> NDJSON lines,
                with _base, retry_after, model_catalog and close() like the real transports.
- FakeClient    a NotionClient on a FakeTransport: headers, call, identity, model, threads,
                transcript (inherited, same return shapes) plus helpers rename_thread,
                delete_thread, thread_records, send and wait.
- install(adapter) makes adapter.client(account_id, anonymous=False) return FakeClient
                objects. Nothing else is patched.

Seed: two accounts, 'ready' (Notion AI on) and 'noai' (Notion AI disabled), each with a
Main chat, three Coworker chats and two private chats. Answers are deterministic and end
with 'RESULT: DONE'.

Faults per thread, in state['faults'][thread_id] = {name: params} (set_fault() or by hand):
  too_large      {'max_limit': N}    getThreadTranscript with limit > N fails with 'Response too large'
  hang           {'seconds': S}      the send commits the user step, then only keep-alive lines until
                                     cancel is set or min(timeout, S) passes (then TimeoutError)
  interrupted    {'cut': True}       the reply stops mid-answer without the final line; the stream is
                                     cut (OSError) unless cut is False
  missing        {}                  HTTP 404 on transcript/send, not listed, record alive=False
  deleted_delay  {'seconds': K}      already deleted (404, alive=False) but still listed for K seconds
                                     after deleted_at
"""
import contextlib
import copy
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

from .notion import MODEL_NAMES, NotionClient, text_content
from .vendor.transport import NotionHttpError

FAKE_BASE = 'https://fake.notion.invalid'
STATE_ENV = 'NOTIONTOOL_FAKE_STATE'
STATE_FILE = 'fake_notion.json'
STATE_VERSION = 1
RESULT_LINE = 'RESULT: DONE'
FAULTS = ('too_large', 'hang', 'interrupted', 'missing', 'deleted_delay')
DEFAULT_DELETED_DELAY = 30
CALL_LOG_KEEP = 500
WELCOME_TITLE = 'Welcome to Notion'

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
    state = dict(version=STATE_VERSION, created_at=now, accounts={}, threads={}, messages={}, faults={}, calls=[])
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

    # --- faults ---------------------------------------------------------------------------
    def set_fault(self, thread_id, name, **params):
        if name not in FAULTS:
            raise ValueError('unknown fault %r; known: %s' % (name, ', '.join(FAULTS)))
        with self.edit() as data:
            if thread_id not in data['threads']:
                raise KeyError(thread_id)
            if name == 'deleted_delay':
                params.setdefault('seconds', DEFAULT_DELETED_DELAY)
                thread = data['threads'][thread_id]
                if thread.get('deleted_at') is None:
                    thread['deleted_at'] = self.clock()
                thread['alive'] = False
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
    return bool(thread.get('alive', True))


def _thread_record(data, thread):
    """syncRecordValues 'thread' value: the fields notion.py reads (alive, title, messages, busy times)."""
    outcome = {'completed_time': thread.get('completed_time') or 0}
    alive = not _gone(data, thread, 0)
    return dict(id=thread['id'], space_id=thread['space_id'], alive=alive, messages=list(thread.get('messages', [])),
                created_time=_ms(thread['created_at']), latest_user_or_trigger_time=thread.get('latest_user_or_trigger_time') or 0,
                last_turn_outcome=outcome,
                data=dict(thread.get('data') or {}, title=thread['title'], last_turn_outcome=outcome))


def _wrap(value):
    return {'value': {'value': value, 'role': 'editor'}}


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

    def close(self):
        self.closed = True

    # --- helpers --------------------------------------------------------------------------
    def _log(self, data, path, body):
        body = body if isinstance(body, dict) else {}
        data['calls'].append(dict(path=path, account=self.account_key, thread_id=body.get('threadId', ''),
                                  step_id=body.get('submittedUserStepId', ''), at=self.state.clock()))
        del data['calls'][:-CALL_LOG_KEEP]

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
        with self.state.edit() as data:
            self._log(data, path, body)
            account = self._account(data)
            handler = getattr(self, '_ep_' + str(path), None)
            if handler is None:
                return self._error(400, 'fake: unhandled endpoint %s' % path)
            return handler(data, account, body)

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
        result = {'patches': [{'op': 'put', 'entity': copy.deepcopy(e)} for e in window],
                  'session': {'status': thread['session_status'], 'sequence': thread['sequence']}}
        if thread['session_status'] not in ('idle', 'completed'):
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
            raise NotionHttpError(400)
        return self._run(body, float(timeout or 60), cancel)

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
            thread['session_status'] = 'idle'
            thread['completed_time'] = max(_ms(now), int(thread.get('latest_user_or_trigger_time') or 0))

    def _run(self, body, timeout, cancel):
        thread, prompt, faults, turn = self._commit_user(body)
        tid = thread['id']
        answer = answer_for(thread['title'], prompt, turn)
        inference = fake_uuid('inference', tid, thread['sequence'])
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
    Returns the FakeState in use.
    """
    store = getattr(adapter, 'store', None)
    state = state or FakeState(data_dir=getattr(store, 'directory', None))
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
        return FakeClient(key, account.get('identity'), state=state, transport=transport)

    adapter.client = client
    return state
