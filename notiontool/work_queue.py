"""Pure planning helpers for one shared work queue across all runs.

plan_assignments decides which ready task goes to which free worker, for
every run in one pass. requeue_orphans finds assigned or running tasks whose
worker is gone, disabled or not ready, or whose lease has expired.

Both functions are pure: they never change their input and have no side
effects. Only the standard library and notiontool.dag are used.

Task mappings use these keys: "id", "run_id", "role", "files",
"depends_on", "estimated_duration", "queued_at" (epoch seconds; "ready_at"
is used when it is missing), "state", "worker_id" and "lease_expires".
Worker mappings use "id", "enabled" and "state".
"""

from notiontool import dag

DEFAULT_STARVE_AFTER_S = 300
# Roles that unblock others go before executors.
ROLE_RANK = {"planner": 0, "reviewer": 1, "integrator": 2, "executor": 3}
UNKNOWN_ROLE_RANK = 4
PINNED_STATES = frozenset({"assigned", "running"})
# A worker that is busy is still alive; it is busy with its pinned task.
ALIVE_WORKER_STATES = frozenset({"ready", "busy"})

_RULES = (
    "starving",
    "starving, oldest first",
    "critical path",
    "critical path length",
    "role rank",
    "age",
    "task id order",
)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fmt(value):
    return "%g" % value


def _role_rank(task):
    return ROLE_RANK.get(task.get("role"), UNKNOWN_ROLE_RANK)


def _queued_at(task, now):
    for key in ("queued_at", "ready_at"):
        if _number(task.get(key)):
            return task[key]
    return now


def _starve_after(settings):
    value = (settings or {}).get("starve_after_s")
    return value if _number(value) else DEFAULT_STARVE_AFTER_S


def _duration(task):
    value = task.get("estimated_duration")
    if _number(value) and value > 0:
        return value
    return 0


def _graph_view(task):
    """Copy with only the keys dag needs; the caller's mapping is never passed on."""
    return {
        "id": task["id"],
        "depends_on": list(task.get("depends_on") or task.get("deps") or ()),
        "estimated_duration": _duration(task),
        "files": list(task.get("files") or ()),
    }


def _run_graph(run_id, ready, running_tasks, run_tasks):
    """All known tasks of one run, without duplicate ids."""
    if run_tasks and run_id in run_tasks:
        source = list(run_tasks[run_id])
    else:
        source = [t for t in running_tasks or () if t.get("run_id") == run_id]
    graph = {}
    for task in source + list(ready):
        graph.setdefault(task["id"], _graph_view(task))
    return list(graph.values())


def _remaining_lengths(graph):
    """Longest chain of estimated durations from each task to the end."""
    index = {task["id"]: task for task in graph}
    dependents = {task_id: [] for task_id in index}
    for task in graph:
        for dep in task["depends_on"]:
            if dep in dependents and task["id"] not in dependents[dep]:
                dependents[dep].append(task["id"])
    memo = {}

    def visit(task_id, active):
        if task_id in memo:
            return memo[task_id]
        if task_id in active:
            return 0
        active = active | {task_id}
        tail = max((visit(n, active) for n in dependents[task_id]), default=0)
        memo[task_id] = index[task_id]["estimated_duration"] + tail
        return memo[task_id]

    return {task_id: visit(task_id, frozenset()) for task_id in index}


def _run_info(graph):
    """Call dag.critical_path once for the run; a cycle gives no path."""
    try:
        path, _total = dag.critical_path(graph)
    except ValueError:  # CycleError is a ValueError
        path = []
    return set(path), _remaining_lengths(graph)


def _is_eligible(eligible, worker, task):
    if eligible is None:
        return True
    result = eligible(dict(worker), dict(task))
    if isinstance(result, tuple):
        return bool(result[0])
    return bool(result)


def _rule_text(entry, nxt, starve_after):
    key = entry["key"]
    if nxt is None:
        rule = None
    else:
        rule = next((i for i, (a, b) in enumerate(zip(key, nxt["key"])) if a != b), None)
    if entry["starving"] and rule not in (0, 1):
        rule = 0
    age = _fmt(entry["age"])
    if rule == 0:
        return "starving: waited %ss, more than starve_after_s %s" % (age, _fmt(starve_after))
    if rule == 1:
        return "starving, oldest first: waited %ss" % age
    if rule == 2:
        return "critical path: on the critical path of run %s" % entry["task"].get("run_id")
    if rule == 3:
        return "critical path length: %s remaining, longest first" % _fmt(entry["remaining"])
    if rule == 4:
        return "role rank: %s goes before %s" % (
            entry["task"].get("role") or "unknown role",
            nxt["task"].get("role") or "unknown role",
        )
    if rule == 5:
        return "age: queued %ss ago, oldest first" % age
    if rule == 6:
        return "task id order: all other rules were equal"
    return "last in queue order: no other ready task competed"


