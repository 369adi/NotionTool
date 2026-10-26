"""Project records and per-project stores on top of the existing Store (kind/id documents).

Pure helpers (new_project, renamed, archived, new_item, assign_task_project) never touch the
store. The store functions only use Store.get, Store.all, Store.put and Store.batch.
Every per-project record carries a project_id field; artifacts live under the kind 'artifacts'.
"""
from __future__ import annotations

import os
import subprocess
import time

from .store import uid

PROJECTS = 'projects'
STATE = 'project_state'
CURRENT = 'current'
STORES = ('goals', 'rules', 'decisions', 'artifacts', 'task_results', 'summaries', 'open_items')


class ProjectError(ValueError):
    """Unknown project id, archived project or unknown per-project store."""


def _now(now):
    return time.time() if now is None else now


def _name(name):
    name = str(name or '').strip()
    if not name:
        raise ProjectError('Project name must not be empty')
    return name


def _kind(kind):
    if kind not in STORES:
        raise ProjectError('Unknown project store ' + repr(kind) + '; use one of ' + ', '.join(STORES))


# Pure record helpers.

def new_project(name, now=None, ident=None):
    now = _now(now)
    return dict(id=ident or uid('p'), name=_name(name), status='active', created=now, updated=now)


def renamed(project, name, now=None):
    return dict(project, name=_name(name), updated=_now(now))


def archived(project, now=None):
    return dict(project, status='archived', updated=_now(now))


def new_item(project_id, kind, data, now=None, ident=None):
    _kind(kind)
    doc = dict(data or {})
    doc.update(id=ident or doc.get('id') or uid(kind[0]), project_id=str(project_id),
               created=doc.get('created', _now(now)))
    return doc


def assign_task_project(task, project_id):
    """Return a copy of task that records which project it belongs to."""
    if not project_id:
        raise ProjectError('project_id is required')
    return dict(task, project_id=str(project_id))


# Store functions.

def get_project(store, project_id):
    project = store.get(PROJECTS, str(project_id)) if project_id else None
    if not project:
        raise ProjectError('Unknown project id ' + repr(project_id))
    return project


def _activatable(store, project_id):
    project = get_project(store, project_id)
    if project.get('status') == 'archived':
        raise ProjectError('Project ' + repr(project_id) + ' is archived and cannot become active')
    return project


def list_projects(store, include_archived=False):
    return [p for p in store.all(PROJECTS) if include_archived or p.get('status') != 'archived']


def current_project_id(store):
    return (store.get(STATE, CURRENT) or {}).get('project_id', '')


def create_project(store, name, now=None, goal='', repo_path='', main_chat_id='', chat_ids=()):
    project = new_project(name, now)
    main, ids = _chats(main_chat_id, chat_ids)
    _check_chats(store, project['id'], ids)
    return store.put(PROJECTS, dict(project, goal=_text(goal), repo_path=_text(repo_path), main_chat_id=main, chat_ids=ids))


def rename_project(store, project_id, name, now=None):
    return store.put(PROJECTS, renamed(get_project(store, project_id), name, now))


def open_project(store, project_id, now=None):
    """Make a project the active one; archived projects cannot become active."""
    project = _activatable(store, project_id)
    store.put(STATE, dict(id=CURRENT, project_id=project['id'], updated=_now(now)))
    return project


def archive_project(store, project_id, now=None):
    """Archive a project; if it was the active one, no project stays active."""
    project = archived(get_project(store, project_id), now)
    records = [(PROJECTS, project)]
    if current_project_id(store) == project['id']:
        records.append((STATE, dict(id=CURRENT, project_id='', updated=project['updated'])))
    store.batch(records)
    return project


def switch_project(store, project_id, intermediate=(), now=None):
    """Save the intermediate results of the project being left, then make project_id active.

    intermediate is a list of dicts; each is stored in the left project's task_results with
    intermediate=True. Saving and switching are committed in one transaction.
    """
    target = _activatable(store, project_id)
    now = _now(now)
    leaving = current_project_id(store)
    records = []
    if leaving and leaving != target['id']:
        for result in intermediate or ():
            records.append(('task_results', new_item(leaving, 'task_results', dict(result, intermediate=True), now)))
    records.append((STATE, dict(id=CURRENT, project_id=target['id'], updated=now)))
    store.batch(records)
    return target


def add_item(store, project_id, kind, data, now=None):
    """Add a record (goal, rule, decision, artifact, ...) to one project's store."""
    get_project(store, project_id)
    return store.put(kind, new_item(project_id, kind, data, now))


