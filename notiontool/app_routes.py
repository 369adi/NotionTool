"""HTTP routes of the standalone NotionTool window (notiontool/web/app): /api/ui/app/*.

Same auth as every Host API route (server.py: X-NotionTool-Token of the window, Origin check). All Notion
traffic goes through notion_api.NotionApi (token_v2 session, Edge transport, network budget); no Notion page
is ever shown.

  GET  /api/ui/app/state              account, accounts, workspace, workspaces, Main, UI mode, jobs
  GET  /api/ui/app/chats[?fresh=1]    chats of the current workspace (cached CHATS_TTL s per account+space)
  GET  /api/ui/app/chat?id=&n=        history of one chat (+ live reply while it streams)
  GET  /api/ui/app/live?id=           live reply of a running send
  POST /api/ui/app/send    {thread_id?, text, model?}   empty thread_id starts a new chat
  POST /api/ui/app/space   {space_id, account_id?}      switch the workspace (the current one reloads)
  POST /api/ui/app/account {account_id}                 switch the account
  POST /api/ui/app/desk    {action, args}               desktop parts (settings.js, projects.js), DESK_ACTIONS
"""
import threading
import time

from .harness_account import selected_account
from .notion_api import HISTORY_LIMIT, NotionApi, model_key, thread_key
from .store import redact

PREFIX = '/api/ui/app'
CHATS_TTL = 15.0
LIVE_KEEP_S = 900
MAX_TEXT = 100000
_RUNTIME_LOCK = threading.Lock()


def matches(path):
    return path == PREFIX or str(path or '').startswith(PREFIX + '/')


def _first(query, key, default=''):
    values = (query or {}).get(key) if isinstance(query, dict) else None
    if isinstance(values, (list, tuple)):
        return str(values[0]) if values else default
    return default if values is None else str(values)


def data_dir(app):
    return getattr(getattr(app, 'desktop', None), 'data_dir', None)


