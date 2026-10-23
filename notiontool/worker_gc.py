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

plan_worktree_reconcile (with parse_worktree_porcelain) decides which git
worktrees can go: only those whose tasks are all finished for longer than
max_age_days and that no live or reviewing task still needs. It is pure in
the same way; PatchMode.reconcile_worktrees does the removal.
"""
import os

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
    pointed = {str(c.get("main_worker")) for c in (chats or ())
               if isinstance(c, dict) and c.get("main_worker")}
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


TERMINAL_TASK_STATES = frozenset({"done", "failed", "blocked", "cancelled"})  # engine.TERMINAL
PENDING_PATCH_STATES = frozenset({"waiting", "merging"})
BASELINE_PREFIX = "_main-"  # PatchMode.main_baseline copies of clean main
DAY_S = 86400.0


def parse_worktree_porcelain(text):
    """Parse `git worktree list --porcelain` into a list of dicts.

    Each entry has path, head, branch (without refs/heads/), locked,
    lock_reason, prunable, bare and detached. The first entry is the main
    worktree.
    """
    entries, current = [], None
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            current = None
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": value, "head": "", "branch": "", "locked": False, "lock_reason": "",
                       "prunable": False, "bare": False, "detached": False}
            entries.append(current)
        elif current is None:
            continue
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value[len("refs/heads/"):] if value.startswith("refs/heads/") else value
        elif key == "locked":
            current["locked"], current["lock_reason"] = True, value
        elif key in ("prunable", "bare", "detached"):
            current[key] = True
    return entries


def _norm_path(path):
    return os.path.normcase(os.path.normpath(str(path))) if path else ""


def _is_live(task):
    return str(task.get("state") or "") not in TERMINAL_TASK_STATES


def _task_names(task):
    names = set()
    for key in ("id", "task_id", "step_id"):
        if task.get(key) not in (None, ""):
            names.add(("id", str(task[key])))
    if task.get("patch_branch"):
        names.add(("branch", str(task["patch_branch"])))
    if task.get("patch_worktree"):
        names.add(("path", _norm_path(task["patch_worktree"])))
    return names


def _entry_names(entry):
    path = entry.get("path") or ""
    names = {("path", _norm_path(path)), ("id", os.path.basename(os.path.normpath(path)))}
    branch = entry.get("branch") or ""
    if branch:
        names.add(("branch", branch))
        if branch.startswith("task/"):
            names.add(("id", branch[len("task/"):]))
    if entry.get("lock_reason"):
        names.add(("id", entry["lock_reason"]))
    return names


def _stamp(task):
    for key in ("finished", "updated", "created"):
        if _number(task.get(key)):
            return task[key]
    return None


def plan_worktree_reconcile(worktrees, tasks, now, max_age_days=7, main_path=None, worktrees_dir=None,
                            ages=None):
    """Return [{path, branch, action, reason, tasks}] in listing order.

    action is "remove" or "keep". worktrees is the porcelain text or its
    parsed entries; ages maps a worktree path to a time (e.g. the folder
    mtime) used when no task has one. A worktree is removed only when tasks
    name it (worktree path, folder name, branch or lock reason), all of them
    are finished (done, failed, blocked, cancelled), none has a pending
    patch state, no live review task reads it (review_of names one of them)
    and the newest finished time is older than max_age_days. Old "_main-*"
    baseline copies need no task. Always kept: the main worktree, anything
    outside worktrees_dir, a worktree no task names, one locked by something
    else than a task, and one whose age is unknown.
    """
    entries = parse_worktree_porcelain(worktrees) if isinstance(worktrees, str) else list(worktrees or ())
    tasks = [t for t in (tasks or ()) if isinstance(t, dict)]
    ages = {_norm_path(k): v for k, v in (ages or {}).items()}
    limit = float(max_age_days) * DAY_S
    main = _norm_path(main_path)
    inside = _norm_path(worktrees_dir)
    index, reviews = {}, {}
    for task in tasks:
        for name in _task_names(task):
            index.setdefault(name, []).append(task)
        if task.get("review_of") not in (None, "") and _is_live(task):
            reviews.setdefault(str(task["review_of"]), []).append(str(task.get("id")))
    result = []
    for number, entry in enumerate(entries):
        path = entry.get("path") or ""
        norm = _norm_path(path)
        item = {"path": path, "branch": entry.get("branch") or "", "action": "keep", "reason": "", "tasks": []}
        result.append(item)
        if number == 0 or entry.get("bare") or (main and norm == main):
            item["reason"] = "kept because the main worktree is never removed"
            continue
        if inside and not norm.startswith(inside.rstrip(os.sep) + os.sep):
            item["reason"] = "kept because it is not inside %s" % worktrees_dir
            continue
        named = []
        for name in _entry_names(entry):
            for task in index.get(name, ()):
                if not any(task is seen for seen in named):
                    named.append(task)
        ids = sorted({str(t.get("id")) for t in named if t.get("id") not in (None, "")})
        item["tasks"] = ids
        live = sorted({str(t.get("id")) for t in named if _is_live(t)})
        if live:
            item["reason"] = "kept because live task %s names it" % ", ".join(live)
            continue
        readers = sorted({r for i in ids for r in reviews.get(i, ())})
        if readers:
            item["reason"] = "kept because live review task %s reads it" % ", ".join(readers)
            continue
        pending = sorted({str(t.get("id")) for t in named if t.get("patch_state") in PENDING_PATCH_STATES})
        if pending:
            item["reason"] = "kept because the patch of task %s still waits for its merge" % ", ".join(pending)
            continue
        lock_reason = entry.get("lock_reason") or ""
        if entry.get("locked") and ("id", lock_reason) not in index:
            item["reason"] = ("kept because it is locked by something else than a task (%s)"
                              % (lock_reason or "no reason"))
            continue
        baseline = os.path.basename(os.path.normpath(path)).startswith(BASELINE_PREFIX)
        if not named and not baseline:
            item["reason"] = "kept because no task names it"
            continue
        stamps = [s for s in (_stamp(t) for t in named) if s is not None]
        stamp = max(stamps) if stamps else ages.get(norm)
        if not _number(stamp):
            item["reason"] = "kept because its age is unknown"
            continue
        days = (now - stamp) / DAY_S
        if now - stamp < limit:
            item["reason"] = "kept because it finished %.1f days ago (limit %s days)" % (days, max_age_days)
            continue
        item["action"] = "remove"
        what = ("its tasks %s are all finished" % ", ".join(ids)) if ids else "it is an old main baseline copy"
        item["reason"] = "removed because %s, newest %.1f days ago" % (what, days)
    return result
