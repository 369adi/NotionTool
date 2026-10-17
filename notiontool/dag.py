"""Pure DAG helpers for task dependencies.

A task is a mapping with at least an "id". Optional keys:

- "depends_on": list of task ids that must be done first
- "state": for example "queued", "running", "done" or "failed"
- "files": list of file paths the task works on
- "estimated_duration": number; a missing value or None counts as 0

Every function takes a list of task mappings and never changes its input.
Only the standard library and task_cache.normalize_path are used.
"""

from notiontool.task_cache import normalize_path

DONE = "done"
RUNNING = "running"
FAILED = "failed"
NOT_READY_STATES = frozenset({DONE, RUNNING, FAILED})


class CycleError(ValueError):
    """Raised when a dependency cycle makes an operation impossible."""

    def __init__(self, cycle):
        self.cycle = list(cycle)
        text = " -> ".join(str(item) for item in self.cycle)
        super().__init__("dependency cycle: " + text)


def _id_key(task_id):
    """Sort key for task ids: the smaller id is the smaller string."""
    return str(task_id)


def _index(tasks):
    index = {}
    for task in tasks:
        task_id = task["id"]
        if task_id in index:
            raise ValueError("duplicate task id: %r" % (task_id,))
        index[task_id] = task
    return index


def _deps(task):
    seen = []
    for dep in task.get("depends_on") or ():
        if dep not in seen:
            seen.append(dep)
    return seen


def _known_deps(index, task_id):
    return sorted((d for d in _deps(index[task_id]) if d in index), key=_id_key)


def missing_dependencies(tasks):
    """Return {task_id: [dependency ids that name no known task]}.

    Tasks without missing dependencies are left out. The lists keep the
    order of depends_on, without duplicates.
    """
    index = _index(tasks)
    result = {}
    for task_id in sorted(index, key=_id_key):
        missing = [d for d in _deps(index[task_id]) if d not in index]
        if missing:
            result[task_id] = missing
    return result


def find_cycle(tasks):
    """Return one dependency cycle as [a, b, ..., a], or None.

    The path follows depends_on edges, so each element depends on the next.
    Unknown dependencies are ignored here (see missing_dependencies). The
    search visits ids in sorted order, so the result is deterministic.
    """
    index = _index(tasks)
    white, gray, black = 0, 1, 2
    color = dict.fromkeys(index, white)
    for start in sorted(index, key=_id_key):
        if color[start] != white:
            continue
        color[start] = gray
        path = [start]
        stack = [iter(_known_deps(index, start))]
        while stack:
            advanced = False
            for dep in stack[-1]:
                if color[dep] == gray:
                    return path[path.index(dep):] + [dep]
                if color[dep] == white:
                    color[dep] = gray
                    path.append(dep)
                    stack.append(iter(_known_deps(index, dep)))
                    advanced = True
                    break
            if not advanced:
                color[path.pop()] = black
                stack.pop()
    return None


def ready_set(tasks):
    """Return the sorted ids of tasks that can start now.

    A task is ready when its state is not done, running or failed, and every
    dependency names a known task that is done. A task with a missing
    dependency is never ready.
    """
    index = _index(tasks)
    ready = []
    for task_id in sorted(index, key=_id_key):
        task = index[task_id]
        if task.get("state") in NOT_READY_STATES:
            continue
        deps = _deps(task)
        if all(d in index and index[d].get("state") == DONE for d in deps):
            ready.append(task_id)
    return ready


def _duration(task):
    value = task.get("estimated_duration")
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("estimated_duration of %r is not a number" % (task["id"],))
    if value < 0:
        raise ValueError("estimated_duration of %r is negative" % (task["id"],))
    return value


def critical_path(tasks):
    """Return (path, total) for the longest chain by estimated duration.

    path lists ids in run order (dependencies first). A missing duration
    counts as 0. A cycle raises CycleError. Unknown dependencies are ignored.
    Ties go to the smaller id, both for the end of the path and for each
    predecessor. An empty task list gives ([], 0).
    """
    cycle = find_cycle(tasks)
    if cycle is not None:
        raise CycleError(cycle)
    index = _index(tasks)
    if not index:
        return [], 0
    durations = {task_id: _duration(task) for task_id, task in index.items()}

    dependents = {task_id: [] for task_id in index}
    pending = {}
    for task_id in index:
        deps = _known_deps(index, task_id)
        pending[task_id] = len(deps)
        for dep in deps:
            dependents[dep].append(task_id)
    order = []
    queue = sorted((t for t, n in pending.items() if n == 0), key=_id_key)
    while queue:
        task_id = queue.pop(0)
        order.append(task_id)
        for nxt in dependents[task_id]:
            pending[nxt] -= 1
            if pending[nxt] == 0:
                queue.append(nxt)
        queue.sort(key=_id_key)

    best = {}
    previous = {}
    for task_id in order:
        choice = None
        for dep in _known_deps(index, task_id):
            if choice is None or best[dep] > best[choice]:
                choice = dep
        previous[task_id] = choice
        best[task_id] = durations[task_id] + (best[choice] if choice is not None else 0)

    end = None
    for task_id in sorted(index, key=_id_key):
        if end is None or best[task_id] > best[end]:
            end = task_id
    path = []
    node = end
    while node is not None:
        path.append(node)
        node = previous[node]
    path.reverse()
    return path, best[end]


def task_files(task):
    """Return the task's normalized paths without duplicates, in order.

    a/b.py and a\\b.py are the same entry, so a task never conflicts with
    itself.
    """
    result = []
    seen = set()
    for path in task.get("files") or ():
        norm = normalize_path(path)
        if norm and norm not in seen:
            seen.add(norm)
            result.append(norm)
    return result


def conflict_keys(task):
    """Return the case-folded file keys used only for conflict checks."""
    return frozenset(path.casefold() for path in task_files(task))


def shared_files(task_a, task_b):
    """Return the sorted conflict keys two tasks have in common."""
    return sorted(conflict_keys(task_a) & conflict_keys(task_b))


def dispatchable(tasks, order=None, limit=None):
    """Return ready task ids that may be handed out together.

    Tasks are taken from ready_set in the given order (a list of ids, for
    example the critical path first) or in sorted id order. Ids in order
    that are not ready are skipped; ready ids missing from order follow in
    sorted order. A task is skipped when one of its files is held by a
    running task or by a task already picked in this call. limit caps the
    number of picked tasks.
    """
    index = _index(tasks)
    ready = ready_set(tasks)
    ready_lookup = set(ready)
    candidates = []
    for task_id in list(order or ()) + ready:
        if task_id in ready_lookup and task_id not in candidates:
            candidates.append(task_id)
    held = set()
    for task in index.values():
        if task.get("state") == RUNNING:
            held |= conflict_keys(task)
    picked = []
    for task_id in candidates:
        if limit is not None and len(picked) >= limit:
            break
        keys = conflict_keys(index[task_id])
        if keys & held:
            continue
        picked.append(task_id)
        held |= keys
    return picked
