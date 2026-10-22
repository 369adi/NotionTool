import unittest

from notiontool import operator_main as om


class AuditLineTests(unittest.TestCase):
    def test_read_only_audit_gets_recount_rule_before_result_line(self):
        out = om._with_result_line('Read-only audit: count the open runs.')
        self.assertIn(om.AUDIT_LINE, out)
        self.assertTrue(out.endswith(om.RESULT_LINE))
        self.assertLess(out.index(om.AUDIT_LINE), out.index(om.RESULT_LINE))

    def test_normal_task_has_no_audit_rule(self):
        out = om._with_result_line('Change BRIEF_MAX_CHARS to 9000.')
        self.assertNotIn(om.AUDIT_LINE, out)
        self.assertTrue(out.endswith(om.RESULT_LINE))

    def test_rule_is_added_only_once(self):
        once = om._with_result_line('Audit the queue.')
        stripped = once[:-len(om.RESULT_LINE)].rstrip()
        twice = om._with_result_line(stripped)
        self.assertEqual(twice.count(om.AUDIT_LINE), 1)


if __name__ == '__main__':
    unittest.main()
