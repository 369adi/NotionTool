import copy
import unittest

from notiontool import autopilot
from notiontool.autopilot import build_goal, should_submit

NOW = 10000.0


def worker(ident, **extra):
    data = dict(id=ident, purpose="worker", enabled=True, state="ready", lease="",
                cooldown_until=0, account_id="a1")
    data.update(extra)
    return data


def item(ident, request_id=None, **extra):
    data = dict(id=ident, goal="Goal " + ident, priority=0, deps=[], approvals=[],
                request_id="req-" + ident if request_id is None else request_id, state="queued",
                submitted_run_id="", created=100.0, submitted_at=None)
    data.update(extra)
    return data


ACCOUNTS = [dict(id="a1", state="ready", cooldown_until=0)]
ON = dict(autopilot=True)


def free_pool():
    return [worker("w1"), worker("w2")]


def decide(items, workers=None, runs=None, settings=None, now=NOW, accounts=ACCOUNTS):
    return should_submit(items, free_pool() if workers is None else workers,
                         runs or [], ON if settings is None else settings, now, accounts=accounts)


class GateTests(unittest.TestCase):
    def test_submits_when_all_conditions_hold(self):
        chosen, reason = decide([item("i1")])
        self.assertEqual(chosen["id"], "i1")
        self.assertEqual(chosen["final_goal"], "Goal i1")
        self.assertTrue(reason)

    def test_autopilot_off_by_default(self):
        chosen, reason = decide([item("i1")], settings={})
        self.assertIsNone(chosen)
        self.assertIn("off", reason)
        self.assertIsNone(decide([item("i1")], settings=dict(autopilot="yes"))[0])

    def test_too_few_free_workers(self):
        chosen, reason = decide([item("i1")], workers=[worker("w1")])
        self.assertIsNone(chosen)
        self.assertIn("free coworkers", reason)
        self.assertIsNotNone(decide([item("i1")], workers=[worker("w1")],
                                    settings=dict(autopilot=True, autopilot_min_free_workers=1))[0])

    def test_main_and_busy_workers_not_counted(self):
        pool = [worker("main", purpose="main"), worker("w1"), worker("w2", state="busy"),
                worker("w3", lease="x"), worker("w4", enabled=False),
                worker("w5", cooldown_until=NOW + 5), worker("w6", account_id="a2")]
        accounts = ACCOUNTS + [dict(id="a2", state="paused")]
        chosen, reason = decide([item("i1")], workers=pool, accounts=accounts)
        self.assertIsNone(chosen)
        self.assertIn("Only 1", reason)

    def test_account_cooldown_excludes_worker(self):
        accounts = [dict(id="a1", state="ready", cooldown_until=NOW + 30)]
        self.assertIsNone(decide([item("i1")], accounts=accounts)[0])

    def test_accounts_omitted_uses_worker_fields_only(self):
        chosen, reason = decide([item("i1")], accounts=None)
        self.assertEqual(chosen["id"], "i1")
        pool = [worker("w1"), worker("w2", account_id="unknown"), worker("w3", lease="x"),
                worker("w4", state="busy"), worker("w5", cooldown_until=NOW + 1),
                worker("w6", enabled=False), worker("main", purpose="main")]
        self.assertEqual([w["id"] for w in autopilot.free_workers(pool, None, NOW)], ["w1", "w2"])
        chosen, reason = decide([item("i1")], workers=pool[2:], accounts=None)
        self.assertIsNone(chosen)
        self.assertIn("Only 0", reason)

    def test_accounts_passed_cooling_account_excludes_its_worker(self):
        accounts = ACCOUNTS + [dict(id="a2", state="ready", cooldown_until=NOW + 30)]
        pool = [worker("w1"), worker("w2", account_id="a2")]
        self.assertEqual([w["id"] for w in autopilot.free_workers(pool, accounts, NOW)], ["w1"])
        chosen, reason = decide([item("i1")], workers=pool, accounts=accounts)
        self.assertIsNone(chosen)
        self.assertIn("Only 1", reason)
        accounts[1] = dict(id="a2", state="ready", cooldown_until=NOW)
        self.assertEqual(decide([item("i1")], workers=pool, accounts=accounts)[0]["id"], "i1")

    def test_min_free_workers_clamped_to_zero(self):
        settings = dict(autopilot=True, autopilot_min_free_workers=-3)
        self.assertEqual(decide([item("i1")], workers=[], settings=settings)[0]["id"], "i1")

    def test_run_limit(self):
        runs = [dict(id="r%d" % n, chat_id="c", state=s, request_id="")
                for n, s in enumerate(["executing", "paused", "reviewing"])]
        chosen, reason = decide([item("i1")], runs=runs)
        self.assertIsNone(chosen)
        self.assertIn("max_runs_per_chat", reason)
        done = [dict(id="r9", chat_id="c", state="done", request_id="")]
        self.assertIsNotNone(decide([item("i1")], runs=runs[:2] + done)[0])
        self.assertIsNone(decide([item("i1")], runs=runs[:1],
                                 settings=dict(autopilot=True, max_runs_per_chat=1))[0])

    def test_run_limit_scoped_to_chat(self):
        runs = [dict(id="r1", chat_id="other", state="executing", request_id="")]
        settings = dict(autopilot=True, max_runs_per_chat=1, autopilot_chat_id="mine")
        self.assertIsNotNone(decide([item("i1")], runs=runs, settings=settings)[0])

    def test_queued_run_blocks_submit(self):
        runs = [dict(id="r1", chat_id="c", state="queued", request_id="")]
        self.assertIsNone(decide([item("i1")], runs=runs)[0])

    def test_runs_per_chat_matches_engine_rule(self):
        self.assertEqual(autopilot.runs_per_chat({}), 3)
        self.assertEqual(autopilot.runs_per_chat(dict(max_runs_per_chat=9)), 6)
        self.assertEqual(autopilot.runs_per_chat(dict(max_runs_per_chat=0)), 1)
        self.assertEqual(autopilot.runs_per_chat(dict(max_runs_per_chat="x")), 1)

    def test_cooldown_active_and_expired(self):
        items = [item("old", state="submitted", submitted_at=NOW - 30), item("i1")]
        chosen, reason = decide(items)
        self.assertIsNone(chosen)
        self.assertIn("Cooldown", reason)
        items[0]["submitted_at"] = NOW - 60
        self.assertEqual(decide(items)[0]["id"], "i1")
        items[0]["submitted_at"] = NOW - 30
        self.assertIsNotNone(decide(items, settings=dict(autopilot=True, autopilot_cooldown_s=10))[0])

    def test_blocked_run_waits_for_operator(self):
        runs = [dict(id="rb", chat_id="c", state="blocked", request_id="")]
        chosen, reason = decide([item("i1")], runs=runs)
        self.assertIsNone(chosen)
        self.assertIn("operator", reason)


