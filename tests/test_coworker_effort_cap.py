"""Rule I47: no Coworker request runs on 'max'; the top level for Coworkers is 'xhigh'. Main chats keep 'max'."""
import unittest
from unittest import mock

from notiontool import effort_policy as ep
from notiontool import notion

ALL = ('low', 'medium', 'high', 'xhigh', 'max')
NO_XHIGH = ('low', 'medium', 'high', 'max')


class CapCoworkerTest(unittest.TestCase):
    def test_max_becomes_xhigh_for_coworkers(self):
        for purpose in ('', 'worker', 'planner', 'reviewer'):
            self.assertEqual(ep.cap_coworker('max', purpose), 'xhigh')

    def test_main_purposes_keep_max(self):
        for purpose in ep.MAIN_PURPOSES:
            self.assertEqual(ep.cap_coworker('max', purpose), 'max')

    def test_lower_levels_unchanged(self):
        for level in ('low', 'medium', 'high', 'xhigh'):
            self.assertEqual(ep.cap_coworker(level, 'worker'), level)
        self.assertIsNone(ep.cap_coworker(None, 'worker'))

    def test_automatic_top_level_is_xhigh_for_coworkers(self):
        self.assertEqual(ep.choose_reasoning_effort('worker', 'review', '', ALL)[0], 'xhigh')
        self.assertEqual(ep.choose_reasoning_effort('worker', 'code', 'high', ALL)[0], 'xhigh')
        self.assertEqual(ep.choose_reasoning_effort('', 'review', '', ALL)[0], 'xhigh')
        self.assertEqual(ep.choose_reasoning_effort('worker', 'code', '', ALL)[0], 'high')

    def test_main_automatic_keeps_max(self):
        self.assertEqual(ep.choose_reasoning_effort('main', '', '', ALL)[0], 'max')

    def test_model_without_xhigh_falls_below_never_up_to_max(self):
        self.assertEqual(ep.choose_reasoning_effort('worker', 'review', '', NO_XHIGH)[0], 'high')


class TaskEffortTest(unittest.TestCase):
    def _pick(self, purpose, asked):
        entry = {'modelConfiguration': {'supportedReasoningEfforts': list(ALL), 'defaultReasoningEffort': 'medium'}}
        with mock.patch.object(notion, 'catalog_entry', return_value=entry):
            return notion.choose_task_effort(object(), 'albuquerque-quinn', purpose, {'requested_effort': asked})

    def test_main_requested_max_is_sent_as_xhigh_to_coworkers(self):
        effort, reason = self._pick('worker', 'max')
        self.assertEqual(effort, 'xhigh')
        self.assertIn('clamped to xhigh', reason)

    def test_requested_xhigh_and_high_unchanged(self):
        self.assertEqual(self._pick('worker', 'xhigh')[0], 'xhigh')
        self.assertEqual(self._pick('worker', 'high')[0], 'high')


if __name__ == '__main__':
    unittest.main()
