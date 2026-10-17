import copy
import json
import unittest

from notiontool.delegation import DelegationError, child_id, resume_ready, validate_delegation


def parent(**extra):
    task = dict(id="tP", run_id="r1", role="executor", deps=[], files=["src/a.py"], state="running", depth=0)
    task.update(extra)
    return task


def child(name, files=(), deps=(), role="executor"):
    return dict(id=name, role=role, prompt="Do part %s." % name, files=list(files), deps=list(deps))


def run_tasks(*extra):
    base = [parent(), dict(id="tX", run_id="r1", role="executor", deps=[], files=[], state="done")]
    return base + list(extra)


def delegated(count):
    return [dict(id="d%d" % i, run_id="r1", role="executor", deps=[], files=[], state="done", parent_id="tZ")
            for i in range(count)]


class ValidateDelegationTests(unittest.TestCase):
    def refused(self, *args, contains=""):
        with self.assertRaises(DelegationError) as ctx:
            validate_delegation(*args)
        text = str(ctx.exception)
        self.assertTrue(text and text.endswith("."), text)
        self.assertEqual(ctx.exception.reason, text)
        if contains:
            self.assertIn(contains, text)
        return text

    def test_valid_delegation_returns_child_specs(self):
        request = {"children": [child("a", ["src/a.py"]), child("b", ["src\\b.py"], deps=["a"])]}
        specs, update = validate_delegation(parent(), request, run_tasks(), {"src/a.py": "tP"}, {})
        self.assertEqual([s["id"] for s in specs], ["tP.c1", "tP.c2"])
        self.assertEqual(specs[1]["deps"], ["tP.c1"])
        self.assertEqual(specs[1]["files"], ["src/b.py"])
        for spec in specs:
            self.assertEqual((spec["parent_id"], spec["run_id"], spec["depth"], spec["state"]),
                             ("tP", "r1", 1, "queued"))
            self.assertEqual(set(spec), {"id", "parent_id", "run_id", "role", "title", "prompt",
                                         "files", "deps", "depth", "state"})

    def test_ids_are_deterministic(self):
        request = {"children": [child("a"), child("b")]}
        first, _ = validate_delegation(parent(), request, run_tasks(), {}, {})
        second, _ = validate_delegation(parent(), request, run_tasks(), {}, {})
        self.assertEqual(first, second)
        self.assertEqual(child_id("tP", 0), "tP.c1")

    def test_depth_three_refused_and_depth_two_allowed(self):
        request = {"children": [child("a")]}
        self.refused(parent(depth=2), request, run_tasks(), {}, {}, contains="depth")
        specs, _ = validate_delegation(parent(depth=1), request, run_tasks(), {}, {})
        self.assertEqual(specs[0]["depth"], 2)

    def test_five_children_refused(self):
        request = {"children": [child(name) for name in "abcde"]}
        self.refused(parent(), request, run_tasks(), {}, {}, contains="at most 4")
        specs, _ = validate_delegation(parent(), {"children": request["children"][:4]}, run_tasks(), {}, {})
        self.assertEqual(len(specs), 4)

    def test_file_claimed_by_another_task_refused(self):
        request = {"children": [child("a", ["src\\c.py"])]}
        self.refused(parent(), request, run_tasks(), {"src/c.py": "tOther"}, {}, contains="tOther")

    def test_file_claimed_by_parent_allowed(self):
        request = {"children": [child("a", ["SRC/A.py"]), child("b", ["src/new.py"])]}
        specs, _ = validate_delegation(parent(), request, run_tasks(), {"src/a.py": "tP"}, {})
        self.assertEqual(specs[0]["files"], ["SRC/A.py"])

    def test_cycle_among_children_refused(self):
        request = {"children": [child("a", deps=["b"]), child("b", deps=["a"])]}
        self.refused(parent(), request, run_tasks(), {}, {}, contains="cycle")

    def test_cycle_through_run_task_refused(self):
        waiting = dict(id="tY", run_id="r1", role="executor", deps=["tP"], files=[], state="queued")
        request = {"children": [child("a", deps=["tY"])]}
        text = self.refused(parent(), request, run_tasks(waiting), {}, {}, contains="cycle")
        self.assertIn("tY", text)

    def test_child_waiting_for_parent_refused(self):
        request = {"children": [child("a", deps=["tP"])]}
        self.refused(parent(), request, run_tasks(), {}, {}, contains="cycle")

    def test_unknown_dep_refused_and_run_task_dep_allowed(self):
        self.refused(parent(), {"children": [child("a", deps=["nope"])]}, run_tasks(), {}, {}, contains="unknown")
        specs, _ = validate_delegation(parent(), {"children": [child("a", deps=["tX"])]}, run_tasks(), {}, {})
        self.assertEqual(specs[0]["deps"], ["tX"])

    def test_run_limit_default_is_12(self):
        request = {"children": [child("a"), child("b"), child("c")]}
        self.refused(parent(), request, run_tasks(*delegated(10)), {}, {}, contains="max_delegated_per_run (12)")
        specs, _ = validate_delegation(parent(), {"children": request["children"][:2]},
                                       run_tasks(*delegated(10)), {}, None)
        self.assertEqual(len(specs), 2)

    def test_run_limit_custom_setting(self):
        request = {"children": [child("a"), child("b"), child("c")]}
        self.refused(parent(), request, run_tasks(), {}, {"max_delegated_per_run": 2}, contains="(2)")
        specs, _ = validate_delegation(parent(), request, run_tasks(), {}, {"max_delegated_per_run": 3})
        self.assertEqual(len(specs), 3)
        self.refused(parent(), request, run_tasks(), {}, {"max_delegated_per_run": "many"}, contains="integer")

    def test_parent_update_frees_the_chat(self):
        _, update = validate_delegation(parent(), {"children": [child("a"), child("b")]}, run_tasks(), {}, {})
        self.assertEqual(update, {"state": "waiting_children", "children": ["tP.c1", "tP.c2"],
                                  "release_worker": True})

    def test_inputs_are_not_mutated(self):
        args = [parent(), {"children": [child("a", ["src\\a.py"], deps=["tX"]), child("b", deps=["a"])]},
                run_tasks(), {"src/a.py": "tP"}, {"max_delegated_per_run": 5}]
        before = copy.deepcopy(args)
        validate_delegation(*args)
        self.assertEqual(args, before)

    def test_bad_requests_have_english_reasons(self):
        self.refused(parent(id=""), {"children": [child("a")]}, run_tasks(), {}, {}, contains="no id")
        self.refused(parent(), {"children": []}, run_tasks(), {}, {}, contains="non-empty")
        self.refused(parent(), {"children": [child("a", role="boss")]}, run_tasks(), {}, {}, contains="role")
        bad_prompt = dict(child("a"), prompt="  ")
        self.refused(parent(), {"children": [bad_prompt]}, run_tasks(), {}, {}, contains="prompt")
        self.refused(parent(), {"children": [child("a"), child("a")]}, run_tasks(), {}, {}, contains="share")


