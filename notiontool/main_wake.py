"""Idle wake-up decision for the Main chat.

Pure module: standard library only, no file I/O, no network. Nothing here is
wired yet (NOT WIRED). The caller would pass the returned message and
wake_step_id(...) to NotionAdapter.continue_turn(worker, text, step_id)
(notion.py:1039), which itself refuses to send while the chat is busy.

Field names follow the real records:
- run states: ACTIVE is imported from autopilot.py:33 (approach, planning,
  executing, reviewing, fixing, integrating); 'queued' as in autopilot.py:153.
  Finished states done, blocked, cancelled are part of engine.py:21 TERMINAL.
- run fields: id, state, updated, summary, error, account_id, main_worker
  (engine.py:195-196, updated also at engine.py:278).
- worker fields: id, enabled, state == 'ready', lease ('' when idle),
  account_id (autopilot.py:14-17, autopilot.py:73-78). A running turn sets
  state 'busy' and a lease (engine.py:569); an optional truthy
  'running_turn' flag is also treated as busy.
- task states: 'running' and 'uncertain' (engine.py:30 LIVE_TASK_STATES).
"""

from __future__ import annotations

import copy
import hashlib
import math

from notiontool.autopilot import ACTIVE

FINISHED = frozenset({"done", "blocked", "cancelled"})
BUSY_TASK_STATES = frozenset({"running", "uncertain"})
DEFAULT_COOLDOWN_S = 120
REPORTED_LIMIT = 200
MAX_MESSAGE = 1500
ID_CHARS = 10
TEXT_CHARS = 150

HEAD = "Automatische Meldung vom Harness: Alle Coworker sind fertig."
FOOT = ("Bitte Ergebnisse selbst pr\u00fcfen (Test-Path, Tests) und mit dem "
        "n\u00e4chsten offenen Punkt aus dem Plan weitermachen.")

REASON_DISABLED = "disabled"
REASON_RUN_ACTIVE = "run_active"
REASON_TASK_BUSY = "task_busy"
REASON_MAIN_BUSY = "main_busy"
REASON_COOLDOWN = "cooldown"
REASON_NOTHING_NEW = "nothing_new"
REASON_WAKE = "wake"


def _num(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(result) else result


def _normal_state(state):
    state = state or {}
    reported = state.get("reported_run_ids") or []
    return {
        "last_wake": _num(state.get("last_wake"), 0.0),
        "reported_run_ids": [str(i) for i in reported],
    }


def _run_account(run, workers_by_id):
    account = run.get("account_id")
    if account:
        return account
    main = workers_by_id.get(run.get("main_worker")) or {}
    return main.get("account_id")


def _main_busy(main_worker):
    worker = main_worker or {}
    return (not worker.get("enabled") or worker.get("state") != "ready"
            or bool(worker.get("lease")) or bool(worker.get("running_turn")))


def _one_line(text):
    return " ".join(str(text or "").split())


def _run_line(run):
    text = _one_line(run.get("summary") or run.get("error") or "")[:TEXT_CHARS]
    line = "- " + str(run.get("id", ""))[:ID_CHARS] + " " + str(run.get("state", ""))
    return line + (": " + text if text else "")


def _more_line(count):
    return "\u2026 und %d weitere Runs" % count


def build_message(finished_runs, tasks=None):
    """German wake message, at most MAX_MESSAGE characters.

    The first and the last line are always kept. Run lines that do not fit
    are left out and replaced by one '... und N weitere Runs' line. tasks is
    accepted for the API but not used: file existence is never checked here.
    """
    lines = [_run_line(run) for run in (finished_runs or [])]
    budget = MAX_MESSAGE - len(HEAD) - len(FOOT) - 2
    reserve = len(_more_line(len(lines))) + 1
    kept = []
    used = 0
    for index, line in enumerate(lines):
        last = index == len(lines) - 1
        need = used + len(line) + 1 + (0 if last else reserve)
        if need > budget:
            break
        kept.append(line)
        used += len(line) + 1
    omitted = len(lines) - len(kept)
    if omitted:
        kept.append(_more_line(omitted))
    message = "\n".join([HEAD] + kept + [FOOT])
    if len(message) > MAX_MESSAGE:
        message = HEAD + "\n" + _more_line(len(lines)) + "\n" + FOOT
    return message


def wake_step_id(run_ids):
    """Deterministic step id, independent of order and duplicates."""
    unique = sorted({str(i) for i in (run_ids or [])})
    digest = hashlib.sha256("\n".join(unique).encode("utf-8")).hexdigest()
    return "main_wake:" + digest[:16]


def decide(runs, tasks, workers, main_worker, state, settings, now):
    """Return (message or None, reason, new_state). Inputs are never changed."""
    settings = settings or {}
    current = _normal_state(state)
    if settings.get("main_wake") is False:
        return None, REASON_DISABLED, current

    workers_by_id = {w.get("id"): w for w in (workers or [])}
    account = (main_worker or {}).get("account_id")
    scoped = [r for r in (runs or []) if _run_account(r, workers_by_id) == account]

    if any(r.get("state") in ACTIVE or r.get("state") == "queued" for r in scoped):
        return None, REASON_RUN_ACTIVE, current
    if any((t or {}).get("state") in BUSY_TASK_STATES for t in (tasks or [])):
        return None, REASON_TASK_BUSY, current
    if _main_busy(main_worker):
        return None, REASON_MAIN_BUSY, current

    last_wake = current["last_wake"]
    cooldown = _num(settings.get("main_wake_cooldown_s", DEFAULT_COOLDOWN_S), DEFAULT_COOLDOWN_S)
    if _num(now) - last_wake < cooldown:
        return None, REASON_COOLDOWN, current

    reported = set(current["reported_run_ids"])
    fresh = [r for r in scoped if r.get("state") in FINISHED
             and _num(r.get("updated")) > last_wake and str(r.get("id")) not in reported]
    if not fresh:
        return None, REASON_NOTHING_NEW, current

    fresh.sort(key=lambda r: (-_num(r.get("updated")), str(r.get("id"))))
    new_ids = [str(r.get("id")) for r in fresh]
    new_state = copy.deepcopy(current)
    new_state["last_wake"] = _num(now)
    combined = [i for i in current["reported_run_ids"] if i not in set(new_ids)] + new_ids
    new_state["reported_run_ids"] = combined[-REPORTED_LIMIT:]
    return build_message(fresh, tasks), REASON_WAKE, new_state
