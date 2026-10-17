import unittest

from notiontool import perfection as p


POLICY_RANK = {name: i for i, name in enumerate(p.MODEL_REVIEW_POLICIES)}


class LevelTableTests(unittest.TestCase):
    def test_values_increase_strictly(self):
        levels = [p.LEVELS[name] for name in p.LEVEL_ORDER]
        for low, high in zip(levels, levels[1:]):
            self.assertLess(len(low["verifier_depth"]), len(high["verifier_depth"]))
            self.assertLess(low["max_fix_rounds"], high["max_fix_rounds"])
            self.assertLess(POLICY_RANK[low["model_review"]], POLICY_RANK[high["model_review"]])
            self.assertLess(low["request_budget"], high["request_budget"])
            self.assertLess(low["time_budget_s"], high["time_budget_s"])

    def test_depth_is_cascade_prefix(self):
        for name in p.LEVEL_ORDER:
            depth = p.LEVELS[name]["verifier_depth"]
            self.assertEqual(tuple(depth), p.STAGES[:len(depth)])
        self.assertEqual(p.LEVELS["perfection"]["verifier_depth"], p.STAGES)

    def test_policies_per_level(self):
        self.assertEqual(p.get_level("standard")["model_review"], "never")
        self.assertEqual(p.get_level("high")["model_review"], "high_risk")
        self.assertEqual(p.get_level("perfection")["model_review"], "always")


class GetLevelTests(unittest.TestCase):
    def test_known_names_case_insensitive(self):
        self.assertEqual(p.get_level(" HIGH ")["name"], "high")
        self.assertEqual(p.get_level("perfection")["max_fix_rounds"], 4)

    def test_unknown_level_falls_back_to_standard(self):
        for value in ("ultra", "", None, 3, ["high"]):
            self.assertEqual(p.get_level(value)["name"], "standard")

    def test_returns_copy(self):
        level = p.get_level("high")
        level["max_fix_rounds"] = 99
        self.assertEqual(p.LEVELS["high"]["max_fix_rounds"], 2)


class EscalateTests(unittest.TestCase):
    def test_escalates_after_failure_with_reason(self):
        level, text = p.escalate_level("standard", True, "2 tests failed")
        self.assertEqual(level["name"], "high")
        self.assertIn("standard to high", text)
        self.assertIn("2 tests failed", text)
        level, text = p.escalate_level(level, True, "diff mismatch")
        self.assertEqual(level["name"], "perfection")

    def test_no_failure_stays(self):
        for failed in (False, None, 1, "yes"):
            level, text = p.escalate_level("high", failed, "reason")
            self.assertEqual(level["name"], "high")
            self.assertIn("no failure", text)

    def test_missing_reason_stays(self):
        for reason in ("", "   ", None, 5):
            level, text = p.escalate_level("standard", True, reason)
            self.assertEqual(level["name"], "standard")
            self.assertIn("missing reason", text)

    def test_top_level_stays(self):
        level, text = p.escalate_level("perfection", True, "still failing")
        self.assertEqual(level["name"], "perfection")
        self.assertIn("already at highest level", text)

    def test_unknown_current_treated_as_standard(self):
        level, _ = p.escalate_level("bogus", True, "failed")
        self.assertEqual(level["name"], "high")


class GuardTests(unittest.TestCase):
    def change(self, **kw):
        base = {"kind": "edit", "path": "notiontool/x.py", "generated": False,
                "file_count": 1, "backup": False, "dry_run": False, "approved": False}
        base.update(kw)
        return base

    def test_small_edit_allowed(self):
        allowed, reason = p.guard_change(self.change())
        self.assertTrue(allowed)
        self.assertIn("not destructive", reason)

    def test_delete_without_backup_denied(self):
        allowed, reason = p.guard_change(self.change(kind="delete", approved=True))
        self.assertFalse(allowed)
        self.assertIn("backup or dry run", reason)

    def test_delete_with_backup_without_approval_denied(self):
        allowed, reason = p.guard_change(self.change(kind="delete", backup=True))
        self.assertFalse(allowed)
        self.assertIn("explicit approval", reason)

    def test_delete_with_dry_run_and_approval_allowed(self):
        allowed, reason = p.guard_change(self.change(kind="delete", dry_run=True, approved=True))
        self.assertTrue(allowed)
        self.assertIn("deletion", reason)

    def test_overwrite_non_generated_denied_generated_allowed(self):
        allowed, reason = p.guard_change(self.change(kind="overwrite"))
        self.assertFalse(allowed)
        self.assertIn("non-generated", reason)
        allowed, _ = p.guard_change(self.change(kind="overwrite", generated=True))
        self.assertTrue(allowed)
        missing = self.change(kind="overwrite")
        del missing["generated"]
        self.assertFalse(p.guard_change(missing)[0])

    def test_mass_edit_threshold(self):
        limit = p.MASS_EDIT_THRESHOLD
        self.assertTrue(p.guard_change(self.change(file_count=limit))[0])
        allowed, reason = p.guard_change(self.change(file_count=limit + 1))
        self.assertFalse(allowed)
        self.assertIn("mass edit", reason)
        self.assertTrue(p.guard_change(self.change(file_count=limit + 1, backup=True, approved=True))[0])

    def test_truthy_non_bool_flags_do_not_count(self):
        allowed, _ = p.guard_change(self.change(kind="delete", backup="yes", approved=1))
        self.assertFalse(allowed)

    def test_invalid_input_denied(self):
        self.assertFalse(p.guard_change(None)[0])
        self.assertFalse(p.guard_change(self.change(kind="rename"))[0])
        self.assertFalse(p.guard_change(self.change(file_count=-1))[0])
        self.assertFalse(p.guard_change(self.change(file_count="3"))[0])


if __name__ == "__main__":
    unittest.main()