def plan_assignments(ready_tasks, free_workers, running_tasks, now, settings,
                     eligible=None, affinity=None, run_tasks=None):
    """Return [(task_id, worker_id, reason)] for all runs together.

    Order: starving tasks first (waiting longer than starve_after_s, default
    300, oldest first); then tasks on their run's critical path; then the
    remaining critical path length, longest first; then role rank (planner,
    reviewer, integrator before executor); then age, oldest first; then task
    id. dag.critical_path is called once per run. A task is skipped when
    dag.dispatchable finds that one of its files is held by a running task
    or by a task already handed out in this plan. Each worker gets at most
    one task. eligible(worker, task) may return a bool or (ok, reason);
    affinity(worker, task) returns a number, and the highest wins, then
    worker id order. run_tasks optionally maps run_id to every task of that
    run, so the critical path also sees tasks that are not ready yet.
    """
    ready = list(ready_tasks or ())
    workers = sorted(free_workers or (), key=lambda w: str(w["id"]))
    if not ready or not workers:
        return []
    starve_after = _starve_after(settings)

    by_run = {}
    for task in ready:
        by_run.setdefault(task.get("run_id"), []).append(task)
    infos = {
        run_id: _run_info(_run_graph(run_id, tasks, running_tasks, run_tasks))
        for run_id, tasks in by_run.items()
    }

    entries = []
    for task in ready:
        on_path, remaining = infos[task.get("run_id")]
        age = now - _queued_at(task, now)
        starving = age > starve_after
        rest = remaining.get(task["id"], _duration(task))
        key = (
            0 if starving else 1,
            -age if starving else 0,
            0 if task["id"] in on_path else 1,
            -rest,
            _role_rank(task),
            -age,
            str(task["id"]),
        )
        entries.append({"key": key, "task": task, "age": age,
                        "starving": starving, "remaining": rest})
    entries.sort(key=lambda e: e["key"])

    held = []
    for number, task in enumerate(running_tasks or ()):
        held.append({"id": "running:%d" % number, "state": dag.RUNNING,
                     "files": list(task.get("files") or ())})

    plan = []
    free = list(workers)
    for position, entry in enumerate(entries):
        if not free:
            break
        task = entry["task"]
        candidate_id = "ready:%d" % position
        candidate = {"id": candidate_id, "state": "queued",
                     "files": list(task.get("files") or ())}
        if dag.dispatchable(held + [candidate], order=[candidate_id], limit=1) != [candidate_id]:
            continue
        choices = [w for w in free if _is_eligible(eligible, w, task)]
        if not choices:
            continue
        scores = {}
        if affinity is not None:
            for worker in choices:
                value = affinity(dict(worker), dict(task))
                scores[worker["id"]] = value if _number(value) else 0
        choices.sort(key=lambda w: (-scores.get(w["id"], 0), str(w["id"])))
        worker = choices[0]
        nxt = entries[position + 1] if position + 1 < len(entries) else None
        reason = _rule_text(entry, nxt, starve_after)
        if affinity is not None:
            reason += "; worker %s chosen by affinity %s" % (worker["id"], _fmt(scores[worker["id"]]))
        else:
            reason += "; worker %s chosen by worker id order" % worker["id"]
        plan.append((task["id"], worker["id"], reason))
        free.remove(worker)
        held.append(dict(candidate, state=dag.RUNNING))
    return plan


def requeue_orphans(tasks, workers, now=None):
    """Return [{task_id, reason}] for pinned tasks that must go back to the queue.

    A task in state assigned or running is returned when its worker is
    missing, disabled or not alive (state not ready or busy), or when its
    lease_expires is earlier than now (default: the current time). Attempts are never touched and the
    input is never changed; the caller requeues without using an attempt.
    """
    if now is None:
        now = time.time()
    if isinstance(workers, dict):
        lookup = {str(k): v for k, v in workers.items()}
    else:
        lookup = {str(w["id"]): w for w in workers or ()}
    result = []
    for task in tasks or ():
        if task.get("state") not in PINNED_STATES:
            continue
        worker_id = task.get("worker_id")
        worker = lookup.get(str(worker_id)) if worker_id is not None else None
        lease = task.get("lease_expires")
        if worker is None:
            reason = "worker %s is missing" % worker_id
        elif not worker.get("enabled"):
            reason = "worker %s is disabled" % worker_id
        elif worker.get("state") not in ALIVE_WORKER_STATES:
            reason = "worker %s is not ready (state %s)" % (worker_id, worker.get("state"))
        elif _number(lease) and lease < now:
            reason = "lease expired at %s, before now %s" % (_fmt(lease), _fmt(now))
        else:
            continue
        result.append({"task_id": task["id"], "reason": reason})
    return result
import time
