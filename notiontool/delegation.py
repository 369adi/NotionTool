"""Pure checks for delegating a task into child tasks.

A parent task (the engine.py add_task shape: id, run_id, role, deps, files,
state, plus an optional depth) may ask to split its work into child tasks.
validate_delegation checks the request and returns the child specs and the
update for the parent. resume_ready decides when the parent can continue.

No side effects: inputs are never changed, nothing is stored, and there is
no network or file I/O. Not wired into engine.py yet (NOT WIRED).
"""

import json

from notiontool import dag
from notiontool.task_cache import normalize_path

MAX_DEPTH = 2
MAX_CHILDREN = 4
DEFAULT_MAX_DELEGATED_PER_RUN = 12
ROLES = ("planner", "executor", "reviewer", "integrator")
DONE = "done"
# A blocked or cancelled child cannot finish on its own, so it counts as failed.
FAILED_STATES = frozenset({"failed", "blocked", "cancelled"})


class DelegationError(Exception):
    """Raised with an English reason when a delegation request is refused."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def child_id(parent_id, index):
    """Deterministic id of the child at the zero-based index."""
    return "%s.c%d" % (parent_id, index + 1)


def _deps(task):
    # engine.py stores deps; dag.py reads depends_on. Accept both.
    deps = task.get("deps")
    if deps is None:
        deps = task.get("depends_on")
    return list(deps or ())


def _depth(task):
    value = task.get("depth") or 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DelegationError("The parent task depth must be a non-negative integer.")
    return value


def _run_limit(settings):
    value = (settings or {}).get("max_delegated_per_run", DEFAULT_MAX_DELEGATED_PER_RUN)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DelegationError("The setting max_delegated_per_run must be a non-negative integer.")
    return value


def _claim_owners(claims):
    """Return {case-folded normalized path: set of owner task ids}."""
    owners = {}
    for path, owner in (claims or {}).items():
        key = normalize_path(path).casefold()
        if key:
            owners.setdefault(key, set()).add(owner)
    return owners


def validate_delegation(parent_task, request, run_tasks, claims, settings=None):
    """Return (children_specs, parent_update) or raise DelegationError.

    request is {"children": [{id or title, role, prompt, files, deps}]}.
    A child's deps may name sibling children (by their id or title) or
    existing run tasks. claims maps a file path to the task id holding it.
    """
    parent = parent_task or {}
    parent_id = parent.get("id")
    if not parent_id:
        raise DelegationError("The parent task has no id.")
    children = (request or {}).get("children")
    if not isinstance(children, (list, tuple)) or not children:
        raise DelegationError("The delegation request needs a non-empty children list.")
    if len(children) > MAX_CHILDREN:
        raise DelegationError("A parent may delegate at most %d children; the request has %d."
                              % (MAX_CHILDREN, len(children)))
    depth = _depth(parent) + 1
    if depth > MAX_DEPTH:
        raise DelegationError("Delegation depth would be %d; the limit is %d." % (depth, MAX_DEPTH))

    tasks = list(run_tasks or ())
    known = {t.get("id") for t in tasks}
    already = sum(1 for t in tasks if t.get("parent_id"))
    limit = _run_limit(settings)
    if already + len(children) > limit:
        raise DelegationError("The run already has %d delegated children; %d more would exceed "
                              "max_delegated_per_run (%d)." % (already, len(children), limit))

    ids = [child_id(parent_id, index) for index in range(len(children))]
    for cid in ids:
        if cid in known:
            raise DelegationError("Child id %s already exists in the run." % cid)

    local = {}
    for index, child in enumerate(children):
        if not isinstance(child, dict):
            raise DelegationError("Child %d is not an object." % (index + 1))
        name = child.get("id") or child.get("title")
        if not name:
            raise DelegationError("Child %d needs an id or a title." % (index + 1))
        if name in local:
            raise DelegationError("Two children share the name %s." % name)
        local[name] = ids[index]

    owners = _claim_owners(claims)
    specs = []
    for index, child in enumerate(children):
        cid = ids[index]
        role = child.get("role") or "executor"
        if role not in ROLES:
            raise DelegationError("Child %s has the unknown role %s." % (cid, role))
        prompt = child.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise DelegationError("Child %s needs a non-empty prompt." % cid)
        raw_files = child.get("files") or ()
        if isinstance(raw_files, str):
            raise DelegationError("Child %s must list its files as a list, not a string." % cid)
        files = dag.task_files({"files": list(raw_files)})
        for path in files:
            others = owners.get(path.casefold(), set()) - {parent_id, None, ""}
            if others:
                holder = sorted(str(owner) for owner in others)[0]
                raise DelegationError("File %s is claimed by task %s, not by the parent." % (path, holder))
        deps = []
        for dep in child.get("deps") or ():
            if dep in local:
                target = local[dep]
            elif dep in known:
                target = dep
            else:
                raise DelegationError("Child %s depends on the unknown task %s." % (cid, dep))
            if target not in deps:
                deps.append(target)
        specs.append(dict(id=cid, parent_id=parent_id, run_id=parent.get("run_id", ""), role=role,
                          title=str(child.get("title") or child.get("id")), prompt=prompt,
                          files=files, deps=deps, depth=depth, state="queued"))

    # The parent waits for its children, so it gets an edge to each child.
    # A child that (directly or through run tasks) waits for the parent is a cycle.
    graph = []
    parent_in_run = False
    for task in tasks:
        deps = _deps(task)
        if task.get("id") == parent_id:
            parent_in_run = True
            deps = deps + ids
        graph.append({"id": task.get("id"), "depends_on": deps})
    if not parent_in_run:
        graph.append({"id": parent_id, "depends_on": _deps(parent) + ids})
    graph.extend({"id": spec["id"], "depends_on": list(spec["deps"])} for spec in specs)
    try:
        cycle = dag.find_cycle(graph)
    except ValueError as exc:
        raise DelegationError("The run tasks are not valid: %s." % exc)
    if cycle:
        raise DelegationError("The delegation would create a dependency cycle: %s."
                              % " -> ".join(str(item) for item in cycle))

    parent_update = {"state": "waiting_children", "children": list(ids), "release_worker": True}
    return specs, parent_update


def _result_text(child):
    result = child.get("result")
    if isinstance(result, dict):
        summary = result.get("summary")
        return summary.strip() if isinstance(summary, str) else json.dumps(result, sort_keys=True)
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except ValueError:
            return result.strip()
        if isinstance(data, dict) and isinstance(data.get("summary"), str):
            return data["summary"].strip()
        return result.strip()
    return "" if result is None else str(result)


def resume_ready(parent, children):
    """Return (ready, info) for a parent in state waiting_children.

    - Any failed, blocked or cancelled child: (False, {parent_update:
      {state: needs_repair, failed_children: [ids]}}). The run is not failed.
    - Every child done: (True, {summary, parent_update: {state: queued}}).
      The summary merges the child results in child order, each with its id.
    - Otherwise: (False, {pending: [ids]}).
    Child order is parent["children"] when present, else the given order.
    """
    items = list(children or ())
    if not items:
        raise DelegationError("The parent has no children to wait for.")
    by_id = {child.get("id"): child for child in items}
    order = list((parent or {}).get("children") or ()) or [child.get("id") for child in items]
    failed = [cid for cid in order if cid in by_id and by_id[cid].get("state") in FAILED_STATES]
    if failed:
        return False, {"parent_update": {"state": "needs_repair", "failed_children": failed}}
    pending = [cid for cid in order if cid not in by_id or by_id[cid].get("state") != DONE]
    if pending:
        return False, {"pending": pending}
    parts = ["[%s] %s" % (cid, _result_text(by_id[cid]) or "(no result)") for cid in order]
    return True, {"summary": "\n\n".join(parts), "parent_update": {"state": "queued"}}