def project_items(store, project_id, kind):
    _kind(kind)
    return [doc for doc in store.all(kind) if doc.get('project_id') == str(project_id)]


def save_task(store, task, project_id):
    """Store a task with the project it belongs to."""
    get_project(store, project_id)
    return store.put('tasks', assign_task_project(task, project_id))



# Projects page model: goal, repo, chats, coworkers, commits.
# The record keeps status ('active'/'archived') as the only archive flag; 'archived' in the view is derived from it.

FIELDS = ('name', 'goal', 'repo_path', 'main_chat_id', 'chat_ids')
GIT_TIMEOUT = 10


def _text(value):
    return str(value or '').strip()


def _chats(main_chat_id, chat_ids):
    """One Main chat per project: it is always the first entry of chat_ids."""
    main = _text(main_chat_id)
    ids = [main] if main else []
    for chat_id in chat_ids or ():
        chat_id = _text(chat_id)
        if chat_id and chat_id not in ids:
            ids.append(chat_id)
    return main, ids


def _check_chats(store, project_id, ids):
    """A chat belongs to at most one active project, so project_for_chat is unambiguous."""
    for other in store.all(PROJECTS):
        if other['id'] != project_id and other.get('status') != 'archived':
            clash = [c for c in ids if c in (other.get('chat_ids') or ())]
            if clash:
                raise ProjectError('Chat ' + repr(clash[0]) + ' already belongs to project ' + repr(other['id']))


def update_project(store, project_id, now=None, **changes):
    """Change name, goal, repo_path, main_chat_id and/or chat_ids of one project."""
    unknown = sorted(set(changes) - set(FIELDS))
    if unknown:
        raise ProjectError('Unknown project field ' + ', '.join(unknown) + '; use one of ' + ', '.join(FIELDS))
    project = dict(get_project(store, project_id), updated=_now(now))
    for key, default in dict(goal='', repo_path='', main_chat_id='', chat_ids=[]).items():
        project.setdefault(key, default)
    if 'name' in changes:
        project['name'] = _name(changes['name'])
    for key in ('goal', 'repo_path'):
        if key in changes:
            project[key] = _text(changes[key])
    if 'main_chat_id' in changes or 'chat_ids' in changes:
        main, ids = _chats(changes.get('main_chat_id', project['main_chat_id']), changes.get('chat_ids', project['chat_ids']))
        _check_chats(store, project['id'], ids)
        project.update(main_chat_id=main, chat_ids=ids)
    return store.put(PROJECTS, project)


def project_for_chat(store, chat_id, include_archived=False):
    """The project that owns a chat, or None."""
    chat_id = _text(chat_id)
    for project in list_projects(store, include_archived):
        if chat_id and chat_id in (project.get('chat_ids') or ()):
            return project
    return None


def coworker_ids(store, project):
    """Workers that ran tasks of this project (tasks carry project_id), minus the Main chat's own worker."""
    main = (store.get('chats', project.get('main_chat_id') or '') or {}).get('main_worker') or ''
    found = []
    for task in store.all('tasks'):
        worker = task.get('worker_id')
        if task.get('project_id') == project['id'] and worker and worker != main and worker not in found:
            found.append(worker)
    return found


def project_view(store, project):
    """The JSON shape of one project (docs/plan/redesign/api/projects.json); fills fields old records lack."""
    view = dict(goal='', repo_path='', main_chat_id='', chat_ids=[])
    view.update(project)
    view.update(chat_ids=list(view['chat_ids']), archived=project.get('status') == 'archived',
                coworker_ids=coworker_ids(store, project))
    return view


def project_detail(store, project_id, n=10):
    """project_view plus the latest commits of its repo."""
    view = project_view(store, get_project(store, project_id))
    return dict(view, commits=recent_commits(view['repo_path'], n))


def recent_commits(repo_path, n=10):
    """Newest commits as dicts (sha, subject, author, date); [] when the path is no git repo or git fails."""
    path = _text(repo_path)
    if not path or not os.path.isdir(path):
        return []
    count = max(1, min(100, int(n)))
    try:
        done = subprocess.run(['git', '-C', path, 'log', '-n', str(count), '--pretty=format:%h%x1f%s%x1f%an%x1f%aI'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=GIT_TIMEOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    commits = []
    for line in done.stdout.splitlines():
        parts = line.split('\x1f')
        if len(parts) == 4:
            commits.append(dict(sha=parts[0], subject=parts[1], author=parts[2], date=parts[3]))
    return commits
