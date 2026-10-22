"""Rule I38: the Main can set the reasoning effort per task; notion.py sends it clamped to the model."""
import inspect
import os
import tempfile
import types
import unittest

from notiontool import notion
from notiontool.engine import Engine
from notiontool.operator_main import EFFORTS, KNOWN_TASK_KEYS, delegate, register_operator, task_effort
from notiontool.store import Store

ACC = 'acc1'
OP = ACC + ':op-thread'
COWORKERS = (ACC + ':co1', ACC + ':co2')

OPUS = {'model': 'albuquerque-quinn',
        'modelConfiguration': {'supportedReasoningEfforts': ['low', 'medium', 'high', 'xhigh', 'max'],
                               'defaultReasoningEffort': 'medium'}}
OLD = {'model': 'apple-danish',
       'modelConfiguration': {'supportedReasoningEfforts': ['low', 'medium', 'high'],
                              'defaultReasoningEffort': 'high'}}
BARE = {'model': 'no-efforts'}


def fake_client(models=(OPUS, OLD, BARE)):
    transport = types.SimpleNamespace(model_catalog={'space-1': (0.0, list(models))})
    return types.SimpleNamespace(info={'space_id': 'space-1'}, transport=transport)


class Adapter:  # fake client: no Notion call is ever made
    pass


def spec(ident='T1', **extra):
    return dict(dict(id=ident, title='Part ' + ident, prompt='Do ' + ident, acceptance='it works', depends_on=[]),
                **extra)


class DelegateEffortTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s = Store(os.path.join(self.temp.name, 'state'))
        s.put('accounts', dict(id=ACC, state='ready', bridge_connected=True, team_threads={}))
        s.put('workers', dict(id=OP, account_id=ACC, thread_id='op-thread', purpose='worker', enabled=False,
                              state='ready', lease='', label='Operator', join_state='joined'))
        for ident in COWORKERS:
            s.put('workers', dict(id=ident, account_id=ACC, thread_id=ident.split(':')[1], purpose='worker',
                                  enabled=True, state='ready', lease='', label='Welcome', join_state='joined'))
        self.engine = Engine(s, Adapter())
        self.chat = register_operator(s, OP, project_path=self.temp.name)['chat_id']

    def tearDown(self):
        self.engine.close()
        close = getattr(self.store, 'close', None)
        if close:
            close()
        self.temp.cleanup()

    def executors(self, out):
        return {t['title']: t for t in self.engine.tasks(out['run_id']) if t.get('role') == 'executor'}

    def test_effort_and_risk_are_known_keys(self):
        self.assertIn('effort', KNOWN_TASK_KEYS)
        self.assertIn('risk', KNOWN_TASK_KEYS)
        self.assertEqual(EFFORTS, ('low', 'medium', 'high', 'xhigh', 'max'))
        self.assertEqual(task_effort({'effort': ' XHigh '}), 'xhigh')
        self.assertEqual(task_effort({'effort': 'turbo'}), '')
        self.assertEqual(task_effort({'effort': 3}), '')
        self.assertEqual(task_effort({}), '')

    def test_override_stored_on_task_without_warning(self):
        out = delegate(self.engine, self.chat, 'Do two parts',
                       [spec('A1', effort='xhigh', risk='high'), spec('A2')])
        self.assertEqual([w for w in out['warnings'] if 'effort' in w or 'unknown key' in w], [])
        tasks = self.executors(out)
        self.assertEqual(tasks['Part A1'].get('requested_effort'), 'xhigh')
        self.assertEqual(tasks['Part A1'].get('risk'), 'high')
        self.assertNotIn('requested_effort', tasks['Part A2'])

    def test_invalid_effort_ignored_with_warning(self):
        out = delegate(self.engine, self.chat, 'Do one part', [spec('B1', effort='turbo')])
        effort_warnings = [w for w in out['warnings'] if 'effort' in w]
        self.assertEqual(len(effort_warnings), 1)
        self.assertIn("Task B1: effort 'turbo' is not one of low, medium, high, xhigh, max", effort_warnings[0])
        self.assertNotIn('unknown key', effort_warnings[0])
        self.assertNotIn('requested_effort', self.executors(out)['Part B1'])


class ChooseTaskEffortTest(unittest.TestCase):
    def test_override_used(self):
        client = fake_client()
        self.assertEqual(notion.choose_task_effort(client, 'albuquerque-quinn', 'worker',
                                                   {'requested_effort': 'xhigh', 'task_class': 'docs'}),
                         ('xhigh', 'set by Main: xhigh'))
        self.assertEqual(notion.choose_task_effort(client, 'albuquerque-quinn', 'worker', {'requested_effort': 'low'}),
                         ('low', 'set by Main: low'))

    def test_clamped_when_unsupported(self):
        self.assertEqual(notion.choose_task_effort(fake_client(), 'apple-danish', 'worker', {'requested_effort': 'max'}),
                         ('high', 'set by Main: max, clamped to high'))

    def test_model_without_efforts_sends_none(self):
        effort, reason = notion.choose_task_effort(fake_client(), 'no-efforts', 'worker', {'requested_effort': 'high'})
        self.assertIsNone(effort)
        self.assertTrue(reason.startswith('set by Main: high; not sent'))

    def test_no_override_is_automatic(self):
        client = fake_client()
        task = {'task_class': 'review', 'risk': 'medium'}
        self.assertEqual(notion.choose_task_effort(client, 'albuquerque-quinn', 'worker', task),
                         notion.choose_effort(client, 'albuquerque-quinn', 'worker', 'review', 'medium'))
        bad = {'requested_effort': 'turbo', 'task_class': 'docs'}
        self.assertEqual(notion.choose_task_effort(client, 'albuquerque-quinn', 'worker', bad),
                         notion.choose_effort(client, 'albuquerque-quinn', 'worker', 'docs', ''))
        self.assertEqual(notion.choose_task_effort(client, 'albuquerque-quinn', 'worker', None),
                         notion.choose_effort(client, 'albuquerque-quinn', 'worker'))

    def test_main_chat_keeps_automatic(self):
        client = fake_client()
        self.assertEqual(notion.choose_task_effort(client, 'albuquerque-quinn', 'main', {'requested_effort': 'low'}),
                         notion.choose_effort(client, 'albuquerque-quinn', 'main'))

    def test_send_path_uses_override(self):
        source = inspect.getsource(notion)
        self.assertIn("effort, effort_reason = choose_task_effort(client, model, worker.get('purpose', ''),", source)
        self.assertIn('effort=effort, effort_reason=effort_reason', source)
        self.assertIn("effort=task.get('effort'))", source)


if __name__ == '__main__':
    unittest.main()