class PickTests(unittest.TestCase):
    def test_duplicate_request_id_never_resubmitted(self):
        items = [item("old", request_id="same", state="done"), item("new", request_id="same")]
        self.assertIsNone(decide(items)[0])
        runs = [dict(id="r1", chat_id="c", state="done", request_id="dup")]
        self.assertIsNone(decide([item("i1", request_id="dup")], runs=runs)[0])
        long_id = "x" * 120
        runs = [dict(id="r2", chat_id="c", state="done", request_id=long_id[:100])]
        self.assertIsNone(decide([item("i2", request_id=long_id)], runs=runs)[0])

    def test_missing_request_id_or_goal_skipped(self):
        self.assertIsNone(decide([item("i1", request_id="", goal="x")])[0])
        items = [item("i1", request_id=""), item("i2", goal="  ")]
        chosen, reason = decide(items)
        self.assertIsNone(chosen)
        self.assertIn("request_id", reason)

    def test_deps_respected(self):
        items = [item("a", state="submitted"), item("b", deps=["a"], priority=5), item("c")]
        self.assertEqual(decide(items)[0]["id"], "c")
        items[0]["state"] = "done"
        self.assertEqual(decide(items)[0]["id"], "b")
        self.assertIsNone(decide([item("x", deps=["missing"])])[0])

    def test_priority_then_age_then_id(self):
        items = [item("b", priority=1, created=50), item("a", priority=1, created=50),
                 item("c", priority=1, created=10), item("d", priority=0, created=1)]
        self.assertEqual(decide(items)[0]["id"], "c")
        items.pop(2)
        self.assertEqual(decide(items)[0]["id"], "a")

    def test_no_ready_item(self):
        chosen, reason = decide([item("i1", state="done")])
        self.assertIsNone(chosen)
        self.assertIn("No queued", reason)
        self.assertIsNone(decide([])[0])


class GoalTests(unittest.TestCase):
    def test_approval_line_present(self):
        it = item("i1", goal=" Fix it ", approvals=["notiontool\\engine.py", " server.py ",
                                                     "notiontool/engine.py", ""])
        self.assertEqual(build_goal(it),
                         "Fix it\nOperator approval for: notiontool/engine.py, server.py")
        self.assertEqual(decide([it])[0]["final_goal"].splitlines()[-1],
                         "Operator approval for: notiontool/engine.py, server.py")

    def test_approval_line_absent(self):
        self.assertEqual(build_goal(item("i1", goal="Fix it")), "Fix it")
        self.assertNotIn("Operator approval", build_goal(item("i1", approvals=None)))

    def test_inputs_left_unchanged(self):
        items = [item("i1", approvals=["a.py"]), item("i0", state="done")]
        pool = free_pool()
        runs = [dict(id="r1", chat_id="c", state="done", request_id="other")]
        settings = dict(autopilot=True)
        before = copy.deepcopy((items, pool, runs, settings, ACCOUNTS))
        chosen, _ = should_submit(items, pool, runs, settings, NOW, accounts=ACCOUNTS)
        self.assertEqual((items, pool, runs, settings, ACCOUNTS), before)
        self.assertNotIn("final_goal", items[0])
        chosen["approvals"].append("b.py")
        self.assertEqual(items[0]["approvals"], ["a.py"])


if __name__ == "__main__":
    unittest.main()
