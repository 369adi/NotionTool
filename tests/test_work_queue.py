"""Tests for notiontool.work_queue (pure planning, no store, no network)."""

import copy
import unittest
from unittest import mock

from notiontool import dag
from notiontool import work_queue as wq

NOW = 10000.0


def T(task_id, run="r1", role="executor", files=(), dur=None, age=10, deps=(), **extra):
    task = {"id": task_id, "run_id": run, "role": role, "files": list(files),
            "estimated_duration": dur, "queued_at": NOW - age,
            "depends_on": list(deps), "state": "queued"}
    task.update(extra)
    return task


def W(worker_id, **extra):
    worker = {"id": worker_id, "enabled": True, "state": "ready"}
    worker.update(extra)
    return worker


def plan(ready, workers, running=(), settings=None, **kwargs):
    return wq.plan_assignments(ready, workers, list(running), NOW, settings or {}, **kwargs)


class PlanOrderTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(plan([], [W("w1")]), [])
        self.assertEqual(plan([T("a")], []), [])
        self.assertEqual(wq.plan_assignments(None, None, None, NOW, None), [])

    def test_critical_path_ordering(self):
        a = T("a", dur=1)
        b = T("b", dur=1)
        c = T("c", dur=10, deps=["b"])
        result = plan([a, b], [W("w1")], run_tasks={"r1": [a, b, c]})
        self.assertEqual([r[:2] for r in result], [("b", "w1")])
        self.assertTrue(result[0][2].startswith("critical path"), result[0][2])

    def test_longer_task_goes_first_in_same_run(self):
        result = plan([T("a", dur=1), T("b", dur=9)], [W("w1")])
        self.assertEqual(result[0][0], "b")

    def test_role_rank_breaks_ties(self):
        ready = [T("a", run="r1", role="executor"), T("b", run="r2", role="reviewer"),
                 T("c", run="r3", role="integrator"), T("d", run="r4", role="planner")]
        result = plan(ready, [W("w1"), W("w2"), W("w3"), W("w4")])
        self.assertEqual([r[0] for r in result], ["d", "b", "c", "a"])
        self.assertTrue(result[0][2].startswith("role rank"), result[0][2])

    def test_age_breaks_ties(self):
        ready = [T("a", run="r1", age=10), T("b", run="r2", age=50)]
        result = plan(ready, [W("w1")])
        self.assertEqual(result[0][0], "b")
        self.assertTrue(result[0][2].startswith("age"), result[0][2])

    def test_starvation_beats_critical_path_at_301_not_299(self):
        long_task = T("a", dur=10, age=5)
        result = plan([long_task, T("b", dur=1, age=301)], [W("w1")])
        self.assertEqual(result[0][0], "b")
        self.assertTrue(result[0][2].startswith("starving"), result[0][2])
        result = plan([long_task, T("b", dur=1, age=299)], [W("w1")])
        self.assertEqual(result[0][0], "a")

    def test_starving_oldest_first_and_custom_threshold(self):
        ready = [T("a", run="r1", age=400), T("b", run="r2", age=900)]
        self.assertEqual(plan(ready, [W("w1")])[0][0], "b")
        ready = [T("a", dur=10, age=5), T("b", dur=1, age=61)]
        self.assertEqual(plan(ready, [W("w1")], settings={"starve_after_s": 60})[0][0], "b")

    def test_critical_path_called_once_per_run(self):
        ready = [T("a", run="r1"), T("b", run="r1"), T("c", run="r2")]
        with mock.patch.object(wq.dag, "critical_path", wraps=dag.critical_path) as spy:
            plan(ready, [W("w1")])
        self.assertEqual(spy.call_count, 2)


class ConflictTests(unittest.TestCase):
    def test_shared_file_not_handed_out_twice(self):
        ready = [T("a", files=["x/a.py"]), T("b", run="r2", files=["X\\a.py"])]
        result = plan(ready, [W("w1"), W("w2")])
        self.assertEqual(len(result), 1)

    def test_file_held_by_running_task_blocks(self):
        running = [{"id": "z", "run_id": "r9", "state": "running", "files": ["f.py"]}]
        ready = [T("a", files=["F.py"], dur=5), T("b", run="r2", files=["g.py"])]
        result = plan(ready, [W("w1"), W("w2")], running=running)
        self.assertEqual([r[0] for r in result], ["b"])

    def test_mixed_runs_in_one_call(self):
        ready = [T("a", run="r1"), T("b", run="r2"), T("c", run="r3")]
        result = plan(ready, [W("w1"), W("w2"), W("w3")])
        self.assertEqual(sorted(r[0] for r in result), ["a", "b", "c"])
        self.assertEqual(len({r[1] for r in result}), 3)

    def test_more_tasks_than_workers(self):
        ready = [T("t%d" % i, run="r%d" % i) for i in range(5)]
        result = plan(ready, [W("w1"), W("w2")])
        self.assertEqual(len(result), 2)
        self.assertEqual(sorted(r[1] for r in result), ["w1", "w2"])


