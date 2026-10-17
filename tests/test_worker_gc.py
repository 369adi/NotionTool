"""Tests for notiontool.worker_gc (pure, no store, no network)."""

import copy
import unittest

from notiontool import worker_gc as gc


def W(worker_id, thread=None, role="coworker", state="ready", **extra):
    worker = {"id": worker_id, "role": role, "thread_id": thread, "state": state}
    worker.update(extra)
    return worker


def actions(result):
    return {entry["worker_id"]: entry["action"] for entry in result}


def reasons(result):
    return {entry["worker_id"]: entry["reason"] for entry in result}


class PlanCleanupTests(unittest.TestCase):
    def test_empty_inputs(self):
        self.assertEqual(gc.plan_cleanup([], [], [], {}), [])
        self.assertEqual(gc.plan_cleanup(None, None, None, None), [])

    def test_dead_thread_is_retired(self):
        result = gc.plan_cleanup([W("w1", "th1")], [], [], {"th1": False})
        self.assertEqual(actions(result), {"w1": "retire"})
        self.assertIn("not alive", result[0]["reason"])

    def test_missing_thread_id_is_retired(self):
        result = gc.plan_cleanup([W("w1", None), W("w2", "")], [], [], {})
        self.assertEqual(actions(result), {"w1": "retire", "w2": "retire"})
        self.assertIn("no thread_id", result[0]["reason"])

    def test_missing_record_is_retired(self):
        result = gc.plan_cleanup([W("w1", "th1", state="missing")], [], [], {"th1": True})
        self.assertEqual(actions(result), {"w1": "retire"})
        self.assertIn("missing", result[0]["reason"])

    def test_uncertain_thread_is_kept(self):
        workers = [W("w1", "th1"), W("w2", "th2")]
        result = gc.plan_cleanup(workers, [], [], {"th1": None})
        self.assertEqual(actions(result), {"w1": "keep", "w2": "keep"})
        self.assertTrue(all("uncertain" in e["reason"] for e in result))

    def test_alive_thread_is_kept(self):
        result = gc.plan_cleanup([W("w1", "th1")], [], [], {"th1": True})
        self.assertEqual(actions(result), {"w1": "keep"})
        self.assertIn("alive", result[0]["reason"])

    def test_duplicate_thread_keeps_newest(self):
        workers = [W("a", "th", created=1), W("b", "th", created=5), W("c", "th", created=3)]
        result = gc.plan_cleanup(workers, [], [], {"th": True})
        self.assertEqual(actions(result), {"a": "retire", "b": "keep", "c": "retire"})
        self.assertIn("worker b holds the same thread", result[0]["reason"])

    def test_duplicate_thread_prefers_chat_then_running(self):
        workers = [W("a", "th", created=1), W("b", "th", created=9), W("c", "th", created=5)]
        tasks = [{"id": "t1", "worker_id": "c", "state": "running"}]
        result = gc.plan_cleanup(workers, tasks, [], {"th": True})
        self.assertEqual(actions(result), {"a": "retire", "b": "retire", "c": "keep"})
        chats = [{"id": "chat1", "main_worker_id": "a"}]
        workers[0]["role"] = "main"
        result = gc.plan_cleanup(workers, [], chats, {"th": True})
        self.assertEqual(actions(result), {"a": "keep", "b": "retire", "c": "retire"})

    def test_dead_record_never_wins_a_duplicate(self):
        workers = [W("old", "th", created=1), W("new", "th", state="missing", created=9)]
        result = gc.plan_cleanup(workers, [], [], {"th": True})
        self.assertEqual(actions(result), {"new": "retire", "old": "keep"})
        why = reasons(result)
        self.assertIn("state missing", why["new"])
        self.assertIn("is alive", why["old"])

    def test_dead_protected_main_does_not_retire_live_duplicate(self):
        workers = [W("m", "th", role="main", state="missing", created=9), W("c", "th", created=1)]
        chats = [{"id": "chat1", "main_worker_id": "m"}]
        result = gc.plan_cleanup(workers, [], chats, {"th": True})
        self.assertEqual(actions(result), {"c": "keep", "m": "reassign_tasks"})

    def test_uncertain_duplicates_are_kept(self):
        for alive in ({"th": None}, {}):
            workers = [W("a", "th", created=1), W("b", "th", created=5),
                       W("x", "th", state="missing", created=9)]
            result = gc.plan_cleanup(workers, [], [], alive)
            self.assertEqual(actions(result), {"a": "keep", "b": "keep", "x": "retire"}, alive)
            why = reasons(result)
            self.assertEqual(why["a"], "duplicate on uncertain thread; kept until liveness is known")
            self.assertIn("uncertain", why["b"])

    def test_main_pointed_by_chat_is_never_retired(self):
        main = W("m", "th", role="main", state="missing")
        chats = [{"id": "chat1", "main_worker_id": "m"}]
        for alive in ({"th": False}, {"th": True}, {}):
            result = gc.plan_cleanup([main], [], chats, alive)
            self.assertNotEqual(result[0]["action"], "retire", alive)
        result = gc.plan_cleanup([main], [], chats, {"th": False})
        self.assertEqual(result[0]["action"], "reassign_tasks")
        unpointed = gc.plan_cleanup([W("m", "th", role="main")], [], [], {"th": False})
        self.assertEqual(unpointed[0]["action"], "retire")

    def test_running_task_gives_reassign_not_retire(self):
        tasks = [{"id": "t1", "worker_id": "w1", "state": "running"}]
        result = gc.plan_cleanup([W("w1", "th1")], tasks, [], {"th1": False})
        self.assertEqual(actions(result), {"w1": "reassign_tasks"})
        result = gc.plan_cleanup([W("w1", None)], tasks, [], {})
        self.assertEqual(actions(result), {"w1": "reassign_tasks"})
        result = gc.plan_cleanup([W("w1", "th1")], tasks, [], {"th1": True})
        self.assertEqual(actions(result), {"w1": "keep"})

    def test_only_queued_tasks_are_reassigned(self):
        tasks = [
            {"id": "t1", "worker_id": "w1", "state": "running"},
            {"id": "t2", "worker_id": "w1", "state": "queued"},
            {"id": "t3", "worker_id": "w1", "state": "done"},
            {"id": "t4", "worker_id": "w1", "state": "queued"},
            {"id": "t5", "worker_id": "w2", "state": "queued"},
        ]
        result = gc.plan_cleanup([W("w1", "th1")], tasks, [], {"th1": False})
        reason = result[0]["reason"]
        self.assertTrue(reason.endswith("reassign queued tasks: t2, t4"), reason)
        self.assertEqual(reason.split("reassign queued tasks: ")[1], "t2, t4")
        self.assertNotIn("t3", reason)
        self.assertNotIn("t5", reason)

    def test_order_is_stable_and_reasons_are_english(self):
        workers = [W("w3", "a"), W("w1", "b"), W("w2", None), W("w10", "c")]
        alive = {"a": True, "b": False}
        first = gc.plan_cleanup(workers, [], [], alive)
        again = gc.plan_cleanup(list(reversed(workers)), [], [], alive)
        self.assertEqual([e["worker_id"] for e in first], ["w1", "w10", "w2", "w3"])
        self.assertEqual(first, again)
        for entry in first:
            self.assertEqual(set(entry), {"worker_id", "action", "reason"})
            self.assertTrue(entry["reason"].strip())
            self.assertTrue(entry["reason"].isascii())

    def test_inputs_not_mutated(self):
        workers = [W("a", "th", created=1), W("b", "th", created=2)]
        tasks = [{"id": "t", "worker_id": "a", "state": "queued"}]
        chats = [{"id": "c", "main_worker_id": "b"}]
        alive = {"th": None}
        before = copy.deepcopy((workers, tasks, chats, alive))
        gc.plan_cleanup(workers, tasks, chats, alive)
        self.assertEqual((workers, tasks, chats, alive), before)


class CountPoolTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(gc.count_pool([]), {"main": 0, "coworkers_ready": 0, "coworkers_busy": 0, "stale": 0})
        self.assertEqual(gc.count_pool(None)["stale"], 0)

    def test_each_category(self):
        workers = [W("m", "t0", role="main"), W("r1", "t1"), W("r2", "t2"),
                   W("b1", "t3", state="busy"), W("x", "t4", state="missing"),
                   W("e", "t5", state="error")]
        self.assertEqual(gc.count_pool(workers),
                         {"main": 1, "coworkers_ready": 2, "coworkers_busy": 1, "stale": 2})

    def test_main_by_purpose_and_unknown_states_are_stale(self):
        workers = [{"id": "m", "purpose": "main", "state": "busy"},
                   W("u1", "t1", state="zombie"), W("u2", "t2", state=None),
                   {"id": "u3"}, W("m2", "t3", role="main", state="login")]
        self.assertEqual(gc.count_pool(workers),
                         {"main": 1, "coworkers_ready": 0, "coworkers_busy": 0, "stale": 4})


if __name__ == "__main__":
    unittest.main()
