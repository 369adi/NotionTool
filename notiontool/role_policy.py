"""Pure role policy for dynamic task assignment (Phase 4b, NOT WIRED).

Field names follow notiontool/engine.py (tick and _choose_worker):

- worker: "id", "enabled", "state" ("ready" means free), "purpose"
  ("worker" for coworkers); optional "kind" ("main" marks the operator Main)
- task: "id", "role", "state", "worker_id", "files", "deps" (dag.py calls
  the same list "depends_on"; both are read), "ready_at"
- run: "id", "main_worker"; optional "tasks"

Shapes added by this module (engine.py has no matching field yet):

- context: {"settings": {...}, "is_main": bool, "run_tasks": [task, ...]}
- task "reviews": optional list of reviewed part ids; without it a reviewer
  task reviews its deps / depends_on
- task "tools" == "read_write" or task "needs_write" == True: the task asks
  for write tools
- task "queued_at": fallback for "ready_at" when computing the affinity wait
- history entry: {"worker_id": str, "files": [path, ...], "at": seconds}

No network, no side effects, stdlib and project imports only.
"""

from notiontool import dag

PLANNER = "planner"
EXECUTOR = "executor"
REVIEWER = "reviewer"
INTEGRATOR = "integrator"
ROLES = (PLANNER, EXECUTOR, REVIEWER, INTEGRATOR)

READ_ONLY = "read_only"
READ_WRITE = "read_write"
_RIGHTS = {
    PLANNER: READ_ONLY,
    EXECUTOR: READ_WRITE,
    REVIEWER: READ_ONLY,
    INTEGRATOR: READ_WRITE,
}

DEFAULT_MAX_AFFINITY_WAIT_S = 30


def tool_rights(role):
    """Return "read_only" or "read_write" for a role; unknown roles raise ValueError."""
    if role not in _RIGHTS:
        raise ValueError("Unknown role: %r." % (role,))
    return _RIGHTS[role]


def _settings(context):
    return (context or {}).get("settings") or {}


def is_main(worker, run=None, context=None):
    """True when the worker is the operator Main."""
    if (context or {}).get("is_main"):
        return True
    if worker.get("kind") == "main" or worker.get("purpose") == "main":
        return True
    return bool(run) and run.get("main_worker") is not None and worker.get("id") == run.get("main_worker")


def _task_deps(task):
    return list(task.get("deps") or task.get("depends_on") or ())


def reviewed_ids(task):
    """Ids of the parts a reviewer task reviews."""
    reviews = task.get("reviews")
    if reviews:
        return list(reviews)
    return _task_deps(task)


def _run_tasks(run, context):
    tasks = (context or {}).get("run_tasks")
    if tasks is None and run:
        tasks = run.get("tasks")
    return list(tasks or ())


def asks_for_write(task):
    return task.get("tools") == READ_WRITE or task.get("needs_write") is True


def eligible(worker, task, run, context=None):
    """Return (ok, reason) for handing task to worker.

    Checks, in order: worker missing, disabled or not ready; operator Main
    without main_takes_tasks; reviewer on a part it executed; read-only role
    asking for write tools. Otherwise any ready coworker may take any role.
    """
    if not worker:
        return False, "The worker is missing."
    if not worker.get("enabled"):
        return False, "The worker is disabled."
    if worker.get("state") != "ready":
        return False, "The worker is not ready."
    role = task.get("role")
    if role not in _RIGHTS:
        return False, "The task has an unknown role."
    if is_main(worker, run, context) and not _settings(context).get("main_takes_tasks", False):
        return False, "The operator Main takes no tasks unless main_takes_tasks is true."
    if role == REVIEWER:
        wanted = set(reviewed_ids(task))
        for other in _run_tasks(run, context):
            if (other.get("id") in wanted and other.get("role") == EXECUTOR
                    and other.get("worker_id") is not None
                    and other.get("worker_id") == worker.get("id")):
                return False, "A reviewer must not review a part it executed."
    if asks_for_write(task) and tool_rights(role) == READ_ONLY:
        return False, "The %s role is read-only and cannot get write tools." % role
    return True, "The worker is ready and may take the %s role." % role


def _file_keys(files):
    return dag.conflict_keys({"files": list(files or ())})


def affinity(worker, task, history, now=None, settings=None):
    """Share (0..1) of the task's files whose most recent holder is this worker.

    Returns 0.0 once now minus the time the task was queued (ready_at, else
    queued_at) exceeds max_affinity_wait_s (default 30). Paths are compared
    with dag.conflict_keys (task_cache.normalize_path, case-folded).
    """
    if not worker:
        return 0.0
    keys = _file_keys(task.get("files"))
    if not keys:
        return 0.0
    max_wait = (settings or {}).get("max_affinity_wait_s", DEFAULT_MAX_AFFINITY_WAIT_S)
    queued = task.get("ready_at") or task.get("queued_at")
    if now is not None and queued is not None and now - queued > max_wait:
        return 0.0
    latest = {}
    for position, entry in enumerate(history or ()):
        stamp = (entry.get("at") or 0, position)
        for key in _file_keys(entry.get("files")):
            if key in keys and (key not in latest or stamp >= latest[key][0]):
                latest[key] = (stamp, entry.get("worker_id"))
    held = sum(1 for key in keys if key in latest and latest[key][1] == worker.get("id"))
    return held / float(len(keys))