class Runtime:
    """Per-app state of the window: chat-list cache and the live replies of running sends."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.chat_cache = {}
        self.live = {}
        self._handlers = None

    def handlers(self):
        if self._handlers is None:
            from . import desktop_shell  # module attribute: wrappers (problems, restart_guard, templates) apply
            self._handlers = desktop_shell.desktop_handlers(self.app, data_dir(self.app))
        return self._handlers

    def desk(self, action, args):
        from .desktop_shell import DESK_ACTIONS
        if action not in DESK_ACTIONS:
            raise ValueError('Action not allowed')
        handler = self.handlers().get(action)
        if handler is None:
            raise ValueError('Action not available')
        return handler(args if isinstance(args, dict) else {})

    def clear(self):
        with self.lock:
            self.chat_cache.clear()

    def prune(self):
        old = time.time() - LIVE_KEEP_S
        for key in [k for k, v in self.live.items() if v['state'] != 'running' and v['finished'] < old]:
            self.live.pop(key, None)


def runtime(app):
    with _RUNTIME_LOCK:
        found = getattr(app, 'standalone_ui', None)
        if found is None:
            found = Runtime(app)
            app.standalone_ui = found
        return found


def current(app):
    ident = selected_account(app.store) or ''
    account = (app.store.get('accounts', ident) if ident else None) or {}
    return ident, account


def main_thread(account):
    entry = (account.get('team_threads') or {}).get('main') or {}
    return thread_key(entry.get('id') if isinstance(entry, dict) else entry)


def known_threads(store, ident, account):
    """{thread id: role} of the chats NotionTool manages for this account (Main, Coworkers, own chats)."""
    roles = {}
    for worker in store.all('workers'):
        if worker.get('account_id') != ident or worker.get('state') == 'missing':
            continue
        tid = thread_key(worker.get('thread_id') or worker.get('thread'))
        if tid:
            roles[tid] = 'coworker' if worker.get('purpose') == 'worker' else 'chat'
    main = main_thread(account)
    if main:
        roles[main] = 'main'
    return roles


def public_live(entry):
    if not entry:
        return None
    return {key: entry.get(key) for key in ('state', 'text', 'error', 'started', 'finished', 'created', 'prompt',
                                            'model', 'real_id')}


def state(app):
    rt = runtime(app)
    ident, account = current(app)
    info = account.get('identity') or {}
    space_id = info.get('space_id') or ''
    spaces = [dict(id=s.get('id'), name=s.get('name') or 'Workspace') for s in info.get('spaces') or [] if s.get('id')]
    try:
        accounts = (rt.desk('account_status', {}) or {}).get('accounts') or []
    except Exception as exc:  # noqa: BLE001 - the window still shows the selected account
        accounts, _ = [], exc
    from .desktop_shell import load_app_settings
    settings = load_app_settings(data_dir(app))
    since = time.time() - 600
    jobs = sorted((j for j in app.store.all('jobs') if j.get('account_id') == ident and
                   float(j.get('created') or j.get('started') or 0) > since),
                  key=lambda j: -float(j.get('created') or j.get('started') or 0))[:4]
    with rt.lock:
        running = [k for k, v in rt.live.items() if v['state'] == 'running' and v.get('account_id') == ident]
    return dict(account_id=ident, account_state=account.get('state', ''), email=account.get('email', ''),
                user_name=info.get('user_name') or account.get('label') or account.get('email', ''),
                space_id=space_id, space_name=next((s['name'] for s in spaces if s['id'] == space_id), ''),
                spaces=spaces, accounts=accounts, main_thread_id=main_thread(account),
                ui_mode=settings.get('app_ui_mode') or 'overlay', model=model_key(settings.get('app_default_model')),
                chat_filter=settings.get('app_chat_filter') or '', running=running,
                jobs=[dict(id=j.get('id'), title=j.get('title', ''), state=j.get('state', ''),
                           error=redact(str(j.get('error') or ''))[:300]) for j in jobs])


def chats(app, fresh=False):
    rt = runtime(app)
    ident, account = current(app)
    if not ident:
        return dict(account_id='', space_id='', main_thread_id='', chats=[])
    space = (account.get('identity') or {}).get('space_id') or ''
    key = (ident, space)
    with rt.lock:
        hit = rt.chat_cache.get(key)
    if hit and not fresh and time.time() - hit[0] < CHATS_TTL:
        payload = hit[1]
    else:
        roles = known_threads(app.store, ident, account)
        space, rows = NotionApi(app.adapter, ident).chats(known=list(roles)[:100])
        for row in rows:
            row['role'] = roles.get(row['id'], 'chat')
        rows.sort(key=lambda row: row['role'] != 'main')  # stable: Main pinned, rest newest first
        payload = dict(account_id=ident, space_id=space, main_thread_id=main_thread(account), chats=rows)
        with rt.lock:
            rt.chat_cache[(ident, space)] = (time.time(), payload)
    listed = {row['id'] for row in payload['chats']}
    with rt.lock:
        pending = [dict(id=k, title=(v.get('prompt') or 'New chat')[:80], updated=int(v['started'] * 1000),
                        unread=False, role='chat', pending=True)
                   for k, v in rt.live.items() if k not in listed and v.get('created') and
                   v.get('account_id') == ident and v.get('space_id') == space]
    with rt.lock:
        aliased = {v.get('real_id') for k, v in rt.live.items() if v.get('real_id') and v.get('real_id') != k}
    if aliased:  # the window's id stands for the real chat (_find_created): list that chat once
        payload = dict(payload, chats=[row for row in payload['chats'] if row['id'] not in aliased])
    if pending:  # a just-created chat shows at once (I56), before Notion lists it
        head = [row for row in payload['chats'] if row['role'] == 'main']
        payload = dict(payload, chats=head + pending + [row for row in payload['chats'] if row['role'] != 'main'])
    return payload


def chat(app, query):
    rt = runtime(app)
    tid = thread_key(_first(query, 'id'))
    if not tid:
        raise ValueError('Choose a chat.')
    try:
        limit = max(1, min(200, int(_first(query, 'n', str(HISTORY_LIMIT)) or HISTORY_LIMIT)))
    except ValueError:
        limit = HISTORY_LIMIT
    ident, _account = current(app)
    if not ident:
        raise ValueError('Sign in to an account first.')
    with rt.lock:
        entry = rt.live.get(tid)
        live = public_live(entry)
        real = (entry or {}).get('real_id') or ''
    try:
        # a createThread chat lives under the id Notion chose (see _find_created)
        data = NotionApi(app.adapter, ident).chat(real or tid, limit)
    except Exception:
        if not (live and live.get('created')):
            raise
        data = dict(messages=[], status='', pending=True)  # new chat: Notion has not stored it yet
    data.update(id=tid, live=live)
    return data


def live(app, query):
    rt = runtime(app)
    tid = thread_key(_first(query, 'id'))
    with rt.lock:
        return dict(id=tid, live=public_live(rt.live.get(tid)))


def _effort(settings):
    value = str(settings.get('app_effort_override') or '').strip().lower()
    return None if value in ('', 'auto', 'none', 'off') else value


def send(app, body):
    rt = runtime(app)
    text = str(body.get('text') or '').strip()
    if not text:
        raise ValueError('Write a message first.')
    if len(text) > MAX_TEXT:
        raise ValueError('The message is too long.')
    ident, account = current(app)
    if not ident:
        raise ValueError('Sign in to an account first.')
    space = (account.get('identity') or {}).get('space_id') or ''
    if not space:
        raise ValueError('Choose a workspace first.')
    tid = thread_key(body.get('thread_id'))
    phantom = ''
    with rt.lock:
        alias = (rt.live.get(tid) or {}).get('real_id') if tid else ''
    if alias:  # the window still holds the id it created; Notion stored the chat under alias
        phantom, tid = tid, alias
    create = not tid
    if create:
        tid = thread_key(app.adapter.new_id(space))
    else:
        check = getattr(app, 'chat_send_check', None)
        if callable(check):  # I50 age rule for every chat; refuses with ValueError (shown in the window)
            check({'thread_id': tid, 'send': True, 'account_id': ident})
    from .desktop_shell import load_app_settings
    settings = load_app_settings(data_dir(app))
    model = model_key(body.get('model') or settings.get('app_default_model'))
    with rt.lock:
        running = rt.live.get(tid)
        if running and running['state'] == 'running':
            raise ValueError('This chat is still answering. Wait for the reply.')
        rt.prune()
        entry = dict(state='running', text='', error='', started=time.time(), finished=0.0,
                     created=create or bool(phantom), prompt=text[:200], model=model, account_id=ident,
                     space_id=space)
        rt.live[tid] = entry
        if phantom:  # keep the window's id pointing at the real chat
            entry['real_id'] = tid
            rt.live[phantom] = entry
    threading.Thread(target=_run_send, args=(app, rt, entry, ident, tid, text, create, model, _effort(settings)),
                     daemon=True, name='app-send').start()
    return dict(ok=True, thread_id=phantom or tid, created=create, model=model)


def _run_send(app, rt, entry, ident, tid, text, create, model, effort):
    api = NotionApi(app.adapter, ident)

    def on_text(value):
        entry['text'] = value

    try:
        result = api.send(tid, text, create=create, model=model, effort=effort, on_text=on_text)
        entry['text'] = result['text'] or entry['text']
        entry['error'] = result['error']
        if create:  # before 'done', so the window loads the real chat at once
            _link_created(rt, entry, api, tid, text)
        entry['state'] = 'error' if result['error'] and not entry['text'].strip() else 'done'
    except Exception as exc:  # noqa: BLE001 - the window shows the error
        entry['error'] = redact(str(exc) or type(exc).__name__)[:400]
        entry['state'] = 'error'
    finally:
        entry['finished'] = time.time()
        rt.clear()
    try:
        app.store.event('app_chat_send', account_id=ident,
                        message='%s %s (%s)' % (entry['state'], tid[:8], 'new' if create else 'existing'))
    except Exception:  # noqa: BLE001 - logging must never break a send
        pass
    if entry['state'] == 'done':
        try:
            api.mark_seen(entry.get('real_id') or tid)
        except Exception:  # noqa: BLE001 - only the unread dot
            pass


def _find_created(api, tid, text, started, tries=3, pause=2.0):
    """Id of the chat Notion stored a createThread send under.

    Live 27 Oct 2026: the send went to our id, but the transcript was under a new id Notion chose
    (titled by generateTitle). Evidence, not a guess: a chat updated since the send whose first user
    message is exactly the prompt. '' when our own id holds the chat."""
    try:
        if api.chat(tid, 4).get('messages'):
            return ''
    except Exception:  # noqa: BLE001 - our id is unknown to Notion: search the list
        pass
    since, prompt = int(started * 1000) - 5000, text.strip()
    for attempt in range(tries):
        _space, rows = api.chats()
        for row in rows:
            cid = row.get('id')
            if not cid or cid == tid or int(row.get('updated') or 0) < since:
                continue
            try:
                messages = api.chat(cid, 6).get('messages') or []
            except Exception:  # noqa: BLE001 - check the next chat
                continue
            first = next((m for m in messages if m.get('role') == 'user'), None)
            if first and str(first.get('text') or '').strip() == prompt:
                return cid
        if attempt + 1 < tries:
            time.sleep(pause)
    return ''


def _link_created(rt, entry, api, tid, text):
    try:
        real = _find_created(api, tid, text, entry['started'])
    except Exception:  # noqa: BLE001 - the window then shows the pending chat
        real = ''
    if real:
        with rt.lock:
            entry['real_id'] = real
            rt.live[real] = entry


def _job(result):
    if isinstance(result, dict):
        inner = result.get('job') if isinstance(result.get('job'), dict) else result
        out = {key: inner.get(key) for key in ('id', 'title', 'state', 'error', 'ok', 'changed') if key in inner}
        jid = result.get('job_id') or inner.get('job_id')
        if jid and not out.get('id'):
            out['id'] = jid
        return out
    return result


def _newest_job(app, ident, since):
    jobs = [j for j in app.store.all('jobs') if j.get('account_id') == ident and
            float(j.get('created') or j.get('started') or 0) >= since]
    jobs.sort(key=lambda j: -float(j.get('created') or j.get('started') or 0))
    return _job(jobs[0]) if jobs else {}


def switch_space(app, body):
    rt = runtime(app)
    ident = str(body.get('account_id') or selected_account(app.store) or '')
    asked = time.time() - 2.0
    result = rt.desk('account_space', {'account_id': ident, 'space_id': str(body.get('space_id') or '')})
    rt.clear()
    job = _job(result)
    if not (isinstance(job, dict) and job.get('id')):  # the handler queued a job without returning it
        job = _newest_job(app, ident, asked) or job
    return dict(ok=True, job=job)


def switch_account(app, body):
    rt = runtime(app)
    result = rt.desk('account_select', {'account_id': str(body.get('account_id') or '')})
    rt.clear()
    return dict(ok=True, result=_job(result))


def desk(app, body):
    rt = runtime(app)
    action = str(body.get('action') or '')
    result = rt.desk(action, body.get('args'))
    if action.startswith(('account_', 'app_chat_', 'main_')) or action in ('new_welcome_chat', 'app_settings_set'):
        rt.clear()
    from .notion_shell import scrub
    return dict(ok=True, result=scrub(result))


GET = {'state': lambda app, query: state(app),
       'chats': lambda app, query: chats(app, fresh=_first(query, 'fresh') in ('1', 'true', 'yes')),
       'chat': chat, 'live': live}
POST = {'send': send, 'space': switch_space, 'account': switch_account, 'desk': desk}


def http_route(app, method, path, query=None, body=None):
    """(status, payload) for one /api/ui/app/* request."""
    name = str(path or '')[len(PREFIX):].strip('/')
    routes = GET if method == 'GET' else POST if method == 'POST' else None
    if routes is None:
        return 405, {'error': 'Method not allowed'}
    handler = routes.get(name)
    if handler is None:
        return (405, {'error': 'Method not allowed'}) if name in GET or name in POST else (404, {'error': 'Unknown API route'})
    try:
        if method == 'GET':
            return 200, handler(app, query or {})
        if not isinstance(body, dict):
            return 400, {'error': 'Expected a JSON object'}
        return 200, handler(app, body)
    except ValueError as exc:
        return 400, {'error': redact(str(exc) or 'Bad request')[:400]}
    except Exception as exc:  # noqa: BLE001 - the window shows the message
        return 500, {'error': redact(str(exc) or type(exc).__name__)[:400]}
