"""Tests for notiontool.main_wake (pure, in-memory data only)."""

import copy
import unittest

from notiontool import autopilot, main_wake
from notiontool.main_wake import FOOT, HEAD, build_message, decide, wake_step_id

NOW = 10000.0


def main(**extra):
    worker = dict(id="main1", purpose="main", enabled=True, state="ready", lease="", account_id="acc1")
    worker.update(extra)
    return worker


def run(rid, state="done", updated=5000.0, account_id="acc1", **extra):
    item = dict(id=rid, state=state, updated=updated, account_id=account_id, main_worker="main1",
                summary="Summary of " + rid, error="")
    item.update(extra)
    return item


def call(runs, tasks=None, worker=None, state=None, settings=None, now=NOW):
    worker = worker or main()
    return decide(runs, tasks or [], [worker], worker, state if state is not None else {},
                  settings or {}, now)


class DecideTests(unittest.TestCase):
    def test_wakes_when_all_finished(self):
        message, reason, state = call([run("r1")])
        self.assertEqual(reason, "wake")
        self.assertTrue(message.startswith(HEAD))
        self.assertEqual(state["last_wake"], NOW)
        self.assertEqual(state["reported_run_ids"], ["r1"])

    def test_no_wake_while_run_active(self):
        message, reason, _ = call([run("r1"), run("r2", state="executing")])
        self.assertIsNone(message)
        self.assertEqual(reason, "run_active")

    def test_no_wake_while_run_queued(self):
        message, reason, _ = call([run("r1"), run("r2", state="queued")])
        self.assertIsNone(message)
        self.assertEqual(reason, "run_active")

    def test_every_active_state_blocks(self):
        for active in sorted(autopilot.ACTIVE):
            _, reason, _ = call([run("r1"), run("r2", state=active)])
            self.assertEqual(reason, "run_active", active)

    def test_active_set_imported_from_autopilot(self):
        self.assertIs(main_wake.ACTIVE, autopilot.ACTIVE)

    def test_no_wake_while_task_uncertain(self):
        message, reason, _ = call([run("r1")], tasks=[dict(id="t1", state="uncertain")])
        self.assertIsNone(message)
        self.assertEqual(reason, "task_busy")

    def test_no_wake_while_task_running(self):
        _, reason, _ = call([run("r1")], tasks=[dict(id="t1", state="running")])
        self.assertEqual(reason, "task_busy")

    def test_no_wake_when_main_busy(self):
        _, reason, _ = call([run("r1")], worker=main(state="busy"))
        self.assertEqual(reason, "main_busy")

    def test_no_wake_when_main_has_lease(self):
        _, reason, _ = call([run("r1")], worker=main(lease="L1"))
        self.assertEqual(reason, "main_busy")

    def test_no_wake_when_main_has_running_turn(self):
        _, reason, _ = call([run("r1")], worker=main(running_turn=True))
        self.assertEqual(reason, "main_busy")

    def test_no_wake_when_main_disabled(self):
        _, reason, _ = call([run("r1")], worker=main(enabled=False))
        self.assertEqual(reason, "main_busy")

    def test_cooldown(self):
        state = {"last_wake": NOW - 60, "reported_run_ids": []}
        message, reason, _ = call([run("r1", updated=NOW - 10)], state=state)
        self.assertIsNone(message)
        self.assertEqual(reason, "cooldown")
        _, reason, _ = call([run("r1", updated=NOW - 10)], state=state, settings={"main_wake_cooldown_s": 30})
        self.assertEqual(reason, "wake")

    def test_no_double_wake_for_same_runs(self):
        runs = [run("r1")]
        _, reason, state = call(runs)
        self.assertEqual(reason, "wake")
        message, reason, _ = call(runs, state=state, now=NOW + 1000)
        self.assertIsNone(message)
        self.assertEqual(reason, "nothing_new")

    def test_new_finished_run_wakes_again(self):
        _, _, state = call([run("r1")])
        later = NOW + 1000
        runs = [run("r1"), run("r2xxxxxxxxyyyy", updated=NOW + 500)]
        message, reason, new_state = call(runs, state=state, now=later)
        self.assertEqual(reason, "wake")
        self.assertIn("r2xxxxxxxx", message)
        self.assertNotIn("Summary of r1", message)
        self.assertEqual(new_state["reported_run_ids"], ["r1", "r2xxxxxxxxyyyy"])

    def test_old_runs_before_last_wake_are_not_new(self):
        state = {"last_wake": 6000.0, "reported_run_ids": []}
        _, reason, _ = call([run("r1", updated=5000.0)], state=state)
        self.assertEqual(reason, "nothing_new")

    def test_blocked_and_cancelled_are_reported(self):
        runs = [run("rb", state="blocked", summary="", error="Needs operator"),
                run("rc", state="cancelled", summary="Stopped by user")]
        message, reason, _ = call(runs)
        self.assertEqual(reason, "wake")
        self.assertIn("rb blocked: Needs operator", message)
        self.assertIn("rc cancelled: Stopped by user", message)

    def test_main_wake_false_disables(self):
        message, reason, _ = call([run("r1")], settings={"main_wake": False})
        self.assertIsNone(message)
        self.assertEqual(reason, "disabled")
        _, reason, _ = call([run("r1")], settings={"main_wake": None})
        self.assertEqual(reason, "wake")

    def test_input_state_not_changed(self):
        state = {"last_wake": 0, "reported_run_ids": ["old"]}
        before = copy.deepcopy(state)
        runs = [run("r1")]
        runs_before = copy.deepcopy(runs)
        _, reason, new_state = call(runs, state=state)
        self.assertEqual(reason, "wake")
        self.assertEqual(state, before)
        self.assertEqual(runs, runs_before)
        self.assertIsNot(new_state["reported_run_ids"], state["reported_run_ids"])

    def test_other_account_runs_ignored(self):
        runs = [run("r1"), run("x1", state="executing", account_id="acc2")]
        message, reason, _ = call(runs)
        self.assertEqual(reason, "wake")
        self.assertNotIn("x1", message)
        _, reason, _ = call([run("x2", account_id="acc2")])
        self.assertEqual(reason, "nothing_new")

    def test_reported_ids_capped(self):
        state = {"last_wake": 0, "reported_run_ids": ["old%d" % i for i in range(250)]}
        _, reason, new_state = call([run("r1")], state=state)
        self.assertEqual(reason, "wake")
        self.assertEqual(len(new_state["reported_run_ids"]), 200)
        self.assertEqual(new_state["reported_run_ids"][-1], "r1")


