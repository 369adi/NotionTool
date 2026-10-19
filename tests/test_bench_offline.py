"""Offline bench client: setup() and check() are really used (MOCKED runs)."""

import tempfile
import unittest

from bench import offline_client, run_bench
from bench.suite import SUITE


class FakeTask:
    def __init__(self, passed, task_id="fake", prompt="do the fake task"):
        self.id = task_id
        self.prompt = prompt
        self.passed = passed
        self.calls = []

    def setup(self, folder):
        self.calls.append(("setup", folder))

    def check(self, folder):
        self.calls.append(("check", folder))
        return self.passed, "fake details"


class OfflineClientTests(unittest.TestCase):
    def test_client_calls_setup_then_check_in_the_same_folder(self):
        task = FakeTask(True)
        solved = []
        client = offline_client.OfflineClient(tasks=[task], solutions={"fake": solved.append})
        result = client.wait_done(client.send_harness(task.prompt))
        self.assertEqual([name for name, _ in task.calls], ["setup", "check"])
        self.assertEqual(task.calls[0][1], task.calls[1][1])
        self.assertEqual(solved, [task.calls[0][1]])
        self.assertTrue(result["accepted"])
        self.assertTrue(result["mocked"])
        self.assertEqual(result["model_calls"], 0)

    def test_failing_check_is_reported_as_not_accepted(self):
        task = FakeTask(False)
        client = offline_client.OfflineClient(tasks=[task], solutions={})
        result = client.wait_done(client.send_b0(task.prompt))
        self.assertFalse(result["accepted"])
        self.assertEqual(result["details"], "fake details")
        self.assertFalse(result["scripted"])

    def test_runner_records_failing_check_as_not_accepted(self):
        task = FakeTask(False)
        client = offline_client.OfflineClient(tasks=[task], solutions={})
        with tempfile.TemporaryDirectory() as tmp:
            records = run_bench.run_bench([task], client, "offline", tmp + "/raw.jsonl", runs=1,
                                          sleep=lambda s: None)
        self.assertEqual([(r["variant"], r["accepted"]) for r in records], [("B0", False), ("final", False)])
        self.assertEqual(sum(1 for name, _ in task.calls if name == "setup"), 2)
        self.assertEqual(sum(1 for name, _ in task.calls if name == "check"), 2)

    def test_reference_scripts_pass_and_control_fails_on_the_real_suite(self):
        for task in SUITE:
            good = offline_client.client()
            self.assertTrue(good.wait_done(good.send_harness(task.prompt))["accepted"], task.id)
            empty = offline_client.control_client()
            self.assertFalse(empty.wait_done(empty.send_b0(task.prompt))["accepted"], task.id)

    def test_temporary_folders_are_removed(self):
        import os
        task = FakeTask(True)
        client = offline_client.OfflineClient(tasks=[task], solutions={})
        client.wait_done(client.send_b0(task.prompt))
        self.assertFalse(os.path.exists(task.calls[0][1]))

    def test_unknown_prompt_is_an_error_recorded_as_not_accepted(self):
        client = offline_client.OfflineClient(tasks=[FakeTask(True)], solutions={})
        with tempfile.TemporaryDirectory() as tmp:
            records = run_bench.run_bench([{"id": "x", "prompt": "unknown"}], client, "offline",
                                          tmp + "/raw.jsonl", runs=1, sleep=lambda s: None)
        self.assertTrue(all(not r["accepted"] and "KeyError" in r["error"] for r in records))


if __name__ == "__main__":
    unittest.main()
