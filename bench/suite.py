"""Benchmark suite: six fixed tasks with fixed start states and objective checks.

Each task writes fixed LF-only literal files into a given folder (setup) and
judges the folder afterwards (check). Checks only read files inside the folder.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

CheckResult = Tuple[bool, str]


def _write_files(folder, files: Dict[str, str]) -> None:
    root = Path(folder)
    for rel in sorted(files):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(files[rel])


def _read_text(folder, rel: str) -> Optional[str]:
    path = Path(folder) / rel
    if not path.is_file():
        return None
    return path.read_bytes().decode("utf-8")


def _unchanged(folder, files: Dict[str, str]) -> List[str]:
    problems = []
    for rel in sorted(files):
        path = Path(folder) / rel
        if not path.is_file():
            problems.append(f"start file {rel} is missing")
        elif path.read_bytes() != files[rel].encode("utf-8"):
            problems.append(f"start file {rel} was changed")
    return problems


def _expect_exact(folder, rel: str, expected: str) -> List[str]:
    text = _read_text(folder, rel)
    if text is None:
        return [f"{rel} is missing"]
    if text != expected:
        return [f"{rel} content is {text!r}, expected {expected!r}"]
    return []


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True, eq=False)
class BenchTask:
    id: str
    title: str
    prompt: str
    files: Dict[str, str]
    checker: Callable[[str], List[str]]

    def setup(self, folder) -> None:
        """Write the fixed start files into folder."""
        _write_files(folder, self.files)

    def check(self, folder) -> CheckResult:
        """Return (passed, details) for the folder after a run."""
        try:
            problems = list(self.checker(folder))
        except Exception as exc:  # a broken result must fail, not crash the bench
            problems = [f"check raised {type(exc).__name__}: {exc}"]
        if problems:
            return False, "; ".join(problems)
        return True, "all checks passed"


# 1. routine ---------------------------------------------------------------

ROUTINE_FILES = {"names.txt": "Charlie\nalice\nBob\ndave\n"}


def _check_routine(folder) -> List[str]:
    problems = _expect_exact(folder, "sorted_names.txt", "alice\nBob\nCharlie\ndave\n")
    return problems + _unchanged(folder, ROUTINE_FILES)


# 2. research / file ---------------------------------------------------------

RESEARCH_FILES = {
    "data/sales.csv": (
        "region,quarter,amount\n"
        "North,Q1,120\n"
        "South,Q1,95\n"
        "East,Q1,140\n"
        "North,Q2,130\n"
        "South,Q2,160\n"
        "East,Q2,80\n"
    ),
    "data/README.md": "Amounts are in thousand EUR. One row per region and quarter.\n",
}


def _check_research(folder) -> List[str]:
    text = _read_text(folder, "answer.json")
    if text is None:
        return ["answer.json is missing"]
    try:
        data = json.loads(text)
    except ValueError as exc:
        return [f"answer.json is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return ["answer.json must contain a JSON object"]
    problems = []
    if data.get("top_region") != "South":
        problems.append(f"top_region is {data.get('top_region')!r}, expected 'South'")
    total = data.get("top_region_total")
    if not (_is_number(total) and total == 255):
        problems.append(f"top_region_total is {total!r}, expected 255")
    grand = data.get("grand_total")
    if not (_is_number(grand) and grand == 725):
        problems.append(f"grand_total is {grand!r}, expected 725")
    return problems + _unchanged(folder, RESEARCH_FILES)


# 3. programming -------------------------------------------------------------

PROGRAMMING_FILES = {
    "calc.py": (
        "def add(a, b):\n"
        "    return a - b\n"
        "\n"
        "\n"
        "def mean(values):\n"
        "    return sum(values) / len(values)\n"
    ),
    "test_calc.py": (
        "import unittest\n"
        "\n"
        "from calc import add, mean\n"
        "\n"
        "\n"
        "class CalcTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n"
        "\n"
        "    def test_mean(self):\n"
        "        self.assertEqual(mean([1, 2, 3]), 2)\n"
        "\n"
        "    def test_mean_empty(self):\n"
        "        self.assertEqual(mean([]), 0.0)\n"
        "\n"
        "\n"
        "if __name__ == \"__main__\":\n"
        "    unittest.main()\n"
    ),
}


def _check_programming(folder) -> List[str]:
    problems = _unchanged(folder, {"test_calc.py": PROGRAMMING_FILES["test_calc.py"]})
    source = _read_text(folder, "calc.py")
    if source is None:
        return problems + ["calc.py is missing"]
    namespace: Dict[str, object] = {}
    try:
        exec(compile(source, "calc.py", "exec"), namespace)
    except Exception as exc:
        return problems + [f"calc.py does not load: {type(exc).__name__}: {exc}"]
    cases = [
        ("add", (2, 3), 5),
        ("add", (-4, 4), 0),
        ("mean", ([1, 2, 3],), 2),
        ("mean", ([],), 0.0),
    ]
    for name, args, expected in cases:
        func = namespace.get(name)
        if not callable(func):
            problems.append(f"calc.py has no function {name}")
            continue
        try:
            got = func(*args)
        except Exception as exc:
            problems.append(f"{name}{args!r} raised {type(exc).__name__}")
            continue
        if got != expected:
            problems.append(f"{name}{args!r} returned {got!r}, expected {expected!r}")
    return problems


# 4. multistep ---------------------------------------------------------------

MULTISTEP_FILES = {
    "settings.txt": "name=notiontool-demo\nversion=1.2.3\n",
    "CHANGELOG.md": "# Changelog\n\n## 1.2.3\n- Initial release\n",
}


def _check_multistep(folder) -> List[str]:
    problems = _expect_exact(folder, "settings.txt", "name=notiontool-demo\nversion=1.2.4\n")
    problems += _expect_exact(
        folder,
        "CHANGELOG.md",
        "# Changelog\n\n## 1.2.4\n- Fix version bump\n\n## 1.2.3\n- Initial release\n",
    )
    release = _read_text(folder, "release.txt")
    if release is None:
        problems.append("release.txt is missing")
    elif release.strip() != "1.2.4":
        problems.append(f"release.txt content is {release!r}, expected '1.2.4'")
    return problems


# 5. dependent workers -------------------------------------------------------

DEPENDENT_FILES = {"parts/a.txt": "alpha\n", "parts/b.txt": "beta\n"}


def _check_dependent(folder) -> List[str]:
    problems = _expect_exact(folder, "upper.txt", "ALPHA\nBETA\n")
    text = _read_text(folder, "summary.json")
    if text is None:
        problems.append("summary.json is missing")
    else:
        try:
            data = json.loads(text)
        except ValueError as exc:
            data = None
            problems.append(f"summary.json is not valid JSON: {exc}")
        if data is not None:
            if not isinstance(data, dict):
                problems.append("summary.json must contain a JSON object")
            else:
                if data.get("lines") != 2 or isinstance(data.get("lines"), bool):
                    problems.append(f"summary lines is {data.get('lines')!r}, expected 2")
                upper_path = Path(folder) / "upper.txt"
                if upper_path.is_file():
                    digest = hashlib.sha256(upper_path.read_bytes()).hexdigest()
                    if str(data.get("sha256", "")).lower() != digest:
                        problems.append("summary sha256 does not match upper.txt")
                else:
                    problems.append("summary sha256 cannot be checked without upper.txt")
    return problems + _unchanged(folder, DEPENDENT_FILES)


# 6. failure case ------------------------------------------------------------

FAILURE_FILES = {
    "request.md": (
        "Convert every price in input/prices.csv from USD to EUR using the rate "
        "in input/rates.json and write output/prices_eur.csv.\n"
    ),
    "input/prices.csv": "item,usd\nlamp,40\ndesk,250\n",
}

FAILURE_MISSING_INPUT = "rates.json"


def _check_failure(folder) -> List[str]:
    problems = []
    blocked = _read_text(folder, "BLOCKED.md")
    if blocked is None:
        problems.append("BLOCKED.md is missing")
    elif FAILURE_MISSING_INPUT not in blocked:
        problems.append(f"BLOCKED.md does not name the missing input {FAILURE_MISSING_INPUT}")
    if (Path(folder) / "input" / "rates.json").exists():
        problems.append("input/rates.json was invented instead of reporting it missing")
    if (Path(folder) / "output" / "prices_eur.csv").exists():
        problems.append("output/prices_eur.csv was written although the input is missing")
    return problems + _unchanged(folder, FAILURE_FILES)


SUITE: List[BenchTask] = [
    BenchTask(
        id="routine",
        title="Sort a name list",
        prompt=(
            "Read names.txt and write sorted_names.txt with the same names in "
            "case-insensitive alphabetical order, one name per line, keeping the "
            "original spelling, LF line endings and a final newline. Do not change names.txt."
        ),
        files=ROUTINE_FILES,
        checker=_check_routine,
    ),
    BenchTask(
        id="research_file",
        title="Answer a question from a CSV file",
        prompt=(
            "Read data/sales.csv and data/README.md. Find the region with the highest "
            "total amount over all quarters. Write answer.json as a JSON object with the "
            "keys top_region (string), top_region_total (integer) and grand_total "
            "(integer, sum of all rows). Do not change the files in data/."
        ),
        files=RESEARCH_FILES,
        checker=_check_research,
    ),
    BenchTask(
        id="programming",
        title="Fix two bugs in calc.py",
        prompt=(
            "calc.py has two bugs. add(a, b) must return the sum, and mean(values) "
            "must return 0.0 for an empty list instead of raising. Fix calc.py so that "
            "python -m unittest test_calc passes. Do not change test_calc.py."
        ),
        files=PROGRAMMING_FILES,
        checker=_check_programming,
    ),
    BenchTask(
        id="multistep",
        title="Release a patch version in three steps",
        prompt=(
            "Do these three steps in order. 1: In settings.txt change version=1.2.3 to "
            "version=1.2.4 and change nothing else. 2: In CHANGELOG.md add a section "
            "'## 1.2.4' with the single line '- Fix version bump' directly above the "
            "'## 1.2.3' section, separated by one empty line. 3: Write release.txt "
            "containing only 1.2.4."
        ),
        files=MULTISTEP_FILES,
        checker=_check_multistep,
    ),
    BenchTask(
        id="dependent_workers",
        title="Two workers where the second depends on the first",
        prompt=(
            "Use two workers. Worker 1 reads parts/a.txt and parts/b.txt and writes "
            "upper.txt with the upper-cased line of a.txt followed by the upper-cased "
            "line of b.txt, each ending with LF. Worker 2 starts only after worker 1 is "
            "done and writes summary.json as a JSON object with lines (the number of "
            "lines in upper.txt) and sha256 (the lowercase hex sha256 of the bytes of "
            "upper.txt). Do not change the files in parts/."
        ),
        files=DEPENDENT_FILES,
        checker=_check_dependent,
    ),
    BenchTask(
        id="failure_case",
        title="Report a missing input instead of guessing",
        prompt=(
            "Carry out the request in request.md. If a required input file is missing, "
            "do not guess, do not create the missing file or any output, and do not "
            "change the existing files. Instead write BLOCKED.md that names the missing "
            "input file."
        ),
        files=FAILURE_FILES,
        checker=_check_failure,
    ),
]

TASK_IDS: List[str] = [task.id for task in SUITE]


def get_task(task_id: str) -> BenchTask:
    """Return the task with this id or raise KeyError."""
    for task in SUITE:
        if task.id == task_id:
            return task
    raise KeyError(f"unknown bench task: {task_id}")
