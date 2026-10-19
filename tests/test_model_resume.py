"""Model routing after a resumed turn (work/audit/MODEL_ROUTING.md sections 3 and 5). Fakes only, no network."""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from notiontool import notion
from notiontool.model_verification import ModelVerificationError
from notiontool.notion import NotionAdapter

OPUS, SONNET, HAIKU = 'albuquerque-quinn', 'achira-donut', 'amla-jam'
WORKER = {'id': 'w1', 'thread_id': 't1', 'account_id': 'a1', 'generation': 0}


class FakeStore:
    def __init__(self, tasks=(), accounts=()):
        self.data = {'tasks': {t['id']: dict(t) for t in tasks}, 'accounts': {a['id']: dict(a) for a in accounts}}
        self.events = []
        self.lock = threading.RLock()

    def get(self, kind, ident, default=None):
        row = self.data.get(kind, {}).get(ident)
        return dict(row) if row is not None else default

    def put(self, kind, row):
        self.data.setdefault(kind, {})[row['id']] = dict(row)

    def settings(self):
        return {'task_timeout': 1800}

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, run_id, fields))


class FakeTransport:
    def __init__(self):
        self.streams = []
        self.model_catalog = {}

    def stream(self, path, body, headers=None, timeout=60, cancel=None):
        self.streams.append((path, body))
        return iter(())


class FakeClient:
    def __init__(self):
        self.transport = FakeTransport()
        self.info = {'space_id': 's1'}
        self.models = {'opus': OPUS, 'sonnet': SONNET, 'haiku': HAIKU}
        self.model_calls = []

    def transcript(self, thread_id, limit=10):
        return {'session': {'status': 'idle'}}

    def headers(self):
        return {}

    def model(self, selected='opus'):
        self.model_calls.append(selected)
        return self.models[selected]

    def call(self, path, body):
        return {'recordMap': {'thread': {'t1': {'space_id': 's1', 'messages': ['m1']}}}}


def make_adapter(store, client):
    a = object.__new__(NotionAdapter)
    a.store = store
    a.client = lambda account_id, anonymous=False: client
    a.template = {'transcript': [{'value': {'model': 'template-model'}}]}
    a.bodies = []
    a.body = lambda c, thread, step, text, model, computer=False, effort=None: a.bodies.append(model) or {'model': model}
    a.confirmed_configs = {}
    return a


class ResumeModelTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.store = FakeStore(accounts=[{'id': 'a1', 'model': OPUS}])
        self.adapter = make_adapter(self.store, self.client)

    def test_resume_without_task_keeps_the_account_model(self):
        self.assertTrue(self.adapter.continue_turn(WORKER, 'continue', 'step1'))
        self.assertEqual(self.adapter.bodies, [OPUS])
        self.assertEqual(self.client.model_calls, [])

    def test_resume_with_task_uses_the_task_model(self):
        task = {'id': 'k1', 'model': 'sonnet', 'assigned_model_id': SONNET}
        self.adapter.continue_turn(WORKER, 'continue', 'step1', task=task)
        self.assertEqual(self.adapter.bodies, [SONNET])

    def test_resume_with_task_without_assigned_id_resolves_its_model(self):
        self.adapter.continue_turn(WORKER, 'continue', 'step1', task={'id': 'k1', 'model': 'haiku'})
        self.assertEqual(self.adapter.bodies, [HAIKU])
        self.assertEqual(self.client.model_calls, ['haiku'])

    def test_resume_clears_the_config_entry_so_the_next_small_task_sets_its_model_again(self):
        # The next send skips ensure_thread_model only while this entry matches (notion.py:924-927).
        key = ('t1', 0, HAIKU)
        self.adapter.confirmed_configs['t1'] = key
        self.adapter.confirmed_configs['t2'] = ('t2', 0, HAIKU)
        self.adapter.continue_turn(WORKER, 'continue', 'step1')  # engine.py still calls without task=
        self.assertNotEqual(self.adapter.confirmed_configs.get('t1'), key)
        self.assertNotIn('t1', self.adapter.confirmed_configs)
        self.assertIn('t2', self.adapter.confirmed_configs)

    def test_busy_chat_sends_nothing_and_keeps_the_entry(self):
        self.client.transcript = lambda thread_id, limit=10: {'session': {'status': 'running'}}
        self.adapter.confirmed_configs['t1'] = ('t1', 0, HAIKU)
        self.assertFalse(self.adapter.continue_turn(WORKER, 'continue', 'step1'))
        self.assertEqual(self.adapter.bodies, [])
        self.assertIn('t1', self.adapter.confirmed_configs)


class FallbackEventTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.client.transport.model_catalog = {'s1': (0.0, [
            {'model': OPUS, 'modelMessage': 'Opus  5.5'}, {'model': SONNET, 'modelMessage': 'Sonnet 5.5'}])}
        self.task = {'id': 'k1', 'run_id': 'r1', 'step_id': 'step1', 'assigned_model_id': SONNET}
        self.store = FakeStore(tasks=[self.task], accounts=[{'id': 'a1', 'model': OPUS}])
        self.adapter = make_adapter(self.store, self.client)

    def verify(self, reported, task=None):
        def fake_verify(steps, step_id, expected):
            if reported == expected:
                return [reported]
            raise ModelVerificationError(expected, [reported])
        with mock.patch.object(notion, 'unwrap', lambda v: v), \
                mock.patch.object(notion, 'own_turn_steps', lambda records, ids, step_id: ([{}], 1)), \
                mock.patch.object(notion, 'verify_execution', fake_verify):
            return self.adapter.check_response_model(WORKER, dict(task or self.task))

    def fallback_events(self):
        return [e for e in self.store.events if e[0] == 'model_fallback']

    def test_one_model_fallback_event_per_task(self):
        self.assertEqual(self.verify(OPUS), [OPUS])
        self.assertEqual(self.verify(OPUS), [OPUS])
        self.assertEqual(self.verify(OPUS), [OPUS])
        self.assertEqual(len(self.fallback_events()), 1)
        saved = self.store.get('tasks', 'k1')
        self.assertEqual(saved['model_verification'], 'fallback')
        self.assertTrue(saved['fallback_reported'])

    def test_each_task_gets_its_own_event(self):
        other = {'id': 'k2', 'run_id': 'r1', 'step_id': 'step2', 'assigned_model_id': SONNET}
        self.store.put('tasks', other)
        self.verify(OPUS)
        self.verify(OPUS, task=other)
        self.verify(OPUS, task=other)
        self.assertEqual([e[2]['task_id'] for e in self.fallback_events()], ['k1', 'k2'])

    def test_event_names_the_replacement_model_when_known(self):
        self.verify(OPUS)
        message = self.fallback_events()[0][2]['message']
        self.assertIn('Opus 5.5 (albuquerque-quinn)', message)
        self.assertIn('Sonnet 5.5 (achira-donut)', message)
        self.assertIn('Opus 5.5', self.store.get('accounts', 'a1')['model_fallback'])

    def test_unknown_replacement_keeps_the_raw_id(self):
        self.verify('almond-croissant-low')
        self.assertIn('chats run on almond-croissant-low.', self.fallback_events()[0][2]['message'])

    def test_verified_small_model_writes_no_event(self):
        self.assertEqual(self.verify(SONNET), [SONNET])
        self.assertEqual(self.fallback_events(), [])


class NoSubstitutionTests(unittest.TestCase):
    def test_missing_label_raises_instead_of_substituting(self):
        transport = SimpleNamespace(request=lambda path, body, headers=None, timeout=45: (200, json.dumps(
            {'models': [{'model': OPUS, 'modelMessage': 'Opus 5.5', 'modelProvider': 'anthropic'}]})))
        client = notion.NotionClient(transport, {'space_id': 's1'})
        with self.assertRaises(ValueError):
            client.model('haiku')


if __name__ == '__main__':
    unittest.main()
