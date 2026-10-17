"""Pure planning helpers for stale worker cleanup.

plan_cleanup decides, for every worker record, whether it is retired, kept,
or keeps its record while its queued tasks are reassigned. count_pool sums
up the pool for the dashboard. Both functions are pure: they never change
their input and have no side effects. Only the standard library is used.

Worker states come from the existing code:
- "ready": new worker records (notion.py:322), after a release
  (engine.py:604, engine.py:610) and after recovery (engine.py:825).
- "busy": set when a lease is taken (engine.py:547).
- "error": set by a failed release (engine.py:777).
- "missing": the thread is gone from Notion (notion.py:329); apply_chat_pool
  skips these records (notion.py:474).
The Main is the worker with purpose "main" (notion.py:478) or role "main".
Tasks are tied to workers through task["worker_id"] (engine.py:540).
"""

READY = "ready"
BUSY = "busy"
MISSING = "missing"
ALIVE_STATES = frozenset({READY, BUSY})
RUNNING_TASK_STATE = "running"
QUEUED_TASK_STATE = "queued"
UNCERTAIN_DUPLICATE_REASON = "duplicate on uncertain thread; kept until liveness is known"


def _is_main(worker):
    return worker.get("role") == "main" or worker.get("purpose") == "main"


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _newness(worker):
    for key in ("created", "last_used"):
        if _number(worker.get(key)):
            return worker[key]
    return 0


def _dead_reason(worker, alive_map):
    """Return why a worker counts as dead, or an empty string if it does not."""
    thread = worker.get("thread_id")
    if worker.get("state") == MISSING:
        return "its record is missing (state missing)"
    if not thread:
        return "it has no thread_id"
    if alive_map.get(thread) is False:
        return "its thread %s is not alive" % thread
    return ""


def plan_cleanup(workers, tasks, chats, threads_alive):
    """Return [{worker_id, action, reason}] sorted by worker_id.

    action is "retire", "keep" or "reassign_tasks". A Main that a chat points
    to, or a worker with a running task, is never retired; if it is dead it
    gets reassign_tasks, which names only its queued tasks. Otherwise a
    worker is retired when it is dead: its record is missing (state
    "missing"), it has no thread_id, or threads_alive says False for its
    thread. Among the workers on one thread that are not dead, one winner is
    chosen (chat pointer first, then a running task, then the newest record,
    then the highest id); a dead worker never wins. On an alive thread the
    other workers are retired as duplicates. A thread whose liveness is None
    or not listed is uncertain: its workers, duplicates included, are kept.
    """
    workers = [w for w in (workers or ()) if isinstance(w, dict) and w.get("id") is not None]
    alive_map = dict(threads_alive or {})
    pointed = {str(c.get("main_worker_id")) for c in (chats or ())
               if isinstance(c, dict) and c.get("main_worker_id")}
    running, queued = {}, {}
    for task in tasks or ():
        if not isinstance(task, dict) or task.get("worker_id") in (None, ""):
            continue
        owner = str(task["worker_id"])
        if task.get("state") == RUNNING_TASK_STATE:
            running.setdefault(owner, []).append(str(task.get("id")))
        elif task.get("state") == QUEUED_TASK_STATE:
            queued.setdefault(owner, []).append(str(task.get("id")))

    def rank(worker):
        wid = str(worker["id"])
        return (wid in pointed, wid in running, _newness(worker), wid)

    winners = {}
    for worker in workers:
        thread = worker.get("thread_id")
        if not thread or _dead_reason(worker, alive_map):
            continue
        best = winners.get(thread)
        if best is None or rank(worker) > rank(best):
            winners[thread] = worker

    result = []
    for worker in workers:
        wid = str(worker["id"])
        thread = worker.get("thread_id")
        alive = alive_map.get(thread) if thread else False
        dead = _dead_reason(worker, alive_map)
        protected = []
        if wid in pointed:
            protected.append("a chat points to it as Main")
        if wid in running:
            protected.append("it has running task %s" % ", ".join(sorted(running[wid])))
        if protected:
            why = " and ".join(protected)
            if dead:
                moved = sorted(queued.get(wid, ()))
                listed = ", ".join(moved) if moved else "none"
                reason = "never retired because %s, but %s; reassign queued tasks: %s" % (why, dead, listed)
                result.append({"worker_id": wid, "action": "reassign_tasks", "reason": reason})
            else:
                result.append({"worker_id": wid, "action": "keep", "reason": "kept because %s" % why})
            continue
        if dead:
            result.append({"worker_id": wid, "action": "retire", "reason": "retired because %s" % dead})
            continue
        winner = winners.get(thread)
        if winner is not None and winner is not worker:
            if alive is True:
                reason = "retired because worker %s holds the same thread %s" % (winner["id"], thread)
                result.append({"worker_id": wid, "action": "retire", "reason": reason})
            else:
                result.append({"worker_id": wid, "action": "keep", "reason": UNCERTAIN_DUPLICATE_REASON})
            continue
        if alive is True:
            reason = "kept because its thread %s is alive" % thread
        else:
            reason = "kept because the state of thread %s is uncertain" % thread
        result.append({"worker_id": wid, "action": "keep", "reason": reason})
    result.sort(key=lambda entry: entry["worker_id"])
    return result


def count_pool(workers):
    """Return {main, coworkers_ready, coworkers_busy, stale}.

    A Main in state ready or busy counts as main. A coworker in state ready
    counts as ready, in state busy as busy. Every other state (missing,
    error, an unknown value or no state) counts as stale, for the Main too.
    """
    counts = {"main": 0, "coworkers_ready": 0, "coworkers_busy": 0, "stale": 0}
    for worker in workers or ():
        if not isinstance(worker, dict):
            continue
        state = worker.get("state")
        if state not in ALIVE_STATES:
            counts["stale"] += 1
        elif _is_main(worker):
            counts["main"] += 1
        elif state == READY:
            counts["coworkers_ready"] += 1
        else:
            counts["coworkers_busy"] += 1
    return counts