class MessageTests(unittest.TestCase):
    def test_length_limit_with_50_long_runs(self):
        runs = [run("run%03d-abcdefghij" % i, summary="x" * 400) for i in range(50)]
        message = build_message(runs, [])
        self.assertLessEqual(len(message), 1500)
        lines = message.split("\n")
        self.assertEqual(lines[0], "Automatische Meldung vom Harness: Alle Coworker sind fertig.")
        self.assertEqual(lines[-1], FOOT)
        self.assertTrue(lines[-2].startswith("\u2026 und "))
        self.assertTrue(lines[-2].endswith(" weitere Runs"))
        shown = len(lines) - 3
        self.assertEqual(int(lines[-2].split()[2]), 50 - shown)

    def test_line_format_and_newlines(self):
        message = build_message([run("abcdefghijKLMN", summary="line one\nline two" + "y" * 300)], [])
        lines = message.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].startswith("- abcdefghij done: line one line two"))
        self.assertNotIn("KLMN", lines[1])
        self.assertEqual(len(lines[1].split(": ", 1)[1]), 150)

    def test_footer_text(self):
        self.assertEqual(FOOT, "Bitte Ergebnisse selbst pr\u00fcfen (Test-Path, Tests) und mit dem "
                               "n\u00e4chsten offenen Punkt aus dem Plan weitermachen.")


class StepIdTests(unittest.TestCase):
    def test_step_id_order_independent(self):
        self.assertEqual(wake_step_id(["b", "a", "a"]), wake_step_id(["a", "b"]))
        self.assertTrue(wake_step_id(["a"]).startswith("main_wake:"))
        self.assertEqual(len(wake_step_id(["a"])), len("main_wake:") + 16)

    def test_step_id_differs_for_other_ids(self):
        self.assertNotEqual(wake_step_id(["a", "b"]), wake_step_id(["a", "c"]))


# ---- MainWake loop (fake store / adapter / client / clock, no network) ----

T0 = 1_000_000.0
THREAD = "thr1"


class WkStore:
    def __init__(self):
        self.data, self.events, self.conf = {}, [], {}

    def get(self, kind, ident, default=None):
        doc = self.data.get(kind, {}).get(str(ident))
        return copy.deepcopy(doc) if doc is not None else default

    def all(self, kind):
        return [copy.deepcopy(d) for d in self.data.get(kind, {}).values()]

    def put(self, kind, doc):
        self.data.setdefault(kind, {})[str(doc["id"])] = copy.deepcopy(doc)
        return doc

    def event(self, kind, run_id="", *, commit=True, **fields):
        self.events.append((kind, run_id, fields))

    def settings(self):
        return dict(self.conf)

    def kinds(self, kind):
        return [e for e in self.events if e[0] == kind]


