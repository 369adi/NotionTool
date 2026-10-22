"""Owner rule 2026-10-22: every Coworker request runs on 'medium' or 'high' only, never 'low', 'xhigh'
or 'max'. Main chats keep the highest level. Covers the automatic choice, a task-level effort set by
the Main, the escalation path and the final send guard."""
import itertools
import types
import unittest

from notiontool import effort_policy as ep
from notiontool import model_fit, notion

ALL = ('low', 'medium', 'high', 'xhigh', 'max')
BAND = ('medium', 'high')
CLASSES = ('routine', 'docs', 'test', 'review', 'code', 'research', 'design', '')
RISKS = ('low', 'medium', 'high', '')
COWORKER_PURPOSES = ('', 'worker', 'planner', 'reviewer', 'executor')
SUPPORTED = (ALL, ('low', 'medium', 'high'), ('medium',), ('high',), ('low', 'high'), ('medium', 'max'))

OPUS = {'model': 'albuquerque-quinn',
        'modelConfiguration': {'supportedReasoningEfforts': list(ALL), 'defaultReasoningEffort': 'medium'}}
OLD = {'model': 'apple-danish',
       'modelConfiguration': {'supportedReasoningEfforts': ['low', 'medium', 'high'],
                              'defaultReasoningEffort': 'high'}}
LOWONLY = {'model': 'low-only', 'modelConfiguration': {'supportedReasoningEfforts': ['low']}}
TOPONLY = {'model': 'top-only', 'modelConfiguration': {'supportedReasoningEfforts': ['xhigh', 'max']}}
BARE = {'model': 'no-efforts'}


def fake_client(models=(OPUS, OLD, LOWONLY, TOPONLY, BARE)):
    transport = types.SimpleNamespace(model_catalog={'space-1': (0.0, list(models))})
    return types.SimpleNamespace(info={'space_id': 'space-1'}, transport=transport)


def expected(cls, risk):
    if risk == 'high' or cls == 'review':
        return 'high'
    if cls in ('routine', 'docs', 'test') or risk == 'low':
        return 'medium'
    return 'high'


class BandTest(unittest.TestCase):
    def test_band_constants(self):
        self.assertEqual(ep.COWORKER_TOP_EFFORT, 'high')
        self.assertEqual(ep.COWORKER_MIN_EFFORT, 'medium')
        self.assertEqual(ep.coworker_band(), BAND)

    def test_cap_coworker_clamps_from_both_sides(self):
        for purpose in COWORKER_PURPOSES:
            self.assertEqual([ep.cap_coworker(level, purpose) for level in ALL],
                             ['medium', 'medium', 'high', 'high', 'high'])
        for purpose in ep.MAIN_PURPOSES:
            self.assertEqual([ep.cap_coworker(level, purpose) for level in ALL], list(ALL))

    def test_band_effort_no_level_in_band_uses_closest_and_says_so(self):
        self.assertEqual(ep.band_effort('high', ('low',), 'worker')[0], 'low')
        self.assertIn('no level in the Coworker band', ep.band_effort('high', ('low',), 'worker')[1])
        self.assertEqual(ep.band_effort('high', ('xhigh', 'max'), 'worker')[0], 'xhigh')
        self.assertEqual(ep.band_effort('medium', ('low', 'xhigh'), 'worker')[0], 'low')  # tie -> weaker
        self.assertEqual(ep.band_effort('high', (), 'worker'), (None, ''))
        self.assertEqual(ep.band_effort('max', ALL, 'main'), ('max', ''))


class ChooseEverythingTest(unittest.TestCase):
    def test_every_class_and_risk_stays_in_band(self):
        for purpose, cls, risk, sup in itertools.product(COWORKER_PURPOSES, CLASSES, RISKS, (ALL, ('low', 'medium', 'high'))):
            effort, reason = ep.choose_reasoning_effort(purpose, cls, risk, sup, 'medium')
            self.assertEqual(effort, expected(cls, risk), (purpose, cls, risk, sup, reason))
            self.assertIn(effort, BAND)
            self.assertNotIn(effort, ('low', 'xhigh', 'max'))

    def test_default_model_effort_is_not_used(self):
        for default in ('low', 'medium', 'high', 'xhigh', 'max', None):
            self.assertEqual(ep.choose_reasoning_effort('worker', 'docs', 'low', ALL, default)[0], 'medium')

    def test_clamped_to_supported_after_band(self):
        self.assertEqual(ep.choose_reasoning_effort('worker', 'code', '', ('medium',))[0], 'medium')
        self.assertEqual(ep.choose_reasoning_effort('worker', 'docs', '', ('high',))[0], 'high')
        self.assertEqual(ep.choose_reasoning_effort('worker', 'code', '', ('low', 'high'))[0], 'high')
        self.assertEqual(ep.choose_reasoning_effort('worker', 'docs', '', ('low', 'high'))[0], 'high')

    def test_no_level_in_band_picks_closest_and_reason_says_so(self):
        effort, reason = ep.choose_reasoning_effort('worker', 'code', '', ('low',))
        self.assertEqual(effort, 'low')
        self.assertIn('no level in the Coworker band', reason)
        effort, reason = ep.choose_reasoning_effort('worker', 'code', '', ('xhigh', 'max'))
        self.assertEqual(effort, 'xhigh')
        self.assertIn('closest supported level xhigh', reason)

    def test_main_chat_keeps_highest_level(self):
        for cls, risk in itertools.product(CLASSES, RISKS):
            self.assertEqual(ep.choose_reasoning_effort('main', cls, risk, ALL)[0], 'max')
        self.assertEqual(ep.choose_reasoning_effort('main', '', '', ('low', 'medium', 'high'))[0], 'high')
        for purpose in ep.MAIN_PURPOSES:
            self.assertEqual(ep.cap_coworker('max', purpose), 'max')

    def test_choose_effort_through_notion_catalog(self):
        client = fake_client()
        for cls, risk in itertools.product(CLASSES, RISKS):
            self.assertEqual(notion.choose_effort(client, 'albuquerque-quinn', 'worker', cls, risk)[0],
                             expected(cls, risk))
        self.assertEqual(notion.choose_effort(client, 'albuquerque-quinn', 'main')[0], 'max')


