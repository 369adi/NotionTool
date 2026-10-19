"""Tests for bench/run_bench.py. Fake client, fake clock and temp paths only."""

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from bench import run_bench as rb

REQUIRED_FIELDS = {"task_id", "variant", "run_index", "started", "accepted_at",
                   "accepted", "corrections", "model_calls"}


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = float(start)
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class FakeClient:
    def __init__(self, clock=None, step=0.0, fail=None, result=None):
        self.clock = clock
        self.step = step
        self.fail = fail or (lambda method, prompt, count: False)
        self.result = result or (lambda handle: {
            "accepted": handle[0] == "final", "corrections": 1, "model_calls": 4})
        self.calls = []
        self.call_times = []

    def _tick(self, name, arg):
        if self.clock is not None:
            self.call_times.append(self.clock.time())
            self.clock.now += self.step
        self.calls.append((name, arg))

    def _send(self, variant, method, prompt):
        self._tick(method, prompt)
        if self.fail(method, prompt, len(self.calls)):
            raise RuntimeError("fake transport failure")
        return (variant, prompt, len(self.calls))

    def send_b0(self, prompt):
        return self._send("B0", "send_b0", prompt)

    def send_harness(self, prompt):
        return self._send("final", "send_harness", prompt)

    def wait_done(self, handle):
        self._tick("wait_done", handle)
        return self.result(handle)


TASKS = [{"id": "t1", "prompt": "first"}, {"id": "t2", "prompt": "second"}]


def read_lines(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read().split("\n")[:-1]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "results", "raw.jsonl")
        self.clock = FakeClock()

    def run_bench(self, client, tasks=TASKS, account="abc123", **kwargs):
        return rb.run_bench(tasks, client, account, self.out, clock=self.clock.time,
                            sleep=self.clock.sleep, **kwargs)

    def test_exact_alternation_order(self):
        client = FakeClient()
        records = self.run_bench(client)
        sends = [(name, arg) for name, arg in client.calls if name != "wait_done"]
        expected = []
        for task in TASKS:
            for _ in range(3):
                expected += [("send_b0", task["prompt"]), ("send_harness", task["prompt"])]
        self.assertEqual(sends, expected)
        names = [name for name, _ in client.calls]
        self.assertEqual(names[1::2], ["wait_done"] * len(expected))
        order = [(r["task_id"], r["variant"], r["run_index"]) for r in records]
        self.assertEqual(order[:6], [("t1", "B0", 0), ("t1", "final", 0),
                                     ("t1", "B0", 1), ("t1", "final", 1),
                                     ("t1", "B0", 2), ("t1", "final", 2)])

    def test_six_lines_per_task_with_fields(self):
        self.run_bench(FakeClient())
        lines = read_lines(self.out)
        self.assertEqual(len(lines), 12)
        rows = [json.loads(line) for line in lines]
        for task_id in ("t1", "t2"):
            self.assertEqual(sum(1 for r in rows if r["task_id"] == task_id), 6)
        for row in rows:
            self.assertEqual(set(row), REQUIRED_FIELDS)
            self.assertIn(row["variant"], ("B0", "final"))
            self.assertIsInstance(row["started"], float)
            self.assertEqual(row["corrections"], 1)
            self.assertEqual(row["model_calls"], 4)
            if row["accepted"]:
                self.assertIsInstance(row["accepted_at"], float)
            else:
                self.assertIsNone(row["accepted_at"])
        with open(self.out, "rb") as handle:
            self.assertNotIn(b"\r", handle.read())

    def test_task_objects_with_attributes(self):
        task = types.SimpleNamespace(id="obj", prompt="p")
        records = self.run_bench(FakeClient(), tasks=[task], runs=1)
        self.assertEqual([(r["task_id"], r["variant"]) for r in records],
                         [("obj", "B0"), ("obj", "final")])

    def test_append_keeps_existing_lines(self):
        os.makedirs(os.path.dirname(self.out))
        with open(self.out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write('{"existing": true}\n')
        self.run_bench(FakeClient(), tasks=TASKS[:1])
        lines = read_lines(self.out)
        self.assertEqual(lines[0], '{"existing": true}')
        self.assertEqual(len(lines), 7)

    def test_refuses_971f_accounts(self):
        for account in ("971fabc", "971FABC", "  971f-x  ", "971F"):
            with self.subTest(account=account):
                client = FakeClient()
                with self.assertRaises(rb.RefusedAccount):
                    self.run_bench(client, account=account)
                self.assertEqual(client.calls, [])
                self.assertFalse(os.path.exists(self.out))
                self.assertFalse(os.path.exists(os.path.dirname(self.out)))

    def test_other_accounts_allowed(self):
        for account in ("abc971f", "f971", "97f1"):
            with self.subTest(account=account):
                client = FakeClient()
                records = self.run_bench(client, tasks=TASKS[:1], account=account, runs=1)
                self.assertEqual(len(records), 2)
                self.assertEqual(len(client.calls), 4)

    def test_limiter_sleeps_when_over_limit(self):
        limiter = rb.RateLimiter(179, 60.0, self.clock.time, self.clock.sleep)
        for _ in range(179):
            limiter.acquire()
        self.assertEqual(self.clock.slept, [])
        limiter.acquire()
        self.assertEqual(len(self.clock.slept), 1)
        self.assertAlmostEqual(self.clock.slept[0], 60.0)
        self.assertEqual(limiter.waits, 1)

    def test_never_more_than_179_calls_in_60_seconds(self):
        client = FakeClient(clock=self.clock, step=0.01)
        tasks = [{"id": "t%d" % i, "prompt": "p%d" % i} for i in range(25)]
        self.run_bench(client, tasks=tasks)
        times = client.call_times
        self.assertEqual(len(times), 25 * 6 * 2)
        self.assertTrue(self.clock.slept)
        for i in range(len(times) - 179):
            self.assertGreaterEqual(times[i + 179] - times[i], 60.0 - 1e-6)
        self.assertEqual(rb.DEFAULT_LIMIT, 179)

    def test_client_error_is_recorded_and_run_continues(self):
        def fail(method, prompt, count):
            return method == "send_harness" and count == 3
        client = FakeClient(fail=fail, result=lambda h: {
            "accepted": True, "corrections": 0, "model_calls": 2})
        records = self.run_bench(client, tasks=TASKS[:1])
        self.assertEqual(len(records), 6)
        failed = [r for r in records if not r["accepted"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0]["variant"], failed[0]["run_index"]), ("final", 0))
        self.assertIsNone(failed[0]["accepted_at"])
        self.assertIn("RuntimeError", failed[0]["error"])
        self.assertEqual(len(read_lines(self.out)), 6)

    def test_wait_done_error_is_recorded(self):
        def result(handle):
            raise TimeoutError("never finished")
        records = self.run_bench(FakeClient(result=result), tasks=TASKS[:1], runs=1)
        self.assertEqual([r["accepted"] for r in records], [False, False])