class WkTransport:
    def __init__(self, client):
        self.client = client

    def stream(self, name, body, headers, timeout=None, cancel=None):
        assert name == "runInferenceTranscript"
        if self.client.fail:
            raise RuntimeError("boom")
        self.client.sent.append(body)
        return iter(["{}"])


class WkClient:
    def __init__(self):
        self.info = {"space_id": "space"}
        self.status, self.pending, self.fail = "idle", False, False
        self.sent = []
        self.transport = WkTransport(self)

    def transcript(self, thread, limit=100):
        return {"session": {"status": self.status}, "session_pending": self.pending}

    def model(self, label=None):
        return "model-1"

    def headers(self):
        return {}


class WkAdapter:
    def __init__(self):
        self.cl = WkClient()
        self.ids = 0

    def client(self, account_id):
        return self.cl

    def new_id(self, space_id, tail="00aa"):
        self.ids += 1
        return "step%d" % self.ids

    def body(self, client, thread, step_id, prompt, model, create=False, computer=False, effort=None):
        return dict(thread=thread, step=step_id, prompt=prompt, computer=computer, effort=effort)


class WkClock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def wk_run(rid, state="done", updated=T0 - 60, **extra):
    item = dict(id=rid, state=state, updated=updated, operator=True, main_worker="acc:" + THREAD,
                chat_id="c1", account_id="acc", summary="Summary of " + rid)
    item.update(extra)
    return item


def wk_task(tid, run_id, state="done"):
    return dict(id=tid, run_id=run_id, state=state)


