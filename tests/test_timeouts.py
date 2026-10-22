import unittest

from notiontool.timeouts import task_limit


class TaskLimitTests(unittest.TestCase):
    def test_scales_with_effort(self):
        s = {'task_timeout': 900}
        self.assertEqual(task_limit(s, {'effort': 'max'}), 2700)
        self.assertEqual(task_limit(s, {'effort': 'xhigh'}), 1800)
        self.assertEqual(task_limit(s, {'effort': 'high'}), 1170)
        self.assertEqual(task_limit(s, {'effort': 'low'}), 900)

    def test_live_max_turns_fit(self):
        # Measured live: max turns took 819 s and 1157 s; both must stay inside the limit.
        self.assertGreater(task_limit({'task_timeout': 900}, {'effort': 'max'}), 1157 + 120)

    def test_defaults(self):
        self.assertEqual(task_limit({}, None), 900)
        self.assertEqual(task_limit({'task_timeout': 600}, {'effort': 'MAX '}), 1800)


if __name__ == '__main__':
    unittest.main()
