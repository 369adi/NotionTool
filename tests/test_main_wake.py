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


if __name__ == "__main__":
    unittest.main()
