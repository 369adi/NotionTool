"""Which files are being edited right now, shared by every chat and kept across restarts.

Coworker tasks own files through their task records (engine.claim_file and
claim_absolute). Operator edits made with the developer tools are registered
here with a time limit. Both live in the store, so a restart keeps them and
every chat (prompts, the context tool, write refusals) sees the same list.
"""
import os
import time
from pathlib import Path

KIND = 'file_claims'
TTL = 15 * 60
ACTIVE = ('running', 'cancelling', 'uncertain')
SUMMARY_LIMIT = 20


def key(path):
    return str(Path(path).resolve()).casefold()


def path_key(path):
    """Comparison key for one file: resolved and case-insensitive (os.path.normcase on Windows)."""
    return os.path.normcase(str(Path(path).resolve())).casefold()


def normalize(root, path):
    """Project-relative spelling with forward slashes.

    Accepts backslashes, ./ segments and absolute paths inside root; refuses paths outside root.
    """
    root = Path(root).resolve()
    text = str(path).strip().replace('\\', '/')
    if not text:
        raise ValueError('A claimed file must name a file below the project folder')
    candidate = Path(text)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    root_key, file_key = path_key(root), path_key(resolved)
    if file_key == root_key:
        raise ValueError('A claimed file must name a file below the project folder')
    if not file_key.startswith(root_key.rstrip(os.sep) + os.sep):
        raise ValueError('A claimed file is outside the project folder: ' + str(path))
    return str(resolved)[len(str(root)):].lstrip('\\/').replace('\\', '/')


def normalize_files(root, files, strict=True):
    """Normalised claim list without duplicates; the first spelling of a file wins.

    With strict=False (stored claims) an unexpected outside path stays visible instead of failing.
    """
    result, seen = [], set()
    for path in files or []:
        try:
            relative = normalize(root, path)
        except ValueError:
            if strict:
                raise
            relative = str(path)
        ident = path_key(Path(root) / relative)
        if ident not in seen:
            seen.add(ident)
            result.append(relative)
    return result


def resource_keys(root, files):
    """Conflict keys for a task's files: the same keys task_claims and claim_operator use."""
    return [path_key(Path(root) / relative) for relative in normalize_files(root, files)]


def overlap(a, b):
    """Same file, or one path is a folder that contains the other."""
    sep = '\\' if '\\' in a or '\\' in b else '/'
    return a == b or a.startswith(b.rstrip(sep) + sep) or b.startswith(a.rstrip(sep) + sep)


def _root(store, run):
    return Path(run.get('project_path') or Path(store.directory) / 'artifacts' / run['id']).resolve()


def operator_claims(store, now=None):
    """Live operator claims. Expired records are deleted first, so every read path (operator_owner,
    in_use, summary) cleans them up; live claims are never touched."""
    now = time.time() if now is None else now
    release_expired(store, now)
    return [claim for claim in store.all(KIND) if claim.get('expires', 0) > now]


def operator_owner(store, keys, now=None):
    """The active operator claim that overlaps one of the resource keys, if any."""
    for claim in operator_claims(store, now):
        if any(overlap(str(k), claim['id']) for k in keys):
            return claim
    return None


def task_claims(store, exclude_task=''):
    """Files owned by active coworker tasks, with the same keys the engine uses for conflicts."""
    result, runs = [], {}
    for task in store.all('tasks'):
        if task.get('id') == exclude_task or (task.get('state') not in ACTIVE and not task.get('remote_unknown')):
            continue
        run_id = task.get('run_id', '')
        if run_id not in runs:
            runs[run_id] = store.get('runs', run_id, {}) or {}
        run = runs[run_id]
        owner = dict(owner='task', task_id=task['id'], title=task.get('title', ''), role=task.get('role', ''),
                     worker_id=task.get('worker_id', ''), run_id=run_id)
        if task.get('files') and run.get('id'):
            root = _root(store, run)
            for relative in normalize_files(root, task['files'], strict=False):
                path = (root / relative).resolve()
                result.append(dict(owner, path=str(path), key=path_key(path)))
        for resource in task.get('absolute_resources') or []:
            result.append(dict(owner, path=str(resource), key=str(resource).casefold()))
    return result


