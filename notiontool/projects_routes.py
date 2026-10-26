"""HTTP routes of the Projects page: /api/ui/projects (docs/plan/redesign/api/projects.json).

GET  /api/ui/projects[?archived=1]      -> {projects, current_project_id, chats}
GET  /api/ui/projects/<id>[?n=10]       -> {project}  (with the latest commits of its repo)
POST /api/ui/projects                   -> {project}  create {name, goal, repo_path, main_chat_id, chat_ids}
POST /api/ui/projects/update            -> {project}  {id, name, goal, repo_path, main_chat_id, chat_ids}
POST /api/ui/projects/open              -> {project, current_project_id}  {id}
POST /api/ui/projects/archive           -> {project, current_project_id}  {id}
POST /api/ui/projects/unarchive         -> {project}  {id}

The model lives in projects.py (objects table, kind 'projects'); this module only maps HTTP to it.
Chats are never created here: a project only links chats that already exist. Every failure is a
ProjectError/ValueError, which server.py answers with 400.
"""
import threading
import time

from . import projects as P

PREFIX = '/api/ui/projects'
LOCK = threading.Lock()
MAX_COMMITS = 50


def matches(path):
    return path == PREFIX or path.startswith(PREFIX + '/')


def _first(query, key, default=''):
    values = (query or {}).get(key) or [default]
    return str(values[0])


def _flag(value):
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def chat_choices(store):
    """Existing chats a project may link: id, title and the project that owns each one."""
    owners = {}
    for project in P.list_projects(store):
        for chat_id in project.get('chat_ids') or ():
            owners[chat_id] = project['id']
    chats = []
    for chat in store.all('chats'):
        if chat.get('state') in ('retired', 'released'):
            continue
        chats.append(dict(id=chat['id'], title=str(chat.get('title') or chat['id'])[:120],
                          main_number=chat.get('main_number') or 0, project_id=owners.get(chat['id'], '')))
    return sorted(chats, key=lambda c: (c['main_number'] or 0, c['title']))


def _thread_id(store, chat):
    """Notion thread of a chat: its Main worker's thread, else the thread stored on the chat (or '')."""
    wid = chat.get('main_worker') or ''
    worker = (store.get('workers', wid) if wid else None) or {}
    return str(worker.get('thread_id') or chat.get('thread_id') or '')


def enrich(store, view):
    """Add readable chat titles, chat thread ids (for the sidebar) and coworker labels to a project view."""
    titles = {}
    threads = {}
    chats = []
    for chat_id in view.get('chat_ids') or ():
        chat = store.get('chats', chat_id) or {}
        title = str(chat.get('title') or '')[:120]
        titles[chat_id] = title
        threads[chat_id] = _thread_id(store, chat) if chat else ''
        chats.append(dict(id=chat_id, title=title or chat_id, main=chat_id == view.get('main_chat_id'),
                          missing=not chat, thread_id=threads[chat_id]))
    coworkers = []
    for worker_id in view.get('coworker_ids') or ():
        worker = store.get('workers', worker_id) or {}
        coworkers.append(dict(id=worker_id, label=str(worker.get('label') or worker_id)[:80],
                              state=str(worker.get('state') or 'missing')))
    return dict(view, chats=chats, coworkers=coworkers, main_chat_title=titles.get(view.get('main_chat_id'), ''),
                main_thread_id=threads.get(view.get('main_chat_id'), ''))


def _view(store, project):
    return enrich(store, P.project_view(store, project))


def _project_id(body):
    return str(body.get('id') or body.get('project_id') or '').strip()


def _changes(body):
    out = {key: body[key] for key in P.FIELDS if key in body}
    if 'chat_ids' in out and not isinstance(out['chat_ids'], (list, tuple)):
        raise P.ProjectError('chat_ids must be a list')
    return out


def http_route(app, method, path, query=None, body=None):
    """(status, payload) for one /api/ui/projects request."""
    store = app.store
    rest = path[len(PREFIX):].strip('/')
    body = body if isinstance(body, dict) else {}
    if method == 'GET':
        if not rest:
            projects = P.list_projects(store, include_archived=_flag(_first(query, 'archived')))
            projects = sorted(projects, key=lambda p: p.get('updated') or 0, reverse=True)
            return 200, dict(projects=[_view(store, p) for p in projects],
                             current_project_id=P.current_project_id(store), chats=chat_choices(store))
        try:
            count = max(1, min(MAX_COMMITS, int(_first(query, 'n', '10'))))
        except ValueError:
            raise P.ProjectError('n must be a number')
        detail = P.project_detail(store, rest, count)
        return 200, dict(project=enrich(store, detail), current_project_id=P.current_project_id(store))
    if method != 'POST':
        return 405, {'error': 'Method not allowed'}
    with LOCK:
        if not rest:
            changes = _changes(body)
            made = P.create_project(store, body.get('name'), goal=changes.get('goal', ''),
                                    repo_path=changes.get('repo_path', ''), main_chat_id=changes.get('main_chat_id', ''),
                                    chat_ids=changes.get('chat_ids') or ())
            return 200, dict(project=_view(store, made))
        ident = _project_id(body)
        if rest == 'update':
            return 200, dict(project=_view(store, P.update_project(store, ident, **_changes(body))))
        if rest == 'open':
            return 200, dict(project=_view(store, P.open_project(store, ident)), current_project_id=P.current_project_id(store))
        if rest == 'archive':
            done = P.archive_project(store, ident)
            return 200, dict(project=_view(store, done), current_project_id=P.current_project_id(store))
        if rest == 'unarchive':
            project = P.get_project(store, ident)
            if project.get('status') == 'archived':
                # An archived project may not hold a chat that another active project took meanwhile.
                P._check_chats(store, project['id'], project.get('chat_ids') or ())
                project = store.put(P.PROJECTS, dict(project, status='active', updated=time.time()))
            return 200, dict(project=_view(store, project))
    return 404, {'error': 'Unknown API route'}
