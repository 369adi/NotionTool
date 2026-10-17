import unittest

from notiontool import effort_policy as ep


class EffortPolicyTests(unittest.TestCase):
    def test_starts_at_low(self):
        result = ep.initial()
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["settings"], ep.settings_for("low"))
        self.assertTrue(result["reason"])

    def test_all_four_levels_mapped(self):
        self.assertEqual(ep.LEVELS, ("low", "standard", "high", "max"))
        for level in ep.LEVELS:
            settings = ep.settings_for(level)
            self.assertEqual(set(settings), set(ep.SETTING_KEYS))
            for key in ep.SETTING_KEYS:
                self.assertIsInstance(settings[key], int)
                self.assertGreater(settings[key], 0)

    def test_values_never_go_down_between_levels(self):
        for lower, higher in zip(ep.LEVELS, ep.LEVELS[1:]):
            a = ep.settings_for(lower)
            b = ep.settings_for(higher)
            for key in ep.SETTING_KEYS:
                self.assertGreaterEqual(b[key], a[key], (lower, higher, key))

    def test_one_step_per_failure(self):
        level = ep.initial()["level"]
        seen = [level]
        for _ in range(3):
            result = ep.escalate(level, True, "tests failed")
            self.assertEqual(ep.level_index(result["level"]), ep.level_index(level) + 1)
            self.assertEqual(result["settings"], ep.settings_for(result["level"]))
            self.assertIn("tests failed", result["reason"])
            level = result["level"]
            seen.append(level)
        self.assertEqual(seen, list(ep.LEVELS))

    def test_no_change_without_failure(self):
        for level in ep.LEVELS:
            result = ep.escalate(level, False, "review passed")
            self.assertEqual(result["level"], level)
            self.assertEqual(result["settings"], ep.settings_for(level))
            self.assertIn("review passed", result["reason"])

    def test_missing_reason_is_rejected(self):
        for bad in (None, "", "   ", 0):
            with self.assertRaises(ep.EffortPolicyError):
                ep.escalate("low", True, bad)
            with self.assertRaises(ep.EffortPolicyError):
                ep.escalate("low", False, bad)

    def test_capped_at_max(self):
        result = ep.escalate("max", True, "still failing")
        self.assertEqual(result["level"], "max")
        self.assertEqual(result["settings"], ep.settings_for("max"))
        self.assertIn("max", result["reason"])
        self.assertIn("still failing", result["reason"])
        again = ep.escalate(result["level"], True, "failing again")
        self.assertEqual(again["level"], "max")

    def test_unknown_level_is_rejected(self):
        for bad in ("medium", "MAX", "", None, 3):
            with self.assertRaises(ep.EffortPolicyError):
                ep.escalate(bad, True, "reason")
            with self.assertRaises(ep.EffortPolicyError):
                ep.settings_for(bad)

    def test_every_result_has_level_settings_reason(self):
        results = [ep.initial(), ep.escalate("low", True, "x"), ep.escalate("high", False, "y"), ep.escalate("max", True, "z")]
        for result in results:
            self.assertEqual(set(result), {"level", "settings", "reason"})

    def test_settings_are_copies(self):
        result = ep.initial()
        result["settings"]["review_rounds"] = 99
        self.assertEqual(ep.settings_for("low")["review_rounds"], 1)


if __name__ == "__main__":
    unittest.main()
