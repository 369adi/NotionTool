"""Idle wake-up decision for the Main chat.

The functions decide, build_message and wake_step_id are pure helpers (standard
library only, no file I/O, no network) and are still NOT WIRED.
class MainWake (end of file) is the live loop started by server.py: it tells the
Operator Main chat once when a delegated Cowork run is completely finished.

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
import threading
import time

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


# ---- MainWake: live loop (started by server.py) ---------------------------------------------

WAKE_KIND = "main_wakes"  # store kind; record id = run id + ':' + run state
TERMINAL_RUN = frozenset({"done", "failed", "cancelled", "blocked", "awaiting_main"})  # canary.py:46
OPEN_TASK_STATES = frozenset({"queued", "ready", "retry", "running", "executing", "fixing", "uncertain",
                              "recovering", "cancelling"})
NOT_ACCEPTED_STATES = frozenset({"failed", "cancelled", "blocked"})
DEFAULT_INTERVAL_S = 15.0
BASELINE_AGE_S = 1800.0  # finished runs older than 30 min are marked as notified at start, never sent
MIN_GAP_S = 60.0  # at most one wake message per Main chat per minute
WAKE_MAX_RUNS = 5
WAKE_SUMMARY_CHARS = 150
WAKE_HEAD = "NotionTool (automatic): Cowork run finished."
WAKE_HEAD_MANY = "NotionTool (automatic): Cowork runs finished."
WAKE_NEXT = ("Next for the Main: read GET /api/run?id=%s, check diffs and reviews, run tests, commit, "
             "restart, live test, confirm awaiting_main runs.")


def wake_run_line(run, tasks):
    """One line per run: id, state, summary, tasks done/total, tasks that were not accepted."""
    done = sum(1 for t in tasks if t.get("state") == "done")
    bad = [str(i) for i in (run.get("needs_main") or [])]
    bad += [str(t.get("id")) for t in tasks if t.get("state") in NOT_ACCEPTED_STATES]
    bad = list(dict.fromkeys(bad))
    summary = _one_line(run.get("summary") or run.get("error") or "")[:WAKE_SUMMARY_CHARS]
    return "- %s %s: %s; tasks %d/%d, not accepted %s" % (
        run.get("id", ""), run.get("state", ""), summary or "no summary", done, len(tasks),
        ", ".join(bad) if bad else "none")


def build_wake_message(items):
    """items: [(run, tasks)]. One short English message for all runs of one chat."""
    shown = items[:WAKE_MAX_RUNS]
    lines = [WAKE_HEAD if len(items) == 1 else WAKE_HEAD_MANY] + [wake_run_line(r, t) for r, t in shown]
    if len(items) > len(shown):
        lines.append("... and %d more runs" % (len(items) - len(shown)))
    lines.append(WAKE_NEXT % (items[0][0].get("id", "<run id>") if len(items) == 1 else "<run id>"))
    return "\n".join(lines)


class MainWake:
    """Tells the operator Main chat once when a delegated (operator) run is completely finished.

    Finished = run.operator, terminal state, no confirmed_at and no open task (so reviews and fix rounds
    are done). Each run+state is recorded in store kind 'main_wakes' BEFORE it is sent, so a crash never
    leads to a resend. Only sends into the existing Main thread; never creates chats, agents or triggers.
    """

    def __init__(self, store, adapter, engine, clock=None):
        self.store, self.adapter, self.engine = store, adapter, engine
        self.clock = clock or time.time
        self.baselined = False
        self.last_sent = {}  # thread id -> time of the last message
        self.waited = set()  # record ids already reported as waiting for a busy chat
        self.last_error = {}  # thread id -> last reported error text

    # ---- settings ----
    def _settings(self):
        try:
            return self.store.settings() or {}
        except Exception:
            return {}

    def enabled(self):
        return bool(self._settings().get("main_wake_enabled", True))

    def interval(self):
        return max(1.0, _num(self._settings().get("main_wake_interval_s", DEFAULT_INTERVAL_S), DEFAULT_INTERVAL_S))

    # ---- decision ----
    @staticmethod
    def record_id(run):
        return "%s:%s" % (run.get("id"), run.get("state"))

    def finished(self):
        """[(run, tasks)] for completely finished operator runs that are not confirmed."""
        by_run = {}
        for task in self.store.all("tasks"):
            by_run.setdefault(task.get("run_id"), []).append(task)
        found = []
        for run in self.store.all("runs"):
            if not run.get("operator") or run.get("state") not in TERMINAL_RUN or run.get("confirmed_at"):
                continue
            tasks = by_run.get(run.get("id"), [])
            if any(t.get("state") in OPEN_TASK_STATES for t in tasks):
                continue
            found.append((run, tasks))
        return found

    def baseline(self):
        """Old finished runs count as notified without a message (no spam after a restart)."""
        now = _num(self.clock())
        for run, _ in self.finished():
            if self.store.get(WAKE_KIND, self.record_id(run)):
                continue
            if now - _num(run.get("updated")) > BASELINE_AGE_S:
                self.store.put(WAKE_KIND, dict(id=self.record_id(run), run_id=run["id"], state=run["state"],
                                               status="baseline", at=now))
        self.baselined = True

    def thread_of(self, run):
        """(account id, Notion thread id) of the Main chat of a run; main_worker is '<account>:<thread id>'."""
        worker_id = str(run.get("main_worker") or "")
        worker = self.store.get("workers", worker_id) or {}
        account, _, tail = worker_id.partition(":")
        return (worker.get("account_id") or run.get("account_id") or account,
                worker.get("thread_id") or tail)

    @staticmethod
    def chat_busy(transcript):
        """Same idle test as main_handoff_ops.chat_busy."""
        session = transcript.get("session") or {}
        return bool(transcript.get("session_pending")) or session.get("status") not in (None, "idle", "completed")

    def _event(self, kind, message="", **fields):
        try:
            self.store.event(kind, message=message, **fields)
        except Exception:
            pass

    def tick(self):
        """One pass; returns the list of run ids that were sent."""
        if not self.baselined:
            self.baseline()
        if not self.enabled():
            return []
        now = _num(self.clock())
        groups = {}
        for run, tasks in self.finished():
            if self.store.get(WAKE_KIND, self.record_id(run)):
                continue
            chat = self.store.get("chats", run.get("chat_id")) or {}
            if chat.get("state") == "released":
                continue  # the thread belongs to the user now
            account, thread = self.thread_of(run)
            if not thread:
                continue
            groups.setdefault((account, thread), []).append((run, tasks))
        sent = []
        for (account, thread), items in groups.items():
            if now - self.last_sent.get(thread, -MIN_GAP_S) < MIN_GAP_S:
                continue
            try:
                client = self.adapter.client(account)
                if self.chat_busy(client.transcript(thread) or {}):
                    for run, _ in items:
                        key = self.record_id(run)
                        if key not in self.waited:
                            self.waited.add(key)
                            self._event("main_wake_wait_busy", "Main chat is busy; wake message waits",
                                        run_id=run["id"], account_id=account)
                    continue
            except Exception as exc:
                self._failed(thread, exc)
                continue
            items.sort(key=lambda it: (_num(it[0].get("updated")), str(it[0].get("id"))))
            records = [dict(id=self.record_id(r), run_id=r["id"], state=r["state"], status="attempted",
                            thread_id=thread, at=now) for r, _ in items]
            for record in records:
                self.store.put(WAKE_KIND, record)  # recorded before sending: a crash never leads to a resend
            self.last_sent[thread] = now
            ids = [r["id"] for r, _ in items]
            try:
                self._send(client, thread, build_wake_message(items))
            except Exception as exc:
                for record in records:
                    self.store.put(WAKE_KIND, dict(record, status="failed", error=str(exc)[:200]))
                self._failed(thread, exc)
                continue
            for record in records:
                self.store.put(WAKE_KIND, dict(record, status="sent"))
            self._event("main_wake_sent", ", ".join(ids), run_ids=ids, account_id=account)
            sent.extend(ids)
        return sent

    def _failed(self, thread, exc):
        text = (type(exc).__name__ + ": " + str(exc))[:200]
        if self.last_error.get(thread) != text:  # one event per distinct error, not one per tick
            self.last_error[thread] = text
            self._event("main_wake_failed", text)

    def _send(self, client, thread, text):
        """Own small sender, same steps as main_handoff_ops.send_prompt."""
        from .notion import user_effort
        step_id = self.adapter.new_id(client.info["space_id"])
        # Main chats keep the user's reasoning (else high), never the model default.
        body = self.adapter.body(client, thread, step_id, text, client.model(), computer=True,
                                 effort=user_effort(client, thread))
        try:
            for _ in client.transport.stream("runInferenceTranscript", body, client.headers(), timeout=180):
                pass
        except OSError:
            pass  # the stream ended early; the prompt was accepted (as in main_handoff_ops)

    def loop(self):
        stop = getattr(self.engine, "stop_event", None)
        while not (stop is not None and stop.is_set()):
            try:
                self.tick()
            except Exception as exc:
                self._failed("loop", exc)
            if stop is not None:
                if stop.wait(self.interval()):
                    break
            else:
                time.sleep(self.interval())
