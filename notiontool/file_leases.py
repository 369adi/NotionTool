"""Short write leases for files, taken only at the moment a chat writes.

Replaces the static 'other chats are editing these files' prompt list:

- Every coworker may read and plan changes to any file; prompts list no files.
- A write takes a short lease (one write call plus at most MAX_LEASE_S).
- Leases are freed by task state (release_for_tasks), not by a long TTL.
- check_write is the optimistic check against the hash the writer read.
- plan_order turns shared files into dependencies instead of forbidding them.
- The Main (operator) uses the same leases with holder MAIN_TASK / MAIN_LABEL.

Every function is pure: no IO, no clock, no globals. Callers pass 'now'
(epoch seconds) and dicts, and get new plain dicts back; inputs are never
changed. A lease store is {path_key: lease}, lease = dict(path, task_id,
label, since, expires).
"""

import math

LIVE_TASK_STATES = frozenset({'running', 'cancelling', 'uncertain', 'checkpoint', 'preparing'})
MAIN_TASK = 'main'
MAIN_LABEL = 'Main'
MAX_LEASE_S = 60
DEFAULT_LEASE_S = 60
DEFAULT_WAIT_HINT_S = 5
SETTING = 'file_lease_s'


def lease_seconds(settings=None):
    """Lease length from settings[SETTING]: 1 to MAX_LEASE_S seconds, default DEFAULT_LEASE_S."""
    value = (settings or {}).get(SETTING, DEFAULT_LEASE_S)
    if isinstance(value, bool):
        return DEFAULT_LEASE_S
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_LEASE_S
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_LEASE_S
    return max(1.0, min(float(MAX_LEASE_S), value))


def normalize_key(path):
    """One key per file: forward slashes, no . or empty segments, .. resolved, case-folded."""
    text = str(path or '').strip().replace('\\', '/')
    absolute = text.startswith('/')
    parts = []
    for part in text.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if parts and parts[-1] != '..':
                parts.pop()
            elif not absolute:
                parts.append(part)
            continue
        parts.append(part)
    return (('/' if absolute else '') + '/'.join(parts)).casefold()


def _live(lease, now):
    return bool(lease) and float(lease.get('expires', 0)) > now


def _since_s(lease, now):
    return max(0, int(round(now - float(lease.get('since', now)))))


def _wait_hint_s(lease, now):
    left = float(lease.get('expires', now)) - now
    return max(1, min(DEFAULT_WAIT_HINT_S, int(math.ceil(left)))) if left > 0 else 1


def acquire(store_dict, path_key, task_id, now, label='', settings=None):
    """Take or renew the write lease for one file.

    Returns granted (new or renewed by the same task) or busy with
    holder_task, holder_label, since_s and wait_hint_s. An expired lease
    never blocks. The returned 'leases' is a new dict; store_dict is unchanged.
    """
    key = normalize_key(path_key)
    task_id = str(task_id)
    leases = dict(store_dict or {})
    current = leases.get(key)
    if _live(current, now) and str(current.get('task_id')) != task_id:
        return dict(status='busy', granted=False, path_key=key,
                    holder_task=str(current.get('task_id')), holder_label=current.get('label') or str(current.get('task_id')),
                    since_s=_since_s(current, now), wait_hint_s=_wait_hint_s(current, now), leases=leases)
    renewed = _live(current, now)
    lease = dict(path=str(path_key), task_id=task_id, label=label or (current or {}).get('label') or task_id,
                 since=float(current['since']) if renewed else float(now), expires=float(now) + lease_seconds(settings))
    leases[key] = lease
    return dict(status='granted', granted=True, renewed=renewed, path_key=key, lease=dict(lease), leases=leases)


def acquire_main(store_dict, path_key, now, settings=None):
    """The Main (operator) edit takes the same lease with holder Main."""
    return acquire(store_dict, path_key, MAIN_TASK, now, MAIN_LABEL, settings)


def release(store_dict, path_key, task_id):
    """Remove the caller's own lease only. Another holder's lease stays."""
    key = normalize_key(path_key)
    leases = dict(store_dict or {})
    current = leases.get(key)
    if current is None:
        return dict(released=False, reason='no_lease', path_key=key, leases=leases)
    if str(current.get('task_id')) != str(task_id):
        return dict(released=False, reason='not_owner', path_key=key, holder_task=str(current.get('task_id')), leases=leases)
    del leases[key]
    return dict(released=True, reason='released', path_key=key, leases=leases)


