import copy
import json
import types
import unittest
from pathlib import Path

from notiontool import effort_policy as ep
from notiontool import engine, notion, task_router as tr

OPUS = {'model': 'albuquerque-quinn', 'modelMessage': 'Opus 5.5',
        'modelConfiguration': {'supportedReasoningEfforts': ['low', 'medium', 'high', 'xhigh', 'max'],
                               'defaultReasoningEffort': 'medium'}}
OLD = {'model': 'apple-danish', 'modelMessage': 'Opus 4.5',
       'modelConfiguration': {'supportedReasoningEfforts': ['low', 'medium', 'high'],
                              'defaultReasoningEffort': 'high'}}
BARE = {'model': 'no-efforts', 'modelMessage': 'Bare'}


def fake_client(models=(OPUS, OLD, BARE)):
    transport = types.SimpleNamespace(model_catalog={'space-1': (0.0, list(models))})
    return types.SimpleNamespace(info={'space_id': 'space-1'}, transport=transport)


def adapter_class():
    return next(v for v in vars(notion).values()
                if isinstance(v, type) and hasattr(v, 'body') and hasattr(v, 'ensure_thread_model'))


class CatalogTest(unittest.TestCase):
    def test_catalog_efforts_reads_model_configuration(self):
        self.assertEqual(ep.catalog_efforts(OPUS), (('low', 'medium', 'high', 'xhigh', 'max'), 'medium'))
        self.assertEqual(ep.catalog_efforts(BARE), ((), None))
        self.assertEqual(ep.catalog_efforts(None), ((), None))
        odd = {'supportedReasoningEfforts': ['HIGH', 'bogus', 'low'], 'defaultReasoningEffort': 'max'}
        self.assertEqual(ep.catalog_efforts(odd), (('low', 'high'), None))

    def test_clamp(self):
        sup = ('low', 'medium', 'high')
        self.assertEqual(ep.clamp_effort('max', sup), 'high')
        self.assertEqual(ep.clamp_effort('medium', sup), 'medium')
        self.assertEqual(ep.clamp_effort('low', ('high', 'max')), 'high')
        self.assertIsNone(ep.clamp_effort('high', ()))


class RuleTest(unittest.TestCase):
    sup, default = ep.catalog_efforts(OPUS)

    def pick(self, purpose='', cls='', risk='', sup=None, default='unset'):
        return ep.choose_reasoning_effort(purpose, cls, risk, self.sup if sup is None else sup,
                                          self.default if default == 'unset' else default)

    def test_main_review_and_high_risk_get_highest(self):
        self.assertEqual(self.pick('main')[0], 'max')
        self.assertEqual(self.pick(cls='review', risk='low')[0], 'max')
        self.assertEqual(self.pick(cls='code', risk='high')[0], 'max')
        self.assertEqual(self.pick('main', sup=('low', 'medium', 'high'))[0], 'high')

    def test_code_research_medium_get_high(self):
        self.assertEqual(self.pick(cls='code', risk='medium')[0], 'high')
        self.assertEqual(self.pick(cls='research')[0], 'high')
        self.assertEqual(self.pick()[0], 'high')  # unclassified work

    def test_light_work_gets_default_or_low(self):
        self.assertEqual(self.pick(cls='docs', risk='low')[0], 'medium')
        self.assertEqual(self.pick(cls='routine')[0], 'medium')
        self.assertEqual(self.pick(cls='test', risk='low', default=None)[0], 'low')
        self.assertEqual(self.pick(cls='code', risk='low')[0], 'medium')

    def test_clamped_and_reasoned(self):
        effort, reason = self.pick(cls='code', sup=('low', 'medium'), default='low')
        self.assertEqual(effort, 'medium')
        self.assertIn('clamped from high', reason)
        self.assertIn('supported: low, medium', reason)

    def test_no_catalog_means_no_effort(self):
        effort, reason = self.pick('main', sup=())
        self.assertIsNone(effort)
        self.assertIn('no supportedReasoningEfforts', reason)