class SuiteIntegrationTests(unittest.TestCase):
    def test_real_suite_output_loads_in_metrics(self):
        from bench import metrics
        from bench.suite import SUITE

        clock = FakeClock()

        def result(handle):
            if handle[2] == 1:
                return {"accepted": True, "accepted_at": "2026-10-16T20:00:00Z",
                        "corrections": 0, "model_calls": 1}
            return {"accepted": True, "corrections": 1, "model_calls": 3}

        def fail(method, prompt, count):
            return method == "send_harness" and count == 3

        client = FakeClient(fail=fail, result=result)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "raw.jsonl")
            records = rb.run_bench(SUITE, client, "abc123", out,
                                   clock=clock.time, sleep=clock.sleep)
            lines = read_lines(out)
            rows = metrics.load_jsonl(out)
        self.assertEqual(len(SUITE), 6)
        self.assertEqual(len(records), 36)
        self.assertEqual(len(lines), 36)
        self.assertEqual(len(rows), 36)
        for row in rows:
            value = row["accepted_at"]
            self.assertTrue(value is None or (isinstance(value, (int, float))
                                              and not isinstance(value, bool)), row)
        self.assertTrue(rows[0]["accepted"])
        self.assertEqual(rows[0]["accepted_at"], clock.time())
        self.assertFalse(rows[1]["accepted"])
        self.assertIsNone(rows[1]["accepted_at"])
        self.assertIn("RuntimeError", rows[1]["error"])
        suite_ids = [task.id if not isinstance(task, dict) else task["id"] for task in SUITE]
        self.assertEqual([row["task_id"] for row in rows[::6]], suite_ids)
        summary = metrics.summarize(rows)
        self.assertEqual(len(summary), 12)
        first = summary[(suite_ids[0], "final")]
        self.assertEqual(first["runs"], 3)
        self.assertAlmostEqual(first["acceptance_rate"], 2 / 3)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "sub", "raw.jsonl")

    def call_main(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = rb.main(argv)
        return code, err.getvalue()

    def test_main_returns_2_on_refusal(self):
        for account in ("971fdead", "971Fdead"):
            with self.subTest(account=account):
                code, err = self.call_main(["--account", account, "--out", self.out,
                                            "--client", "no_such_module:client"])
                self.assertEqual(code, 2)
                self.assertIn("refusing", err)
                self.assertFalse(os.path.exists(os.path.dirname(self.out)))

    def test_main_fails_clearly_without_client(self):
        code, err = self.call_main(["--account", "abc", "--out", self.out])
        self.assertEqual(code, 3)
        self.assertIn("--client", err)
        code, err = self.call_main(["--account", "abc", "--out", self.out,
                                    "--client", "no_such_bench_module_x:client"])
        self.assertEqual(code, 3)
        self.assertIn("cannot load client", err)
        self.assertFalse(os.path.exists(self.out))

    def test_main_runs_with_injected_client_and_suite(self):
        client_module = types.ModuleType("fake_bench_client_module")
        client_module.client = FakeClient()
        suite_module = types.ModuleType("bench.suite")
        suite_module.SUITE = TASKS[:1]
        with mock.patch.dict(sys.modules, {"fake_bench_client_module": client_module,
                                           "bench.suite": suite_module}):
            code, _ = self.call_main(["--account", "abc", "--out", self.out, "--runs", "2",
                                      "--client", "fake_bench_client_module:client"])
        self.assertEqual(code, 0)
        self.assertEqual(len(read_lines(self.out)), 4)

    def test_default_out_path(self):
        self.assertEqual(rb.build_parser().parse_args(["--account", "x"]).out,
                         "bench/results/raw.jsonl")


if __name__ == "__main__":
    unittest.main()