def release_for_tasks(leases, task_states, now=None):
    """Drop every lease whose task is not in LIVE_TASK_STATES, unknown tasks included.

    task_states maps task_id to state. Main leases are not tasks: they stay
    until they expire. When 'now' is given, expired leases are dropped too.
    Returns dict(leases=kept, freed=[path keys]).
    """
    states = {str(k): v for k, v in (task_states or {}).items()}
    kept, freed = {}, []
    for key, lease in (leases or {}).items():
        task_id = str(lease.get('task_id'))
        expired = now is not None and not _live(lease, now)
        if task_id == MAIN_TASK:
            keep = not expired
        else:
            keep = states.get(task_id) in LIVE_TASK_STATES and not expired
        if keep:
            kept[key] = dict(lease)
        else:
            freed.append(key)
    return dict(leases=kept, freed=sorted(freed))


def prune(leases, now):
    """Drop expired leases only. Returns dict(leases=kept, freed=[path keys])."""
    kept = {k: dict(v) for k, v in (leases or {}).items() if _live(v, now)}
    return dict(leases=kept, freed=sorted(k for k in (leases or {}) if k not in kept))


def _same_hash(a, b):
    return str(a).strip().casefold() == str(b).strip().casefold()


def check_write(path_key, base_hash, current_hash):
    """Optimistic check before a write.

    base_hash is the hash the writer read (None: it read nothing),
    current_hash the file's hash now (None: the file does not exist).
    """
    key = normalize_key(path_key)
    shown = str(path_key).replace('\\', '/')
    base_hash = base_hash or None
    current_hash = current_hash or None
    result = dict(path_key=key, base_hash=base_hash, current_hash=current_hash)
    if current_hash is None and base_hash is None:
        return dict(result, status='ok', ok=True, reason='new_file', message='')
    if base_hash is not None and current_hash is not None and _same_hash(base_hash, current_hash):
        return dict(result, status='ok', ok=True, reason='unchanged', message='')
    if base_hash is None:
        return dict(result, status='conflict', ok=False, reason='not_read',
                    message='File ' + shown + ' already exists and you have not read it. Read it first and apply your change on top of it; never overwrite it.')
    if current_hash is None:
        return dict(result, status='conflict', ok=False, reason='deleted',
                    message='File ' + shown + ' was deleted since you read it. Check why before you write it again; never recreate it blindly.')
    return dict(result, status='conflict', ok=False, reason='changed',
                message='File ' + shown + ' changed since you read it. Read it again and apply your change on top of the new version; never overwrite it.')


def busy_message(path, holder_label, since_s, wait_hint_s=None):
    """Short English text for a busy lease, plus wait_hint_s."""
    seconds = max(0, int(round(float(since_s or 0))))
    hint = DEFAULT_WAIT_HINT_S if wait_hint_s is None else max(1, int(wait_hint_s))
    text = ('File ' + str(path).replace('\\', '/') + ' is being written by ' + str(holder_label)
            + ' right now (about ' + str(seconds) + ' s). Wait a few seconds and try again.')
    return dict(text=text, wait_hint_s=hint)


def busy_message_for(result, path=None):
    """busy_message from a busy acquire result."""
    return busy_message(path or result.get('path_key'), result.get('holder_label'), result.get('since_s'), result.get('wait_hint_s'))


def _task_deps(task):
    deps = task.get('depends_on')
    if deps is None:
        deps = task.get('deps')
    out = []
    for dep in deps or ():
        if str(dep) not in out:
            out.append(str(dep))
    return out


def _reaches(graph, start, target):
    """True when start depends on target, directly or through other tasks."""
    seen, stack = set(), [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, ()))
    return False


def plan_order(tasks):
    """Chain planned tasks that list the same file, in planned order.

    For each file, every later task depends on the previous task that lists
    it. Edges that already exist are skipped, and so are edges that would
    create a cycle. Inputs are unchanged. Returns dict(edges=[{task,
    depends_on, file}], skipped=[{task, depends_on, file, reason}],
    depends_on={task_id: full new list}).
    """
    order = [str(t['id']) for t in tasks]
    graph = {str(t['id']): _task_deps(t) for t in tasks}
    last, edges, skipped = {}, [], []
    for task in tasks:
        tid = str(task['id'])
        keys = []
        for path in task.get('files') or ():
            key = normalize_key(path)
            if key and key not in keys:
                keys.append(key)
        for key in keys:
            prev = last.get(key)
            last[key] = tid
            if prev is None or prev == tid:
                continue
            edge = dict(task=tid, depends_on=prev, file=key)
            if prev in graph[tid]:
                skipped.append(dict(edge, reason='exists'))
            elif _reaches(graph, prev, tid):
                skipped.append(dict(edge, reason='cycle'))
            else:
                graph[tid].append(prev)
                edges.append(edge)
    return dict(edges=edges, skipped=skipped, depends_on={tid: list(graph[tid]) for tid in order})
