"""Project records and per-project stores on top of the existing Store (kind/id documents).

Pure helpers (new_project, renamed, archived, new_item, assign_task_project) never touch the
store. The store functions only use Store.get, Store.all, Store.put and Store.batch.
Every per-project record carries a project_id field; artifacts live under the kind 'artifacts'.
"""
from __future__ import annotations

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


def create_project(store, name, now=None):
    return store.put(PROJECTS, new_project(name, now))


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
