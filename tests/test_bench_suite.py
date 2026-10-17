import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from bench import suite

EXPECTED_IDS = [
    "routine",
    "research_file",
    "programming",
    "multistep",
    "dependent_workers",
    "failure_case",
]


def _write(folder, rel, text):
    path = Path(folder) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _digests(folder):
    root = Path(folder)
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# Hand-written reference solutions, one per task.

def solve_routine(folder):
    _write(folder, "sorted_names.txt", "alice\nBob\nCharlie\ndave\n")


def solve_research_file(folder):
    _write(
        folder,
        "answer.json",
        '{"top_region": "South", "top_region_total": 255, "grand_total": 725}\n',
    )


def solve_programming(folder):
    _write(
        folder,
        "calc.py",
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def mean(values):\n"
        "    if not values:\n"
        "        return 0.0\n"
        "    return sum(values) / len(values)\n",
    )


def solve_multistep(folder):
    _write(folder, "settings.txt", "name=notiontool-demo\nversion=1.2.4\n")
    _write(
        folder,
        "CHANGELOG.md",
        "# Changelog\n\n## 1.2.4\n- Fix version bump\n\n## 1.2.3\n- Initial release\n",
    )
    _write(folder, "release.txt", "1.2.4\n")


def solve_dependent_workers(folder):
    upper = "ALPHA\nBETA\n"
    _write(folder, "upper.txt", upper)
    digest = hashlib.sha256(upper.encode("utf-8")).hexdigest()
    _write(folder, "summary.json", json.dumps({"lines": 2, "sha256": digest}) + "\n")


def solve_failure_case(folder):
    _write(folder, "BLOCKED.md", "Blocked: the required input input/rates.json is missing.\n")


REFERENCE = {
    "routine": solve_routine,
    "research_file": solve_research_file,
    "programming": solve_programming,
    "multistep": solve_multistep,
    "dependent_workers": solve_dependent_workers,
    "failure_case": solve_failure_case,
}


class BenchSuiteTest(unittest.TestCase):
    def test_exactly_six_unique_ids_in_order(self):
        ids = [task.id for task in suite.SUITE]
        self.assertEqual(ids, EXPECTED_IDS)
        self.assertEqual(len(set(ids)), 6)
        self.assertEqual(suite.TASK_IDS, EXPECTED_IDS)

    def test_get_task(self):
        for task in suite.SUITE:
            self.assertIs(suite.get_task(task.id), task)
        with self.assertRaises(KeyError):
            suite.get_task("missing")

    def test_prompts_are_english_text(self):
        for task in suite.SUITE:
            with self.subTest(task=task.id):
                self.assertIsInstance(task.prompt, str)
                self.assertTrue(task.prompt.strip())
                self.assertTrue(task.prompt.isascii())

    def test_setup_is_deterministic(self):
        for task in suite.SUITE:
            with self.subTest(task=task.id):
                with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
                    task.setup(one)
                    task.setup(two)
                    first = _digests(one)
                    self.assertTrue(first)
                    self.assertEqual(first, _digests(two))
                    expected = {
                        rel: hashlib.sha256(text.encode("utf-8")).hexdigest()
                        for rel, text in task.files.items()
                    }
                    self.assertEqual(first, expected)

    def test_start_files_are_lf_without_bom(self):
        for task in suite.SUITE:
            with self.subTest(task=task.id):
                with tempfile.TemporaryDirectory() as folder:
                    task.setup(folder)
                    for path in Path(folder).rglob("*"):
                        if path.is_file():
                            data = path.read_bytes()
                            self.assertNotIn(b"\r", data, path.name)
                            self.assertFalse(data.startswith(b"\xef\xbb\xbf"), path.name)

    def test_check_fails_on_untouched_start_state(self):
        for task in suite.SUITE:
            with self.subTest(task=task.id):
                with tempfile.TemporaryDirectory() as folder:
                    task.setup(folder)
                    passed, details = task.check(folder)
                    self.assertFalse(passed)
                    self.assertTrue(details.strip())

    def test_check_fails_on_empty_folder(self):
        for task in suite.SUITE:
            with self.subTest(task=task.id):
                with tempfile.TemporaryDirectory() as folder:
                    passed, details = task.check(folder)
                    self.assertFalse(passed)
                    self.assertTrue(details.strip())

    def test_check_passes_on_reference_solution(self):
        self.assertEqual(sorted(REFERENCE), sorted(EXPECTED_IDS))
        for task in suite.SUITE:
            with self.subTest(task=task.id):
                with tempfile.TemporaryDirectory() as folder:
                    task.setup(folder)
                    REFERENCE[task.id](folder)
                    passed, details = task.check(folder)
                    self.assertTrue(passed, details)

    def test_failure_case_fails_if_start_file_changed(self):
        task = suite.get_task("failure_case")
        with tempfile.TemporaryDirectory() as folder:
            task.setup(folder)
            solve_failure_case(folder)
            _write(folder, "input/prices.csv", "item,usd\nlamp,40\ndesk,251\n")
            passed, details = task.check(folder)
            self.assertFalse(passed)
            self.assertIn("input/prices.csv was changed", details)

    def test_failure_case_fails_if_missing_input_is_invented(self):
        task = suite.get_task("failure_case")
        with tempfile.TemporaryDirectory() as folder:
            task.setup(folder)
            solve_failure_case(folder)
            _write(folder, "input/rates.json", '{"EUR": 0.9}\n')
            passed, details = task.check(folder)
            self.assertFalse(passed)
            self.assertIn("rates.json", details)

    def test_failure_case_needs_the_missing_input_named(self):
        task = suite.get_task("failure_case")
        with tempfile.TemporaryDirectory() as folder:
            task.setup(folder)
            _write(folder, "BLOCKED.md", "Blocked.\n")
            passed, details = task.check(folder)
            self.assertFalse(passed)
            self.assertIn("does not name", details)

    def test_programming_fails_if_test_file_changed(self):
        task = suite.get_task("programming")
        with tempfile.TemporaryDirectory() as folder:
            task.setup(folder)
            solve_programming(folder)
            _write(folder, "test_calc.py", "")
            passed, details = task.check(folder)
            self.assertFalse(passed)
            self.assertIn("test_calc.py was changed", details)


if __name__ == "__main__":
    unittest.main()
