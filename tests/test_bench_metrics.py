"""Tests for bench/metrics.py using synthetic data in temp files only."""

import json
import os
import tempfile
import unittest

from bench import metrics


def run(task_id, variant, run_index, started, accepted_at, accepted,
        corrections=0, model_calls=1):
    return {
        "task_id": task_id,
        "variant": variant,
        "run_index": run_index,
        "started": started,
        "accepted_at": accepted_at,
        "accepted": accepted,
        "corrections": corrections,
        "model_calls": model_calls,
    }


class JsonlCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def write_lines(self, lines, name="raw.jsonl"):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(line + "\n")
        return path

    def write_rows(self, rows, name="raw.jsonl"):
        return self.write_lines([json.dumps(r) for r in rows], name)


class LoadJsonlTests(JsonlCase):
    def test_blank_lines_are_ignored(self):
        path = self.write_lines([
            json.dumps(run("t1", "B0", 0, 0, 10, True)),
            "",
            "   ",
            json.dumps(run("t1", "final", 0, 0, 5, True)),
        ])
        rows = metrics.load_jsonl(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["variant"] for r in rows], ["B0", "final"])

    def test_bad_line_raises_value_error_with_line_number(self):
        path = self.write_lines([
            json.dumps(run("t1", "B0", 0, 0, 10, True)),
            "",
            "{not json",
        ])
        with self.assertRaises(ValueError) as ctx:
            metrics.load_jsonl(path)
        self.assertIn("line 3", str(ctx.exception))

    def test_missing_field_raises_value_error_with_line_number(self):
        row = run("t1", "B0", 0, 0, 10, True)
        del row["model_calls"]
        path = self.write_rows([run("t1", "B0", 1, 0, 10, True), row])
        with self.assertRaises(ValueError) as ctx:
            metrics.load_jsonl(path)
        self.assertIn("line 2", str(ctx.exception))
        self.assertIn("model_calls", str(ctx.exception))

    def test_unknown_variant_raises_value_error(self):
        path = self.write_rows([run("t1", "B1", 0, 0, 10, True)])
        with self.assertRaises(ValueError) as ctx:
            metrics.load_jsonl(path)
        self.assertIn("line 1", str(ctx.exception))


class SummarizeTests(JsonlCase):
    def test_aggregation_two_tasks_two_variants(self):
        rows = [
            run("t1", "B0", 0, 100, 160, True, 2, 5),
            run("t1", "B0", 1, 200, 280, True, 3, 6),
            run("t1", "B0", 2, 300, 370, True, 4, 7),
            run("t1", "final", 0, 100, 130, True, 0, 3),
            run("t1", "final", 1, 200, 240, True, 1, 4),
            run("t1", "final", 2, 300, 335, True, 0, 3),
            run("t2", "B0", 0, 0, 50, True, 1, 2),
            run("t2", "B0", 1, 0, 70, True, 1, 2),
            run("t2", "B0", 2, 0, 60, True, 1, 2),
            run("t2", "final", 0, 0, 20, True, 0, 1),
            run("t2", "final", 1, 0, 40, True, 0, 1),
            run("t2", "final", 2, 0, 30, True, 0, 1),
        ]
        summary = metrics.summarize(metrics.load_jsonl(self.write_rows(rows)))
        self.assertEqual(
            sorted(summary),
            [("t1", "B0"), ("t1", "final"), ("t2", "B0"), ("t2", "final")],
        )
        self.assertEqual(summary[("t1", "B0")], {
            "runs": 3,
            "acceptance_rate": 1.0,
            "median_time_to_acceptance": 70,
            "median_corrections": 3,
            "median_model_calls": 6,
        })
        self.assertEqual(summary[("t1", "final")]["median_time_to_acceptance"], 35)
        self.assertEqual(summary[("t1", "final")]["median_corrections"], 0)
        self.assertEqual(summary[("t1", "final")]["median_model_calls"], 3)
        self.assertEqual(summary[("t2", "B0")]["median_time_to_acceptance"], 60)
        self.assertEqual(summary[("t2", "final")]["median_time_to_acceptance"], 30)
        self.assertEqual(summary[("t2", "final")]["runs"], 3)

    def test_acceptance_rate_with_mixed_results(self):
        rows = [
            run("t1", "B0", 0, 0, 10, True),
            run("t1", "B0", 1, 0, None, False),
            run("t1", "B0", 2, 0, None, False),
            run("t1", "B0", 3, 0, 20, True),
        ]
        summary = metrics.summarize(rows)
        self.assertEqual(summary[("t1", "B0")]["runs"], 4)
        self.assertAlmostEqual(summary[("t1", "B0")]["acceptance_rate"], 0.5)

    def test_time_median_uses_accepted_runs_only(self):
        rows = [
            run("t1", "final", 0, 0, 10, True, 0, 1),
            run("t1", "final", 1, 0, 30, True, 0, 1),
            run("t1", "final", 2, 0, 9999, False, 5, 9),
        ]
        summary = metrics.summarize(rows)[("t1", "final")]
        self.assertEqual(summary["median_time_to_acceptance"], 20)
        self.assertEqual(summary["median_corrections"], 0)
        self.assertEqual(summary["median_model_calls"], 1)

    def test_time_is_none_when_nothing_accepted(self):
        rows = [
            run("t1", "B0", 0, 0, None, False, 2, 3),
            run("t1", "B0", 1, 0, None, False, 4, 5),
        ]
        summary = metrics.summarize(rows)[("t1", "B0")]
        self.assertIsNone(summary["median_time_to_acceptance"])
        self.assertEqual(summary["acceptance_rate"], 0.0)
        self.assertEqual(summary["median_corrections"], 3)
        self.assertEqual(summary["median_model_calls"], 4)


class ClaimsSuperiorityTests(unittest.TestCase):
    def test_well_separated_values_return_true(self):
        self.assertTrue(metrics.claims_superiority([10, 11, 12], [30, 31, 32]))

    def test_overlapping_spread_returns_false(self):
        # Medians 20 vs 25 (difference 5), but spreads are 20 and 20.
        a = [10, 20, 30]
        b = [15, 25, 35]
        self.assertLess(sorted(a)[1], sorted(b)[1])
        self.assertFalse(metrics.claims_superiority(a, b))

    def test_difference_equal_to_spread_returns_false(self):
        # Difference 10 equals spread 10 of a: not strictly greater.
        self.assertFalse(metrics.claims_superiority([0, 5, 10], [15, 15, 15]))

    def test_equal_medians_return_false(self):
        self.assertFalse(metrics.claims_superiority([5, 5, 5], [5, 5, 5]))
        self.assertFalse(metrics.claims_superiority([4, 5, 6], [5, 5, 5]))

    def test_empty_list_returns_false(self):
        self.assertFalse(metrics.claims_superiority([], [1, 2, 3]))
        self.assertFalse(metrics.claims_superiority([1, 2, 3], []))
        self.assertFalse(metrics.claims_superiority([], []))

    def test_worse_a_returns_false(self):
        self.assertFalse(metrics.claims_superiority([30, 31, 32], [10, 11, 12]))

    def test_higher_is_better_direction(self):
        high = [0.9, 0.95, 1.0]
        low = [0.1, 0.15, 0.2]
        self.assertTrue(metrics.claims_superiority(high, low, lower_is_better=False))
        self.assertFalse(metrics.claims_superiority(low, high, lower_is_better=False))
        self.assertFalse(metrics.claims_superiority(high, low))


if __name__ == "__main__":
    unittest.main()