class ResumeReadyTests(unittest.TestCase):
    def kids(self, *states):
        return [dict(id="tP.c%d" % (i + 1), state=state,
                     result=json.dumps({"status": state, "summary": "Result %d" % (i + 1)}))
                for i, state in enumerate(states)]

    def test_all_done_merges_summary_in_child_order(self):
        waiting = parent(state="waiting_children", children=["tP.c1", "tP.c2"])
        ready, info = resume_ready(waiting, list(reversed(self.kids("done", "done"))))
        self.assertTrue(ready)
        self.assertEqual(info["summary"], "[tP.c1] Result 1\n\n[tP.c2] Result 2")
        self.assertEqual(info["parent_update"], {"state": "queued"})

    def test_one_pending_is_not_ready(self):
        ready, info = resume_ready(parent(children=["tP.c1", "tP.c2"]), self.kids("done", "running"))
        self.assertFalse(ready)
        self.assertEqual(info, {"pending": ["tP.c2"]})

    def test_failed_child_marks_parent_for_repair(self):
        ready, info = resume_ready(parent(children=["tP.c1", "tP.c2", "tP.c3"]),
                                   self.kids("done", "failed", "queued"))
        self.assertFalse(ready)
        self.assertEqual(info, {"parent_update": {"state": "needs_repair", "failed_children": ["tP.c2"]}})

    def test_missing_child_counts_as_pending_and_empty_raises(self):
        ready, info = resume_ready(parent(children=["tP.c1", "tP.c2"]), self.kids("done"))
        self.assertEqual((ready, info), (False, {"pending": ["tP.c2"]}))
        with self.assertRaises(DelegationError):
            resume_ready(parent(), [])

    def test_resume_does_not_mutate_inputs(self):
        waiting, children = parent(children=["tP.c1"]), self.kids("done")
        before = copy.deepcopy((waiting, children))
        resume_ready(waiting, children)
        self.assertEqual((waiting, children), before)


if __name__ == "__main__":
    unittest.main()
