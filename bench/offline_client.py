"""Offline benchmark client: MOCKED, scripted runs that never touch Notion.

run_bench.py talks to a client with send_b0(prompt), send_harness(prompt) and
wait_done(handle) (bench/run_bench.py:4-8, :106-108). This client implements
that interface without any model, chat or network:

1. find the suite task whose prompt was sent,
2. task.setup(folder) in a fresh temporary folder,
3. apply a fixed scripted solution for that task id, if one is configured
   (otherwise do nothing),
4. report accepted = task.check(folder)[0].

Honesty notes:
- bench/suite.py ships no solutions. The scripted solutions below are reference
  scripts kept in this file. ``client`` uses them; ``control_client`` uses none,
  so every task must fail its check (this proves check() is really judged).
- B0 and harness are the SAME scripted path here. The variant is only recorded.
  Accept rates, timings, corrections and model_calls from this client say
  nothing about the real B0 or the real harness. model_calls is always 0.

Usage:
    python -m bench.run_bench --account offline --runs 1 \
        --client bench.offline_client:client --out work/bench/offline.jsonl
"""

import hashlib
import itertools
import json
import shutil
import tempfile
from pathlib import Path

MOCKED = True


def _write(folder, rel, text):
    path = Path(folder) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _read(folder, rel):
    return (Path(folder) / rel).read_bytes().decode("utf-8")


def solve_routine(folder):
    names = _read(folder, "names.txt").splitlines()
    _write(folder, "sorted_names.txt", "".join(n + "\n" for n in sorted(names, key=str.lower)))


def solve_research_file(folder):
    lines = _read(folder, "data/sales.csv").splitlines()[1:]
    totals = {}
    for line in lines:
        region, _quarter, amount = line.split(",")
        totals[region] = totals.get(region, 0) + int(amount)
    top = max(totals, key=totals.get)
    answer = {"top_region": top, "top_region_total": totals[top], "grand_total": sum(totals.values())}
    _write(folder, "answer.json", json.dumps(answer) + "\n")


def solve_programming(folder):
    _write(folder, "calc.py", (
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def mean(values):\n"
        "    if not values:\n"
        "        return 0.0\n"
        "    return sum(values) / len(values)\n"
    ))


def solve_multistep(folder):
    settings = _read(folder, "settings.txt").replace("version=1.2.3\n", "version=1.2.4\n")
    _write(folder, "settings.txt", settings)
    changelog = _read(folder, "CHANGELOG.md").replace(
        "## 1.2.3\n", "## 1.2.4\n- Fix version bump\n\n## 1.2.3\n", 1)
    _write(folder, "CHANGELOG.md", changelog)
    _write(folder, "release.txt", "1.2.4\n")


def solve_dependent_workers(folder):
    upper = "".join(_read(folder, rel).strip().upper() + "\n" for rel in ("parts/a.txt", "parts/b.txt"))
    _write(folder, "upper.txt", upper)  # "worker 1"
    data = upper.encode("utf-8")       # "worker 2" runs after worker 1 is done
    summary = {"lines": len(upper.splitlines()), "sha256": hashlib.sha256(data).hexdigest()}
    _write(folder, "summary.json", json.dumps(summary) + "\n")


def solve_failure_case(folder):
    if not (Path(folder) / "input" / "rates.json").exists():
        _write(folder, "BLOCKED.md", "Blocked: the required input input/rates.json is missing.\n")


SCRIPTED_SOLUTIONS = {
    "routine": solve_routine,
    "research_file": solve_research_file,
    "programming": solve_programming,
    "multistep": solve_multistep,
    "dependent_workers": solve_dependent_workers,
    "failure_case": solve_failure_case,
}


class OfflineClient:
    """MOCKED client: setup() -> scripted solution (or nothing) -> check()."""

    def __init__(self, tasks=None, solutions=None, keep_dirs=False, base_dir=None):
        if tasks is None:
            from bench.suite import SUITE as tasks
        self.tasks = list(tasks)
        self.solutions = dict(SCRIPTED_SOLUTIONS if solutions is None else solutions)
        self.keep_dirs = keep_dirs
        self.base_dir = base_dir
        self.pending = {}
        self.log = []
        self._ids = itertools.count(1)

    def _task_for(self, prompt):
        for task in self.tasks:
            if task.prompt == prompt:
                return task
        raise KeyError("no suite task has this prompt")

    def _send(self, variant, prompt):
        handle = "%s-%d" % (variant, next(self._ids))
        self.pending[handle] = (variant, self._task_for(prompt))
        return handle

    def send_b0(self, prompt):
        return self._send("B0", prompt)

    def send_harness(self, prompt):
        return self._send("final", prompt)  # same scripted path as B0

    def wait_done(self, handle):
        variant, task = self.pending.pop(handle)
        folder = tempfile.mkdtemp(prefix="bench-%s-" % task.id, dir=self.base_dir)
        try:
            task.setup(folder)
            solution = self.solutions.get(task.id)
            if solution is not None:
                solution(folder)
            passed, details = task.check(folder)
        finally:
            if not self.keep_dirs:
                shutil.rmtree(folder, ignore_errors=True)
        result = {
            "accepted": bool(passed),
            "corrections": 0,
            "model_calls": 0,
            "details": details,
            "scripted": solution is not None,
            "mocked": True,
        }
        self.log.append(dict(result, task_id=task.id, variant=variant))
        return result


def client():
    """Factory for --client bench.offline_client:client (reference scripts)."""
    return OfflineClient()


def control_client():
    """Factory for --client bench.offline_client:control_client (no solutions)."""
    return OfflineClient(solutions={})