def release_expired(store, now=None):
    """Delete operator claims whose time limit has passed (expires <= now). Coworker task claims live in the
    task records and are not affected."""
    now = time.time() if now is None else now
    for claim in store.all(KIND):
        if claim.get('expires', 0) <= now:
            store.delete(KIND, claim['id'])


def claim_operator(store, path, now=None):
    """Register an operator edit. Refuses while an active coworker task owns the file."""
    now = time.time() if now is None else now
    resolved = Path(path).resolve()
    ident = path_key(resolved)
    for claim in task_claims(store):
        if overlap(ident, claim['key']):
            raise ValueError('A coworker task is editing this file right now (' + (claim['title'] or claim['role'] or 'task')
                             + ', ' + claim['task_id'] + '); wait until it finishes or cancel that run first. Nothing was changed.')
    release_expired(store, now)
    old = store.get(KIND, ident)
    fresh = not old or old.get('expires', 0) <= now
    store.put(KIND, dict(id=ident, path=str(resolved), owner='operator', since=now if fresh else old.get('since', now),
                         at=now, expires=now + TTL))
    if fresh:
        store.event('file_claimed', path=str(resolved), owner='operator', ttl=TTL)
    return ident


def _label(claim):
    name = claim.get('title') or claim.get('role') or 'task'
    return name + ' (' + str(claim.get('task_id', ''))[:9] + ')'


def in_use(store, exclude_task='', now=None):
    """Every file another chat is editing right now: coworker tasks first, then the operator."""
    items = [dict(path=claim['path'], owner=_label(claim), task_id=claim['task_id']) for claim in task_claims(store, exclude_task)]
    items += [dict(path=claim['path'], owner='operator', since=claim.get('since')) for claim in operator_claims(store, now)]
    return items


SCRATCH_DIRS = ('work', 'data', '.git')


def _relative(root, path):
    if root is None:
        return None
    try:
        return Path(path).resolve().relative_to(root)
    except (ValueError, OSError):
        return None


def _is_scratch(root, path):
    """Operator helper files (work/, data/) are never something a coworker would edit."""
    rel = _relative(root, path)
    return rel is not None and bool(rel.parts) and rel.parts[0].casefold() in SCRATCH_DIRS


def relevant(store, project_path='', exclude_task='', now=None, task=None):
    """Claims that matter for this task: no operator scratch, and with a task only files overlapping its own files."""
    root = Path(project_path).resolve() if project_path else None
    items = [item for item in in_use(store, exclude_task, now) if not _is_scratch(root, item['path'])]
    if task is None:
        return items
    if root is None or not task.get('files'):
        return []
    mine = [path_key(root / relative) for relative in normalize_files(root, task['files'], strict=False)]
    return [item for item in items if any(overlap(path_key(item['path']), key) for key in mine)]


def summary(store, project_path='', exclude_task='', now=None, task=None):
    """Prompt text about files of this task that another chat is also changing.

    Lists only what is relevant: never operator helper files, and with a task only its own files.
    A task with its own worktree is not forbidden anything: it writes into its copy and the
    overlap is solved at merge, so the note only asks to keep that edit small.
    """
    items = relevant(store, project_path, exclude_task, now, task)
    if not items:
        return ''
    root = Path(project_path).resolve() if project_path else None
    names = []
    for item in items[:SUMMARY_LIMIT]:
        rel = _relative(root, item['path'])
        names.append((str(rel) if rel is not None else str(item['path'])) + ' (' + item['owner'] + ')')
    more = len(items) - SUMMARY_LIMIT
    text = '; '.join(names) + ('; and ' + str(more) + ' more' if more > 0 else '')
    if task is not None and task.get('patch_worktree'):
        return ('\nHeads-up: another chat is also changing some of your files: ' + text + '. You work in your own'
                ' copy, so go ahead, but keep your edits there focused; overlaps are merged after review.\n')
    return ('\nOther chats are editing these files right now; do not write to them, and describe a needed change'
            ' in your result instead: ' + text + '.\n')