class WorkerChoiceTests(unittest.TestCase):
    def test_eligibility_callback_refuses(self):
        refuse_w1 = lambda worker, task: (worker["id"] != "w1", "test rule")
        result = plan([T("a")], [W("w1"), W("w2")], eligible=refuse_w1)
        self.assertEqual(result[0][1], "w2")
        self.assertEqual(plan([T("a")], [W("w1")], eligible=lambda w, t: False), [])

    def test_affinity_chooses_worker(self):
        self.assertEqual(plan([T("a")], [W("w1"), W("w2")])[0][1], "w1")
        score = lambda worker, task: 5 if worker["id"] == "w2" else 1
        result = plan([T("a")], [W("w1"), W("w2")], affinity=score)
        self.assertEqual(result[0][1], "w2")
        self.assertIn("affinity 5", result[0][2])

    def test_inputs_not_mutated(self):
        ready = [T("a", files=["a.py"]), T("b", run="r2")]
        workers = [W("w1"), W("w2")]
        running = [{"id": "z", "state": "running", "files": ["q.py"]}]
        before = copy.deepcopy((ready, workers, running))
        plan(ready, workers, running=running)
        self.assertEqual((ready, workers, running), before)


class RequeueTests(unittest.TestCase):
    def test_requeue_orphans(self):
        workers = [W("ok"), W("busy", state="busy"), W("off", enabled=False),
                   W("down", state="offline")]
        tasks = [
            {"id": "t1", "state": "running", "worker_id": "gone", "attempts": 1},
            {"id": "t2", "state": "assigned", "worker_id": "off", "attempts": 2},
            {"id": "t3", "state": "running", "worker_id": "down", "attempts": 0},
            {"id": "t4", "state": "running", "worker_id": "busy", "lease_expires": NOW - 1, "attempts": 3},
            {"id": "t5", "state": "running", "worker_id": "busy", "lease_expires": NOW + 60},
            {"id": "t6", "state": "queued", "worker_id": "gone"},
            {"id": "t7", "state": "assigned", "worker_id": "ok"},
        ]
        before = copy.deepcopy((tasks, workers))
        result = wq.requeue_orphans(tasks, workers, NOW)
        self.assertEqual([r["task_id"] for r in result], ["t1", "t2", "t3", "t4"])
        reasons = [r["reason"] for r in result]
        self.assertIn("missing", reasons[0])
        self.assertIn("disabled", reasons[1])
        self.assertIn("not ready", reasons[2])
        self.assertIn("lease expired", reasons[3])
        self.assertEqual((tasks, workers), before)
        self.assertTrue(all(set(r) == {"task_id", "reason"} for r in result))

    def test_lease_equal_to_now_is_not_expired(self):
        tasks = [{"id": "t", "state": "running", "worker_id": "w", "lease_expires": NOW}]
        self.assertEqual(wq.requeue_orphans(tasks, {"w": W("w")}, NOW), [])


class DepsFallbackTests(unittest.TestCase):
    def test_deps_only_keeps_critical_path_order(self):
        tasks = [
            {"id": "a", "run_id": "r1", "role": "executor", "files": [], "estimated_duration": 1,
             "queued_at": NOW - 10, "state": "queued"},
            {"id": "b", "run_id": "r1", "role": "executor", "files": [], "estimated_duration": 1,
             "queued_at": NOW - 10, "state": "queued"},
            {"id": "c", "run_id": "r1", "role": "executor", "files": [], "estimated_duration": 10,
             "queued_at": NOW - 10, "state": "queued", "deps": ["b"]},
        ]
        self.assertFalse(any("depends_on" in task for task in tasks))
        result = plan(tasks, [W("w1")])
        self.assertEqual([(r[0], r[1]) for r in result], [("b", "w1")])
        self.assertTrue(result[0][2].startswith("critical path"), result[0][2])


class RequeueWithoutNowTests(unittest.TestCase):
    def test_two_arguments_use_current_time_for_leases(self):
        workers = [W("busy", state="busy"), W("off", enabled=False), W("down", state="offline")]
        tasks = [
            {"id": "t1", "state": "running", "worker_id": "gone"},
            {"id": "t2", "state": "assigned", "worker_id": "off"},
            {"id": "t3", "state": "running", "worker_id": "down"},
            {"id": "t4", "state": "running", "worker_id": "busy", "lease_expires": NOW - 1},
        ]
        tasks.append({"id": "t5", "state": "running", "worker_id": "busy", "lease_expires": 4102444800})
        result = wq.requeue_orphans(tasks, workers)
        self.assertEqual([r["task_id"] for r in result], ["t1", "t2", "t3", "t4"])
        self.assertIn("lease expired", result[3]["reason"])

if __name__ == "__main__":
    unittest.main()