class MainWakeLoopTests(unittest.TestCase):
    def setUp(self):
        self.store, self.adapter, self.clock = WkStore(), WkAdapter(), WkClock()
        self.store.put("workers", dict(id="acc:" + THREAD, account_id="acc", thread_id=THREAD, purpose="operator"))
        self.store.put("chats", dict(id="c1", operator=True, state="active", main_worker="acc:" + THREAD))
        self.wake = main_wake.MainWake(self.store, self.adapter, None, clock=self.clock)

    def add(self, rid, state="done", tasks=("done",), **extra):
        self.store.put("runs", wk_run(rid, state, **extra))
        for i, task_state in enumerate(tasks):
            self.store.put("tasks", wk_task("%s-t%d" % (rid, i), rid, task_state))

    @property
    def sent(self):
        return self.adapter.cl.sent

    def test_one_message_for_a_finished_run(self):
        self.add("r1", tasks=("done", "done", "failed"))
        self.assertEqual(self.wake.tick(), ["r1"])
        self.assertEqual(len(self.sent), 1)
        body = self.sent[0]
        self.assertEqual((body["thread"], body["computer"]), (THREAD, True))
        self.assertTrue(body["effort"])
        self.assertTrue(body["prompt"].startswith("NotionTool (automatic): Cowork run finished."))
        self.assertIn("- r1 done: Summary of r1; tasks 2/3, not accepted r1-t2", body["prompt"])
        self.assertIn("GET /api/run?id=r1", body["prompt"])
        self.assertEqual(self.store.kinds("main_wake_sent")[0][2]["run_ids"], ["r1"])
        self.assertEqual(self.store.get("main_wakes", "r1:done")["status"], "sent")

    def test_no_accepted_problem_says_none(self):
        self.add("r1")
        self.wake.tick()
        self.assertIn("tasks 1/1, not accepted none", self.sent[0]["prompt"])

    def test_nothing_while_review_or_fix_task_is_open(self):
        for open_state in ("queued", "ready", "retry", "running", "executing", "fixing", "uncertain",
                           "recovering", "cancelling"):
            self.store.data.clear()
            self.setUp()
            self.add("r1", tasks=("done", open_state))
            self.assertEqual(self.wake.tick(), [], open_state)
            self.assertEqual(self.sent, [])
        self.store.put("tasks", wk_task("r1-t1", "r1", "done"))
        self.assertEqual(self.wake.tick(), ["r1"])

    def test_run_not_terminal_yet(self):
        self.add("r1", state="fixing")
        self.assertEqual(self.wake.tick(), [])

    def test_waits_while_chat_busy_then_sends_when_idle(self):
        self.add("r1")
        self.adapter.cl.status = "running"
        self.assertEqual(self.wake.tick(), [])
        self.assertEqual(self.wake.tick(), [])
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.store.kinds("main_wake_wait_busy")), 1)  # at most once per run
        self.assertIsNone(self.store.get("main_wakes", "r1:done"))  # stays pending
        self.adapter.cl.status = "idle"
        self.assertEqual(self.wake.tick(), ["r1"])
        self.assertEqual(len(self.sent), 1)

    def test_pending_session_counts_as_busy(self):
        self.add("r1")
        self.adapter.cl.pending = True
        self.assertEqual(self.wake.tick(), [])
        self.adapter.cl.pending = False
        self.assertEqual(self.wake.tick(), ["r1"])

    def test_no_second_message_for_same_run_and_state(self):
        self.add("r1")
        self.wake.tick()
        self.clock.t += 600
        self.assertEqual(self.wake.tick(), [])
        fresh = main_wake.MainWake(self.store, self.adapter, None, clock=self.clock)  # after a restart
        self.assertEqual(fresh.tick(), [])
        self.assertEqual(len(self.sent), 1)

    def test_new_state_of_same_run_is_a_new_message(self):
        self.add("r1", state="awaiting_main")
        self.wake.tick()
        self.store.put("runs", wk_run("r1", "failed", updated=self.clock.t))
        self.clock.t += 120
        self.assertEqual(self.wake.tick(), ["r1"])
        self.assertEqual(len(self.sent), 2)

    def test_baseline_skips_old_runs_but_not_recent_ones(self):
        self.add("old", updated=T0 - 31 * 60)
        self.add("recent", updated=T0 - 29 * 60)
        self.assertEqual(self.wake.tick(), ["recent"])
        self.assertEqual(self.store.get("main_wakes", "old:done")["status"], "baseline")
        self.assertIn("recent", self.sent[0]["prompt"])
        self.assertNotIn("old", self.sent[0]["prompt"].replace("Cowork", ""))
        self.clock.t += 600
        self.assertEqual(self.wake.tick(), [])
        self.assertEqual(len(self.sent), 1)

    def test_baseline_only_runs_at_start_not_later(self):
        self.assertEqual(self.wake.tick(), [])
        self.add("late", updated=T0 - 31 * 60)  # finishes after start: still reported
        self.assertEqual(self.wake.tick(), ["late"])

    def test_two_runs_in_one_tick_make_one_message(self):
        self.add("r1", updated=T0 - 20)
        self.add("r2", state="failed", updated=T0 - 10)
        self.assertEqual(self.wake.tick(), ["r1", "r2"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("- r1 done", self.sent[0]["prompt"])
        self.assertIn("- r2 failed", self.sent[0]["prompt"])
        self.assertTrue(self.sent[0]["prompt"].startswith("NotionTool (automatic): Cowork runs finished."))

    def test_at_most_one_message_per_minute_per_chat(self):
        self.add("r1")
        self.wake.tick()
        self.add("r2")
        self.clock.t += 30
        self.assertEqual(self.wake.tick(), [])
        self.clock.t += 31
        self.assertEqual(self.wake.tick(), ["r2"])
        self.assertEqual(len(self.sent), 2)

    def test_disabled_setting_sends_nothing(self):
        self.store.conf["main_wake_enabled"] = False
        self.add("r1")
        self.assertEqual(self.wake.tick(), [])
        self.assertEqual(self.sent, [])
        self.store.conf["main_wake_enabled"] = True
        self.assertEqual(self.wake.tick(), ["r1"])

    def test_confirmed_and_non_operator_runs_send_nothing(self):
        self.add("r1", confirmed_at=T0 - 5)
        self.add("r2", operator=False)
        self.assertEqual(self.wake.tick(), [])
        self.assertEqual(self.sent, [])

    def test_interval_setting_default_and_override(self):
        self.assertEqual(self.wake.interval(), 15.0)
        self.store.conf["main_wake_interval_s"] = 40
        self.assertEqual(self.wake.interval(), 40.0)

    def test_failed_send_is_recorded_first_and_never_resent(self):
        self.add("r1")
        self.adapter.cl.fail = True
        self.assertEqual(self.wake.tick(), [])
        self.assertEqual(self.store.get("main_wakes", "r1:done")["status"], "failed")
        self.assertEqual(len(self.store.kinds("main_wake_failed")), 1)
        self.adapter.cl.fail = False
        self.clock.t += 600
        self.assertEqual(self.wake.tick(), [])
        self.assertEqual(self.sent, [])

    def test_never_creates_chats_or_agents(self):
        self.add("r1")
        self.wake.tick()
        self.assertEqual(sorted(self.store.data), ["chats", "main_wakes", "runs", "tasks", "workers"])

    def test_message_stays_short_for_many_runs(self):
        for i in range(12):
            self.add("run%02d" % i, summary="x" * 400, updated=T0 - 100 + i)
        self.wake.tick()
        prompt = self.sent[0]["prompt"]
        self.assertIn("... and 7 more runs", prompt)
        self.assertLess(len(prompt), 1500)


if __name__ == "__main__":
    unittest.main()
