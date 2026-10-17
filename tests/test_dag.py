import copy
import unittest

from notiontool import dag


def task(task_id, deps=(), state="queued", files=(), duration=None):
    item = {"id": task_id, "depends_on": list(deps), "state": state, "files": list(files)}
    if duration is not None:
        item["estimated_duration"] = duration
    return item


class CycleTests(unittest.TestCase):
    def test_no_cycle_in_chain(self):
        tasks = [task("a"), task("b", ["a"]), task("c", ["b"])]
        self.assertIsNone(dag.find_cycle(tasks))

    def test_three_node_cycle_path(self):
        tasks = [task("a", ["c"]), task("b", ["a"]), task("c", ["b"]), task("d")]
        cycle = dag.find_cycle(tasks)
        self.assertEqual(cycle, ["a", "c", "b", "a"])
        index = {t["id"]: t for t in tasks}
        for here, nxt in zip(cycle, cycle[1:]):
            self.assertIn(nxt, index[here]["depends_on"])

    def test_self_dependency_is_cycle(self):
        self.assertEqual(dag.find_cycle([task("a", ["a"])]), ["a", "a"])

    def test_unknown_dependency_is_not_cycle(self):
        self.assertIsNone(dag.find_cycle([task("a", ["zz"])]))

    def test_duplicate_id_raises(self):
        with self.assertRaises(ValueError):
            dag.find_cycle([task("a"), task("a")])


class MissingAndReadyTests(unittest.TestCase):
    def test_missing_dependencies(self):
        tasks = [task("a"), task("b", ["a", "x", "x", "y"]), task("c", ["a"])]
        self.assertEqual(dag.missing_dependencies(tasks), {"b": ["x", "y"]})

    def test_ready_set_states_and_deps(self):
        tasks = [
            task("a", state="done"),
            task("b", ["a"]),
            task("c", ["b"]),
            task("d", state="running"),
            task("e", state="failed"),
            task("f"),
            task("g", ["d"]),
        ]
        self.assertEqual(dag.ready_set(tasks), ["b", "f"])

    def test_missing_dependency_never_ready(self):
        tasks = [task("a", ["ghost"]), task("b")]
        self.assertEqual(dag.ready_set(tasks), ["b"])


class CriticalPathTests(unittest.TestCase):
    def test_longest_chain(self):
        tasks = [
            task("a", duration=2),
            task("b", ["a"], duration=5),
            task("c", ["a"], duration=1),
            task("d", ["b", "c"], duration=1),
        ]
        self.assertEqual(dag.critical_path(tasks), (["a", "b", "d"], 8))

    def test_ties_go_to_smaller_id(self):
        # Predecessor tie: a and b both 3, so d goes through a.
        tasks = [
            task("b", duration=3),
            task("a", duration=3),
            task("d", ["a", "b"], duration=1),
            task("c", duration=3),
        ]
        self.assertEqual(dag.critical_path(tasks), (["a", "d"], 4))
        # End tie: c alone is 4 and d is 4, so the smaller id c wins.
        tasks[3] = task("c", duration=4)
        self.assertEqual(dag.critical_path(tasks), (["c"], 4))
        self.assertEqual(dag.critical_path([task("y", duration=1), task("x", duration=1)]), (["x"], 1))

    def test_missing_duration_counts_as_zero(self):
        tasks = [task("a"), task("b", ["a"], duration=2), task("c", ["b"], duration=1)]
        self.assertEqual(dag.critical_path(tasks), (["a", "b", "c"], 3))
        # A zero-length tail ties with b, and the smaller id b ends the path.
        tasks[2] = task("c", ["b"])
        self.assertEqual(dag.critical_path(tasks), (["a", "b"], 2))
        self.assertEqual(dag.critical_path([]), ([], 0))

    def test_cycle_raises(self):
        with self.assertRaises(dag.CycleError) as ctx:
            dag.critical_path([task("a", ["b"]), task("b", ["a"])])
        self.assertEqual(ctx.exception.cycle, ["a", "b", "a"])

    def test_bad_duration_raises(self):
        with self.assertRaises(ValueError):
            dag.critical_path([task("a", duration=-1)])


class DispatchTests(unittest.TestCase):
    def test_shared_file_not_handed_out_together(self):
        tasks = [
            task("a", files=["notiontool/engine.py"]),
            task("b", files=["NotionTool\\Engine.py"]),
            task("c", files=["docs/x.md"]),
        ]
        self.assertEqual(dag.dispatchable(tasks), ["a", "c"])
        self.assertEqual(dag.shared_files(tasks[0], tasks[1]), ["notiontool/engine.py"])

    def test_running_task_holds_file(self):
        tasks = [
            task("r", state="running", files=["./notiontool//dag.py"]),
            task("a", files=["notiontool\\dag.py"]),
            task("b", files=["tests/test_dag.py"]),
        ]
        self.assertEqual(dag.dispatchable(tasks), ["b"])

    def test_self_duplicate_paths_do_not_conflict(self):
        item = task("a", files=["notiontool/dag.py", "notiontool\\dag.py", "tests/test_dag.py", "tests\\test_dag.py"])
        self.assertEqual(dag.task_files(item), ["notiontool/dag.py", "tests/test_dag.py"])
        self.assertEqual(dag.dispatchable([item]), ["a"])

    def test_order_and_limit(self):
        tasks = [task("a"), task("b"), task("c", files=["f.py"]), task("d", files=["F.py"]), task("z", state="done")]
        self.assertEqual(dag.dispatchable(tasks, order=["d", "z", "b"]), ["d", "b", "a"])
        self.assertEqual(dag.dispatchable(tasks, limit=2), ["a", "b"])

    def test_inputs_unchanged(self):
        tasks = [
            task("a", state="done", files=["a\\x.py"], duration=1),
            task("b", ["a"], files=["a/x.py", "A\\X.py"], duration=2),
            task("c", ["a", "ghost"]),
        ]
        before = copy.deepcopy(tasks)
        dag.find_cycle(tasks)
        dag.missing_dependencies(tasks)
        dag.ready_set(tasks)
        dag.critical_path(tasks)
        dag.dispatchable(tasks, order=["b"])
        dag.task_files(tasks[1])
        self.assertEqual(tasks, before)


if __name__ == "__main__":
    unittest.main()
