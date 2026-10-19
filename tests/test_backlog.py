import json
import os
import tempfile
import unittest
from unittest import mock

from notiontool import backlog


class BacklogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self._tmp.name, "queue")

    def tearDown(self):
        self._tmp.cleanup()

    def add(self, item_id, created, **extra):
        return backlog.add(dict(id=item_id, goal="goal " + item_id, created=created, **extra), self.dir)

    def test_add_and_load_roundtrip(self):
        item = self.add("a", 10, priority=2, deps=["x"], approvals=["notiontool\\engine.py"], request_id="r1")
        self.assertEqual(backlog.load(self.dir), [item])
        self.assertEqual(item["approvals"], ["notiontool/engine.py"])
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "a.json")))

    def test_add_fills_defaults(self):
        item = backlog.add({"goal": "  do it  "}, self.dir, now=5)
        self.assertEqual(item["goal"], "do it")
        self.assertEqual(item["priority"], 0)
        self.assertEqual(item["deps"], [])
        self.assertEqual(item["approvals"], [])
        self.assertEqual(item["state"], "queued")
        self.assertIsNone(item["submitted_run_id"])
        self.assertIsNone(item["submitted_at"])
        self.assertEqual(item["created"], 5.0)
        self.assertEqual(item["request_id"], "backlog:" + item["id"])

    def test_add_dedupes_by_request_id(self):
        first = self.add("a", 1, request_id="same")
        again = backlog.add(dict(id="b", goal="other", request_id="same"), self.dir)
        self.assertEqual(again, first)
        self.assertEqual([i["id"] for i in backlog.load(self.dir)], ["a"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "b.json")))

    def test_add_rejects_bad_items(self):
        with self.assertRaises(backlog.BacklogError):
            backlog.add({"goal": "   "}, self.dir)
        with self.assertRaises(backlog.BacklogError):
            backlog.add({"goal": "g", "id": "../escape"}, self.dir)
        with self.assertRaises(backlog.BacklogError):
            backlog.add({"goal": "g", "state": "running"}, self.dir)
        self.add("a", 1, request_id="r1")
        with self.assertRaises(backlog.BacklogError):
            self.add("a", 2, request_id="r2")

    def test_load_sorted_by_created_and_missing_dir(self):
        self.assertEqual(backlog.load(self.dir), [])
        self.add("late", 30)
        self.add("early", 10)
        self.add("mid", 20)
        self.assertEqual([i["id"] for i in backlog.load(self.dir)], ["early", "mid", "late"])

    def test_next_ready_priority_order(self):
        self.add("low", 1, priority=0)
        self.add("high", 3, priority=5)
        self.add("mid", 2, priority=1)
        self.assertEqual(backlog.next_ready(backlog.load(self.dir), set())["id"], "high")

    def test_next_ready_age_then_id_tie_break(self):
        items = [dict(id="b", priority=1, created=5, state="queued", deps=[]),
                 dict(id="c", priority=1, created=3, state="queued", deps=[]),
                 dict(id="a", priority=1, created=3, state="queued", deps=[])]
        self.assertEqual(backlog.next_ready(items, [])["id"], "a")
        self.assertEqual(backlog.next_ready(items[:2], [])["id"], "c")

    def test_next_ready_deps_block_and_release(self):
        self.add("first", 2, priority=0)
        self.add("second", 1, priority=9, deps=["first"])
        items = backlog.load(self.dir)
        self.assertEqual(backlog.next_ready(items, set())["id"], "first")
        backlog.mark("first", "submitted", "r-1", self.dir)
        items = backlog.load(self.dir)
        self.assertIsNone(backlog.next_ready(items, backlog.done_ids(items)))
        backlog.mark("first", "done", dir=self.dir)
        items = backlog.load(self.dir)
        self.assertEqual(backlog.done_ids(items), {"first"})
        self.assertEqual(backlog.next_ready(items, backlog.done_ids(items))["id"], "second")

    def test_next_ready_missing_dep_blocks(self):
        self.add("only", 1, deps=["never-created"])
        items = backlog.load(self.dir)
        self.assertIsNone(backlog.next_ready(items, {"something-else"}))
        self.assertEqual(backlog.next_ready(items, {"never-created"})["id"], "only")

    def test_next_ready_skips_states_other_than_queued(self):
        items = [dict(id=s, priority=0, created=1, state=s, deps=[]) for s in ("submitted", "done", "failed")]
        self.assertIsNone(backlog.next_ready(items, []))
        self.assertIsNone(backlog.next_ready([], []))

    def test_mark_each_state(self):
        self.add("a", 1)
        sub = backlog.mark("a", "submitted", "run-7", self.dir, now=100)
        self.assertEqual((sub["state"], sub["submitted_run_id"], sub["submitted_at"]), ("submitted", "run-7", 100.0))
        done = backlog.mark("a", "done", dir=self.dir, now=200)
        self.assertEqual((done["state"], done["submitted_run_id"], done["submitted_at"]), ("done", "run-7", 100.0))
        failed = backlog.mark("a", "failed", dir=self.dir)
        self.assertEqual(failed["state"], "failed")
        queued = backlog.mark("a", "queued", dir=self.dir)
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(backlog.load(self.dir), [queued])
        with self.assertRaises(backlog.BacklogError):
            backlog.add({"goal": "g", "id": "b"}, self.dir)
            backlog.mark("b", "submitted", dir=self.dir)

    def test_mark_unknown_id(self):
        with self.assertRaises(backlog.BacklogError) as ctx:
            backlog.mark("nope", "done", dir=self.dir)
        self.assertIn("Unknown backlog item", str(ctx.exception))

    def test_mark_invalid_state(self):
        self.add("a", 1)
        with self.assertRaises(backlog.BacklogError) as ctx:
            backlog.mark("a", "running", dir=self.dir)
        self.assertIn("Unknown backlog state", str(ctx.exception))
        self.assertEqual(backlog.load(self.dir)[0]["state"], "queued")

    def test_atomic_write_leaves_no_temp_and_full_content(self):
        self.add("a", 1, goal_note="ignored")
        backlog.mark("a", "submitted", "r", self.dir)
        self.assertEqual(sorted(os.listdir(self.dir)), ["a.json"])
        with open(os.path.join(self.dir, "a.json"), "rb") as handle:
            raw = handle.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\n", raw)
        self.assertTrue(raw.endswith(b"\n"))
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(set(data), {"id", "goal", "priority", "deps", "approvals", "request_id", "state",
                                     "submitted_run_id", "created", "submitted_at"})

    def test_failed_replace_keeps_old_file_and_removes_temp(self):
        self.add("a", 1)
        path = os.path.join(self.dir, "a.json")
        with open(path, "rb") as handle:
            before = handle.read()
        with mock.patch("notiontool.backlog.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                backlog.mark("a", "done", dir=self.dir)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertEqual(os.listdir(self.dir), ["a.json"])

    def test_legacy_txt_goal(self):
        os.makedirs(self.dir)
        txt = os.path.join(self.dir, "old-goal.txt")
        with open(txt, "wb") as handle:
            handle.write(b"\xef\xbb\xbfFix the thing\r\n")
        os.utime(txt, (50, 50))
        [item] = backlog.load(self.dir)
        self.assertEqual(item["id"], "old-goal")
        self.assertEqual(item["goal"], "Fix the thing")
        self.assertEqual(item["priority"], backlog.DEFAULT_PRIORITY)
        self.assertEqual(item["request_id"], "legacy:old-goal.txt")
        self.assertEqual(item["state"], "queued")
        self.assertEqual(item["created"], 50.0)

    def test_legacy_txt_never_changed_and_mark_writes_json(self):
        os.makedirs(self.dir)
        txt = os.path.join(self.dir, "legacy.txt")
        with open(txt, "wb") as handle:
            handle.write(b"Legacy goal\n")
        backlog.mark("legacy", "submitted", "run-1", self.dir)
        with open(txt, "rb") as handle:
            self.assertEqual(handle.read(), b"Legacy goal\n")
        [item] = backlog.load(self.dir)
        self.assertEqual((item["state"], item["submitted_run_id"]), ("submitted", "run-1"))
        dup = backlog.add({"goal": "again", "request_id": "legacy:legacy.txt"}, self.dir)
        self.assertEqual(dup["id"], "legacy")

    def test_broken_json_skipped_and_reported(self):
        self.add("good", 1)
        with open(os.path.join(self.dir, "bad.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with open(os.path.join(self.dir, "empty.txt"), "w", encoding="utf-8") as handle:
            handle.write("   ")
        errors = []
        items = backlog.load(self.dir, errors=errors)
        self.assertEqual([i["id"] for i in items], ["good"])
        self.assertEqual(sorted(name for name, _ in errors), ["bad.json", "empty.txt"])
        self.assertEqual(backlog.load(self.dir), items)


if __name__ == "__main__":
    unittest.main()
