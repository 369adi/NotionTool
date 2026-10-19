import unittest

from notiontool import role_policy as rp


def coworker(ident="w1", **extra):
    worker = {"id": ident, "enabled": True, "state": "ready", "purpose": "worker", "account_id": "a1"}
    worker.update(extra)
    return worker


RUN = {"id": "r1", "main_worker": "main"}


class ToolRightsTest(unittest.TestCase):
    def test_rights_per_role(self):
        self.assertEqual(rp.tool_rights("planner"), "read_only")
        self.assertEqual(rp.tool_rights("reviewer"), "read_only")
        self.assertEqual(rp.tool_rights("executor"), "read_write")
        self.assertEqual(rp.tool_rights("integrator"), "read_write")

    def test_unknown_role_raises(self):
        with self.assertRaises(ValueError):
            rp.tool_rights("boss")


class EligibleTest(unittest.TestCase):
    def test_coworker_takes_every_role(self):
        for role in rp.ROLES:
            ok, reason = rp.eligible(coworker(), {"id": "t", "role": role}, RUN, {})
            self.assertTrue(ok, role)
            self.assertTrue(reason.endswith("."))

    def test_missing_worker_refused(self):
        ok, reason = rp.eligible(None, {"id": "t", "role": "executor"}, RUN, {})
        self.assertFalse(ok)
        self.assertIn("missing", reason)

    def test_disabled_worker_refused(self):
        ok, reason = rp.eligible(coworker(enabled=False), {"id": "t", "role": "executor"}, RUN, {})
        self.assertFalse(ok)
        self.assertIn("disabled", reason)

    def test_not_ready_worker_refused(self):
        ok, reason = rp.eligible(coworker(state="busy"), {"id": "t", "role": "executor"}, RUN, {})
        self.assertFalse(ok)
        self.assertIn("not ready", reason)

    def test_main_refused_by_default(self):
        task = {"id": "t", "role": "executor"}
        for worker, context in ((coworker("x", kind="main"), {}),
                                (coworker("y"), {"is_main": True}),
                                (coworker("main"), {})):
            ok, reason = rp.eligible(worker, task, RUN, context)
            self.assertFalse(ok)
            self.assertIn("main_takes_tasks", reason)

    def test_main_allowed_with_setting(self):
        ok, _ = rp.eligible(coworker("x", kind="main"), {"id": "t", "role": "planner"}, RUN,
                            {"settings": {"main_takes_tasks": True}})
        self.assertTrue(ok)

    def test_reviewer_refused_on_own_part(self):
        run_tasks = [{"id": "p1", "role": "executor", "worker_id": "w1"},
                     {"id": "p2", "role": "executor", "worker_id": "w2"}]
        review = {"id": "rv", "role": "reviewer", "deps": ["p1", "p2"]}
        ok, reason = rp.eligible(coworker("w1"), review, RUN, {"run_tasks": run_tasks})
        self.assertFalse(ok)
        self.assertIn("executed", reason)

    def test_reviewer_allowed_on_other_part(self):
        run_tasks = [{"id": "p1", "role": "executor", "worker_id": "w2"}]
        review = {"id": "rv", "role": "reviewer", "depends_on": ["p1"]}
        ok, _ = rp.eligible(coworker("w1"), review, RUN, {"run_tasks": run_tasks})
        self.assertTrue(ok)
        # run["tasks"] is used when the context has no run_tasks
        run = dict(RUN, tasks=[{"id": "p1", "role": "executor", "worker_id": "w1"}])
        ok, _ = rp.eligible(coworker("w1"), dict(review, reviews=["p1"]), run, {})
        self.assertFalse(ok)

    def test_read_only_role_asking_for_write_refused(self):
        for role in ("planner", "reviewer"):
            ok, reason = rp.eligible(coworker(), {"id": "t", "role": role, "tools": "read_write"}, RUN, {})
            self.assertFalse(ok)
            self.assertIn("read-only", reason)
        ok, _ = rp.eligible(coworker(), {"id": "t", "role": "reviewer", "needs_write": True}, RUN, {})
        self.assertFalse(ok)
        ok, _ = rp.eligible(coworker(), {"id": "t", "role": "executor", "tools": "read_write"}, RUN, {})
        self.assertTrue(ok)

    def test_unknown_role_refused(self):
        ok, reason = rp.eligible(coworker(), {"id": "t", "role": "boss"}, RUN, {})
        self.assertFalse(ok)
        self.assertIn("unknown role", reason)


class AffinityTest(unittest.TestCase):
    HISTORY = [
        {"worker_id": "w2", "files": ["a.py", "b.py"], "at": 1},
        {"worker_id": "w1", "files": ["a.py", "b.py"], "at": 5},
        {"worker_id": "w2", "files": ["c.py"], "at": 6},
    ]

    def test_last_holder_of_all_files(self):
        task = {"id": "t", "files": ["a.py", ".\\b.py"], "ready_at": 100}
        self.assertEqual(rp.affinity(coworker("w1"), task, self.HISTORY, now=110), 1.0)

    def test_partial_overlap(self):
        task = {"id": "t", "files": ["a.py", "c.py"], "ready_at": 100}
        self.assertAlmostEqual(rp.affinity(coworker("w1"), task, self.HISTORY, now=110), 0.5)
        self.assertAlmostEqual(rp.affinity(coworker("w2"), task, self.HISTORY, now=110), 0.5)

    def test_zero_for_others(self):
        task = {"id": "t", "files": ["a.py", "b.py"], "ready_at": 100}
        self.assertEqual(rp.affinity(coworker("w3"), task, self.HISTORY, now=110), 0.0)
        self.assertEqual(rp.affinity(coworker("w1"), {"id": "t", "files": []}, self.HISTORY), 0.0)

    def test_zero_after_default_wait(self):
        task = {"id": "t", "files": ["a.py"], "ready_at": 100}
        self.assertEqual(rp.affinity(coworker("w1"), task, self.HISTORY, now=130), 1.0)
        self.assertEqual(rp.affinity(coworker("w1"), task, self.HISTORY, now=131), 0.0)

    def test_custom_wait_setting(self):
        task = {"id": "t", "files": ["a.py"], "queued_at": 100}
        self.assertEqual(rp.affinity(coworker("w1"), task, self.HISTORY, now=150,
                                     settings={"max_affinity_wait_s": 60}), 1.0)
        self.assertEqual(rp.affinity(coworker("w1"), task, self.HISTORY, now=110,
                                     settings={"max_affinity_wait_s": 5}), 0.0)


if __name__ == "__main__":
    unittest.main()