class TaskLevelEffortTest(unittest.TestCase):
    def pick(self, asked, model='albuquerque-quinn', purpose='worker'):
        return notion.choose_task_effort(fake_client(), model, purpose, {'requested_effort': asked})

    def test_xhigh_max_low_are_sent_inside_the_band(self):
        for asked, want in (('xhigh', 'high'), ('max', 'high'), ('low', 'medium'),
                            ('high', 'high'), ('medium', 'medium')):
            effort, reason = self.pick(asked)
            self.assertEqual(effort, want, (asked, reason))
            if want != asked:
                self.assertIn('clamped to ' + want, reason)

    def test_old_model_and_every_coworker_purpose(self):
        for purpose in COWORKER_PURPOSES:
            for asked in ALL:
                self.assertIn(self.pick(asked, 'apple-danish', purpose)[0], BAND)

    def test_model_without_efforts_sends_none(self):
        self.assertIsNone(self.pick('high', 'no-efforts')[0])

    def test_model_without_band_level_uses_closest_and_says_so(self):
        effort, reason = self.pick('high', 'low-only')
        self.assertEqual(effort, 'low')
        self.assertIn('no level in the Coworker band', reason)

    def test_main_chat_keeps_automatic_choice(self):
        self.assertEqual(self.pick('low', purpose='main')[0], 'max')


class FinalGuardTest(unittest.TestCase):
    def test_stale_values_are_banded_for_workers(self):
        client = fake_client()
        for stale, want in (('low', 'medium'), ('xhigh', 'high'), ('max', 'high'), ('medium', 'medium'), ('high', 'high')):
            for purpose in COWORKER_PURPOSES:
                self.assertEqual(notion.final_effort(client, 'albuquerque-quinn', stale, purpose), want)
                self.assertEqual(notion.final_effort(client, 'apple-danish', stale, purpose), want)
        self.assertEqual(notion.final_effort(client, 'unknown-model', 'max', 'worker'), 'high')
        self.assertEqual(notion.final_effort(client, 'unknown-model', 'low', 'worker'), 'medium')

    def test_main_and_empty_values_pass_through(self):
        client = fake_client()
        for purpose in ep.MAIN_PURPOSES:
            self.assertEqual(notion.final_effort(client, 'albuquerque-quinn', 'max', purpose), 'max')
        self.assertIsNone(notion.final_effort(client, 'albuquerque-quinn', None, 'worker'))
        self.assertEqual(notion.final_effort(client, 'albuquerque-quinn', '', 'worker'), '')

    def test_send_and_resume_paths_use_the_guard(self):
        import inspect
        source = inspect.getsource(notion)
        self.assertIn("effort=final_effort(client, model, task.get('effort'), worker.get('purpose', '')))", source)
        self.assertIn("effort = final_effort(client, model, effort, worker.get('purpose', ''))", source)
        self.assertNotIn("effort=task.get('effort'))", source)


class EscalationTest(unittest.TestCase):
    def test_coworker_escalation_never_leaves_the_band(self):
        for start, steps, sup in itertools.product(ALL, range(-1, 7), SUPPORTED):
            for purpose in ('', 'worker'):
                got = model_fit.notion_effort(start, sup, steps, purpose)
                self.assertIn(got, BAND + tuple(l for l in sup if l not in BAND) if not set(sup) & set(BAND) else BAND,
                              (start, steps, sup, got))

    def test_escalation_steps_inside_the_band(self):
        self.assertEqual(model_fit.notion_effort('low'), 'medium')
        self.assertEqual(model_fit.notion_effort('medium'), 'medium')
        self.assertEqual(model_fit.notion_effort('medium', escalation=1), 'high')
        self.assertEqual(model_fit.notion_effort('low', escalation=1), 'high')
        self.assertEqual(model_fit.notion_effort('high', escalation=1), 'high')
        for steps in (2, 3, 5, 50):
            for start in ALL:
                self.assertEqual(model_fit.notion_effort(start, escalation=steps), 'high')
        self.assertEqual(model_fit.notion_effort('medium', escalation=-3), 'medium')

    def test_escalation_respects_supported_levels(self):
        self.assertEqual(model_fit.notion_effort('medium', ('low', 'medium', 'high'), escalation=4), 'high')
        self.assertEqual(model_fit.notion_effort('medium', ('low', 'medium'), escalation=4), 'medium')

    def test_main_escalation_keeps_old_behaviour(self):
        for purpose in ep.MAIN_PURPOSES:
            self.assertEqual(model_fit.notion_effort('high', escalation=1, purpose=purpose), 'xhigh')
            self.assertEqual(model_fit.notion_effort('high', escalation=2, purpose=purpose), 'max')
            self.assertEqual(model_fit.notion_effort('low', purpose=purpose), 'low')


if __name__ == '__main__':
    unittest.main()
