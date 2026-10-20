import unittest

from notiontool import notion


class FakeClient:
    def __init__(self, steps, fail=False):
        self.info = {'space_id': 'space'}
        self.steps = steps
        self.fail = fail
        self.calls = 0

    def model(self, name):
        return {'opus': 'albuquerque-quinn', 'sonnet': 'achira-donut', 'haiku': 'amla-jam'}[name]

    def call(self, endpoint, payload):
        self.calls += 1
        if self.fail:
            raise RuntimeError('offline')
        table = payload['requests'][0]['pointer']['table']
        if table == 'thread':
            ids = ['m%d' % i for i in range(len(self.steps))]
            return {'recordMap': {'thread': {'t': {'value': {'messages': ids}}}}}
        out = {}
        for req in payload['requests']:
            ident = req['pointer']['id']
            out[ident] = {'value': {'step': self.steps[int(ident[1:])]}}
        return {'recordMap': {'thread_message': out}}


def uc(model=None, effort=None):
    value = {}
    if model:
        value['model'] = model
    if effort:
        value['reasoningEffort'] = effort
    return {'type': 'updated-config', 'value': value}


class MainEffortInheritTest(unittest.TestCase):
    def test_newest_choice_wins(self):
        steps = [uc('albuquerque-quinn', 'medium'), {'type': 'user'}, uc('achira-donut', 'xhigh'), {'type': 'agent-inference'}]
        self.assertEqual(notion.latest_user_config(steps), ('achira-donut', 'xhigh'))

    def test_inherit_returns_user_pick(self):
        client = FakeClient([{'type': 'config', 'value': {'model': 'albuquerque-quinn'}}, uc('amla-jam', 'High')])
        selected, model, effort, reason = notion.inherit_main_config(client, 't')
        self.assertEqual((selected, model, effort), ('haiku', 'amla-jam', 'high'))
        self.assertIn('user choice', reason)

    def test_effort_only_keeps_opus(self):
        client = FakeClient([uc(effort='max')])
        self.assertEqual(notion.inherit_main_config(client, 't')[:3], ('opus', 'albuquerque-quinn', 'max'))

    def test_no_choice_or_error_falls_back(self):
        self.assertIsNone(notion.inherit_main_config(FakeClient([{'type': 'user'}]), 't'))
        self.assertIsNone(notion.inherit_main_config(FakeClient([uc('x', 'low')], fail=True), 't'))

    def test_only_main_purposes(self):
        self.assertIn('main', notion.MAIN_OWN_CONFIG)
        self.assertIn('operator', notion.MAIN_OWN_CONFIG)
        self.assertNotIn('worker', notion.MAIN_OWN_CONFIG)


if __name__ == '__main__':
    unittest.main()
