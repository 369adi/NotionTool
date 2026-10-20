"""Autopilot decision for the batch backlog.

Pure module: standard library only, no file I/O, no imports from other
notiontool modules. Nothing here is wired yet (NOT WIRED).

Field names follow the real records (engine.py sha256 79CA1BED...):
- run states: ACTIVE at engine.py:20, 'paused' counted with ACTIVE at
  engine.py:185 and :222, 'queued' at engine.py:184 and :191.
- 'blocked' run state (engine.py:21, set at :882, :914, :925) waits for the
  operator: retry_blocked (engine.py:323-333) or resume_remote
  (engine.py:299-321).
- max_runs_per_chat default 3, clamped to 1..6, invalid gives 1
  (engine.py:212-216; server.py:630 clamps 1..6).
- worker: purpose == 'worker' (engine.py:187, :480), enabled
  (engine.py:187, :477), state == 'ready' (engine.py:477), lease cleared to ''
  when idle (engine.py:607), cooldown_until (engine.py:478), account_id
  (engine.py:187).
- account: state == 'ready' (engine.py:186, :496), cooldown_until
  (engine.py:498).
- request_id: cut to 100 characters and deduplicated against runs
  (engine.py:156-163); /api/send passes it through (server.py:589-590).

Backlog item contract: {id, goal, priority, deps, approvals, request_id,
state (queued, submitted, done or failed), submitted_run_id, created,
submitted_at}.
"""

from __future__ import annotations

import copy
import math

ACTIVE = frozenset({"approach", "planning", "executing", "reviewing", "fixing", "integrating"})
OPERATOR_WAIT = frozenset({"blocked"})
USED_ITEM_STATES = frozenset({"submitted", "done", "failed"})
DEFAULT_MIN_FREE_WORKERS = 2
DEFAULT_COOLDOWN_S = 60
DEFAULT_RUNS_PER_CHAT = 3
REQUEST_ID_LIMIT = 100


def _num(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(result) else result


def runs_per_chat(settings) -> int:
    """Same rule as Engine._runs_per_chat (engine.py:212-216)."""
    try:
        return max(1, min(int((settings or {}).get("max_runs_per_chat", DEFAULT_RUNS_PER_CHAT)), 6))
    except (TypeError, ValueError):
        return 1


def _request_key(value) -> str:
    return str(value or "")[:REQUEST_ID_LIMIT]


def free_workers(workers, accounts, now) -> list:
    """Coworkers that are ready and idle; the Main worker never counts.

    The worker fields always decide: purpose 'worker', enabled, state
    'ready', empty lease and cooldown_until not after now. When accounts is
    None the account check is skipped. When accounts is passed, the worker's
    account must exist, be 'ready' and have cooldown_until not after now.
    """
    by_id = None if accounts is None else {a.get("id"): a for a in accounts}
    result = []
    for worker in workers or []:
        if not (worker.get("purpose") == "worker" and worker.get("enabled")
                and worker.get("state") == "ready" and not worker.get("lease")
                and _num(worker.get("cooldown_until")) <= now):
            continue
        if by_id is not None:
            account = by_id.get(worker.get("account_id"))
            if (account is None or account.get("state") != "ready"
                    or _num(account.get("cooldown_until")) > now):
                continue
        result.append(worker)
    return result


def normalize_files(files) -> list:
    seen, result = set(), []
    for name in files or []:
        text = str(name).strip().replace("\\", "/")
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def build_goal(item) -> str:
    """Goal text plus a final 'Operator approval for: ...' line when approvals exist."""
    goal = str((item or {}).get("goal") or "").strip()
    files = normalize_files((item or {}).get("approvals"))
    if files:
        return goal + "\nOperator approval for: " + ", ".join(files)
    return goal


def _sort_key(item):
    created = _num(item.get("created"), math.inf)
    return (-_num(item.get("priority")), created, str(item.get("id", "")))


def pick_item(backlog_items, runs):
    """Return (item or None, reason) for the next queued item whose deps are done."""
    items = list(backlog_items or [])
    states = {item.get("id"): item.get("state") for item in items}
    used = {_request_key(i.get("request_id")) for i in items if i.get("state") in USED_ITEM_STATES}
    used |= {_request_key(r.get("request_id")) for r in runs or []}
    used.discard("")
    queued = [i for i in items if i.get("state") == "queued"]
    if not queued:
        return None, "No queued backlog item."
    skipped = 0
    for item in sorted(queued, key=_sort_key):
        key = _request_key(item.get("request_id"))
        if not key or key in used or not str(item.get("goal") or "").strip():
            skipped += 1
            continue
        if all(states.get(dep) == "done" for dep in item.get("deps") or []):
            return item, "Item " + str(item.get("id")) + " is ready."
    if skipped == len(queued):
        return None, "Every queued item lacks a goal or a new request_id."
    return None, "No queued item has all its dependencies done."


def should_submit(backlog_items, workers, runs, settings, now, accounts=None):
    """Return (item with final_goal, reason) or (None, reason). Inputs are not changed.

    accounts is the optional list of account records. When it is left out,
    only the worker fields decide whether a coworker is free (see
    free_workers). When it is passed, a worker whose account is missing, not
    ready or cooling down does not count. autopilot_min_free_workers is
    clamped to at least 0. settings may hold autopilot_chat_id; then
    only that chat's runs count toward max_runs_per_chat, as in
    engine.py:183-185.
    """
    settings = settings or {}
    if settings.get("autopilot") is not True:
        return None, "Autopilot is off."
    all_runs = list(runs or [])
    blocked = [r for r in all_runs if r.get("state") in OPERATOR_WAIT]
    if blocked:
        return None, "Run " + str(blocked[0].get("id")) + " is blocked and waits for the operator."
    chat_id = settings.get("autopilot_chat_id")
    scoped = [r for r in all_runs if not chat_id or r.get("chat_id") == chat_id]
    if any(r.get("state") == "queued" for r in scoped):
        return None, "A run is already queued, so a new one would only wait."
    active = sum(r.get("state") in ACTIVE or r.get("state") == "paused" for r in scoped)
    limit = runs_per_chat(settings)
    if active >= limit:
        return None, "Active runs (%d) are at max_runs_per_chat (%d)." % (active, limit)
    needed = max(0, int(_num(settings.get("autopilot_min_free_workers", DEFAULT_MIN_FREE_WORKERS), DEFAULT_MIN_FREE_WORKERS)))
    free = len(free_workers(workers, accounts, now))
    if free < needed:
        return None, "Only %d free coworkers, %d needed." % (free, needed)
    cooldown = _num(settings.get("autopilot_cooldown_s", DEFAULT_COOLDOWN_S), DEFAULT_COOLDOWN_S)
    stamps = [_num(i.get("submitted_at"), None) for i in backlog_items or [] if i.get("submitted_at") is not None]
    stamps = [s for s in stamps if s is not None]
    if stamps and now - max(stamps) < cooldown:
        return None, "Cooldown active: last submit %.0f s ago, %.0f s needed." % (now - max(stamps), cooldown)
    item, reason = pick_item(backlog_items, all_runs)
    if item is None:
        return None, reason
    result = copy.deepcopy(item)
    result["final_goal"] = build_goal(item)
    return result, reason
