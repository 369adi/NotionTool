"""Benchmark metrics: read raw run results and compare variants.

Raw results are JSONL, one object per run with these fields:
  task_id (str), variant ("B0" or "final"), run_index (int),
  started (epoch seconds), accepted_at (epoch seconds or null),
  accepted (bool), corrections (int), model_calls (int).

Stdlib only. No network, no environment access.
"""

import json
import statistics

VARIANTS = ("B0", "final")

REQUIRED_FIELDS = (
    "task_id",
    "variant",
    "run_index",
    "started",
    "accepted_at",
    "accepted",
    "corrections",
    "model_calls",
)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_row(row):
    """Return None if row is valid, else a short error message."""
    if not isinstance(row, dict):
        return "expected a JSON object"
    for field in REQUIRED_FIELDS:
        if field not in row:
            return "missing field %r" % field
    if not isinstance(row["task_id"], str) or not row["task_id"]:
        return "task_id must be a non-empty string"
    if row["variant"] not in VARIANTS:
        return "variant must be one of %s" % ", ".join(VARIANTS)
    if not _is_int(row["run_index"]):
        return "run_index must be an int"
    if not _is_number(row["started"]):
        return "started must be epoch seconds"
    if row["accepted_at"] is not None and not _is_number(row["accepted_at"]):
        return "accepted_at must be epoch seconds or null"
    if not isinstance(row["accepted"], bool):
        return "accepted must be a bool"
    if not _is_int(row["corrections"]):
        return "corrections must be an int"
    if not _is_int(row["model_calls"]):
        return "model_calls must be an int"
    return None


def load_jsonl(path):
    """Read one JSON object per line. Blank lines are ignored.

    A line that is not valid JSON or misses a required field raises
    ValueError naming the 1-based line number.
    """
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except ValueError as exc:
                raise ValueError("line %d: invalid JSON (%s)" % (number, exc)) from None
            error = validate_row(row)
            if error:
                raise ValueError("line %d: %s" % (number, error))
            rows.append(row)
    return rows


def _median_or_none(values):
    return statistics.median(values) if values else None


def summarize(rows):
    """Aggregate runs per (task_id, variant).

    Returns {(task_id, variant): {runs, acceptance_rate,
    median_time_to_acceptance, median_corrections, median_model_calls}}.
    Time to acceptance is accepted_at - started over accepted runs only;
    it is None when no run was accepted.
    """
    groups = {}
    for row in rows:
        groups.setdefault((row["task_id"], row["variant"]), []).append(row)
    summary = {}
    for key in sorted(groups):
        runs = groups[key]
        accepted = [r for r in runs if r["accepted"]]
        times = [
            r["accepted_at"] - r["started"]
            for r in accepted
            if r["accepted_at"] is not None
        ]
        summary[key] = {
            "runs": len(runs),
            "acceptance_rate": len(accepted) / len(runs),
            "median_time_to_acceptance": _median_or_none(times),
            "median_corrections": statistics.median(r["corrections"] for r in runs),
            "median_model_calls": statistics.median(r["model_calls"] for r in runs),
        }
    return summary


def claims_superiority(a, b, lower_is_better=True):
    """Return True only if a is clearly better than b.

    Both lists must be non-empty, a's median must be better than b's, and
    the absolute difference of medians must be strictly greater than the
    spread (max - min) of a and also of b. Otherwise False.
    """
    a = list(a)
    b = list(b)
    if not a or not b:
        return False
    median_a = statistics.median(a)
    median_b = statistics.median(b)
    if lower_is_better:
        better = median_a < median_b
    else:
        better = median_a > median_b
    if not better:
        return False
    difference = abs(median_a - median_b)
    spread_a = max(a) - min(a)
    spread_b = max(b) - min(b)
    return difference > spread_a and difference > spread_b
