"""Owner rule 2026-10-22 (replaces I47): Coworker effort stays in the band medium..high, never low,
xhigh or max. Main chats keep 'max'."""
import unittest
from unittest import mock

from notiontool import effort_policy as ep
from notiontool import notion

ALL = ('low', 'medium', 'high', 'xhigh', 'max')
NO_XHIGH = ('low', 'medium', 'high', 'max')


class CapCoworkerTest(unittest.TestCase):
    def test_max_becomes_high_for_coworkers(self):
        for purpose in ('', 'worker', 'planner', 'reviewer'):
            self.assertEqual(ep.cap_coworker('max', purpose), 'high')

    def test_main_purposes_keep_max(self):
        for purpose in ep.MAIN_PURPOSES:
            self.assertEqual(ep.cap_coworker('max', purpose), 'max')

    def test_levels_are_put_into_the_band(self):
        self.assertEqual(ep.cap_coworker('low', 'worker'), 'medium')
        self.assertEqual(ep.cap_coworker('medium', 'worker'), 'medium')
        self.assertEqual(ep.cap_coworker('high', 'worker'), 'high')
        self.assertEqual(ep.cap_coworker('xhigh', 'worker'), 'high')
        self.assertIsNone(ep.cap_coworker(None, 'worker'))

    def test_automatic_top_level_is_high(self):
        # Even the hardest work (risk high) runs on high for Coworkers.
        self.assertEqual(ep.choose_reasoning_effort('worker', 'code', 'high', ALL)[0], 'high')
        self.assertEqual(ep.choose_reasoning_effort('worker', 'review', 'high', ALL)[0], 'high')
        self.assertEqual(ep.choose_reasoning_effort('worker', 'review', '', ALL)[0], 'high')
        self.assertEqual(ep.choose_reasoning_effort('', 'review', 'low', ALL)[0], 'high')
        self.assertEqual(ep.choose_reasoning_effort('worker', 'code', '', ALL)[0], 'high')
        self.assertEqual(ep.choose_reasoning_effort('worker', 'research', 'medium', ALL)[0], 'high')

    def test_main_automatic_keeps_max(self):
        self.assertEqual(ep.choose_reasoning_effort('main', '', '', ALL)[0], 'max')

    def test_model_without_xhigh_falls_below_never_up_to_max(self):
        self.assertEqual(ep.choose_reasoning_effort('worker', 'review', '', NO_XHIGH)[0], 'high')


class TaskEffortTest(unittest.TestCase):
    def _pick(self, purpose, asked):
        entry = {'modelConfiguration': {'supportedReasoningEfforts': list(ALL), 'defaultReasoningEffort': 'medium'}}
        with mock.patch.object(notion, 'catalog_entry', return_value=entry):
            return notion.choose_task_effort(object(), 'albuquerque-quinn', purpose, {'requested_effort': asked})

    def test_main_requested_max_is_sent_as_high_to_coworkers(self):
        effort, reason = self._pick('worker', 'max')
        self.assertEqual(effort, 'high')
        self.assertIn('clamped to high', reason)

    def test_requested_levels_land_in_the_band(self):
        self.assertEqual(self._pick('worker', 'xhigh')[0], 'high')
        self.assertEqual(self._pick('worker', 'high')[0], 'high')
        self.assertEqual(self._pick('worker', 'medium')[0], 'medium')
        self.assertEqual(self._pick('worker', 'low')[0], 'medium')


if __name__ == '__main__':
    unittest.main()