class NotionWiringTest(unittest.TestCase):
    def test_field_name(self):
        self.assertEqual(notion.EFFORT_FIELD, 'reasoningEffort')

    def test_choose_effort_uses_cached_catalog(self):
        client = fake_client()
        self.assertEqual(notion.choose_effort(client, 'albuquerque-quinn', 'main')[0], 'max')
        self.assertEqual(notion.choose_effort(client, 'apple-danish', 'main')[0], 'high')
        self.assertIsNone(notion.choose_effort(client, 'no-efforts', 'main')[0])
        self.assertIsNone(notion.choose_effort(client, 'missing', 'main')[0])
        bare = types.SimpleNamespace(info={'space_id': 'space-1'})  # test fakes without transport
        self.assertIsNone(notion.choose_effort(bare, 'albuquerque-quinn', 'main')[0])

    def _body(self, effort):
        cls = adapter_class()
        template = json.loads((Path(notion.__file__).parent / 'template.json').read_text(encoding='utf-8'))
        fake = types.SimpleNamespace(template=copy.deepcopy(template), new_id=cls.new_id)
        client = types.SimpleNamespace(info={'space_id': '00000000-0000-0000-0000-000000000001',
                                             'user_id': 'u', 'email': 'e@x', 'records': {}})
        return cls.body(fake, client, 'thread', 'step', 'hi', 'albuquerque-quinn', effort=effort)

    def test_body_carries_effort_in_updated_config_step(self):
        # Live proof 2026-10-20: model/effort only work in the 'updated-config' step; the config step
        # keeps the base model and no effort.
        body = self._body('max')
        value = body['transcript'][0]['value']
        self.assertNotIn('reasoningEffort', value)
        self.assertEqual(value['model'], notion.BASE_CONFIG_MODEL)
        self.assertTrue(value['modelFromUser'])
        steps = [s for s in body['transcript'] if s.get('type') == 'updated-config']
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]['value'], {'model': 'albuquerque-quinn', 'reasoningEffort': 'max'})
        self.assertEqual(body['transcript'][-1]['type'], 'user')
        self.assertEqual(body['transcript'][-2]['type'], 'updated-config')

    def test_body_without_effort_sends_no_field(self):
        body = self._body(None)
        self.assertNotIn('reasoningEffort', body['transcript'][0]['value'])
        for step in body['transcript']:
            if step.get('type') == 'updated-config':
                self.assertNotIn('reasoningEffort', step['value'])


class RoutingReasonTest(unittest.TestCase):
    def test_unclassified_work_is_design_on_opus(self):
        rule = tr.rule_for('', None)
        self.assertEqual((rule['model'], rule['task_class']), ('opus', 'design'))
        self.assertIn('Opus 5.5 is the default model', rule['model_reason'])
        self.assertIn('UI/design/complex', rule['model_reason'])
        self.assertEqual(tr.rule_for('UI', 'low')['model'], 'opus')
        self.assertEqual(tr.decide('', None)['model'], 'opus')

    def test_unrouted_executor_reason_names_rule(self):
        cls = next(v for v in vars(engine).values() if isinstance(v, type) and hasattr(v, '_rule_reason'))
        task = cls._rule_reason({'model': 'opus', 'task_class': '', 'model_reason': ''})
        self.assertTrue(task['model_reason'].startswith('model rule: executor (design): Opus 5.5 is the default model'))
        self.assertNotIn('fallback', task['model_reason'])
        task = cls._rule_reason({'model': 'opus', 'task_class': 'docs', 'risk': 'low', 'model_reason': ''})
        self.assertIn('routing is off for this run, Opus 5.5 kept', task['model_reason'])
        self.assertIn('rule would pick Haiku 5.5', task['model_reason'])


if __name__ == '__main__':
    unittest.main()
