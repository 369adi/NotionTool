"""Notion shell attention items, run_confirm and the three context settings (batch 15)."""
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from notiontool import notion_shell as ns


ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / 'notiontool' / 'web' / 'notion_shell' / 'overlay.js'
SERVER_PATH = ROOT / 'notiontool' / 'server.py'
CREATE_WORDS = ('new_welcome_chat', 'create_chat', 'new_chat', 'create_main', 'main/create', 'account/create-chat')


class FakeStore:
    def __init__(self, data=None, settings=None):
        self.data = {k: [dict(r) for r in v] for k, v in (data or {}).items()}
        self._settings = dict(settings or {})

    def all(self, kind):
        return [dict(r) for r in self.data.get(kind, [])]

    def get(self, kind, ident, default=None):
        for row in self.data.get(kind, []):
            if row.get('id') == ident:
                return dict(row)
        return default

    def settings(self):
        return dict(self._settings)


class FakeServer:
    def __init__(self, store, status=200, payload=None):
        self.store = store
        self.calls = []
        self.confirms = []
        self.status = status
        self.payload = payload if payload is not None else {'ok': True, 'id': 'r1', 'state': 'executing', 'result': None}

    def action(self, path, body):
        self.calls.append((path, body))
        return {'ok': True}

    def confirm_run_request(self, body):
        self.confirms.append(body)
        return self.status, self.payload


def awaiting_store(count=12):
    runs = [{'id': 'r%02d' % i, 'account_id': 'a1', 'state': 'awaiting_main', 'created': i, 'goal': 'G' * 400,
             'needs_main': ['t%d' % n for n in range(15)],
             'needs_main_reasons': {'t0': 'R' * 500, 't1': 'verifier risk'}} for i in range(count)]
    runs.append({'id': 'rx', 'account_id': 'a1', 'state': 'executing', 'created': 99, 'goal': 'Busy'})
    runs.append({'id': 'ro', 'account_id': 'other', 'state': 'awaiting_main', 'created': 100, 'goal': 'Other'})
    workers = [{'id': 'w1', 'account_id': 'a1', 'purpose': 'worker'}, {'id': 'wo', 'account_id': 'other', 'purpose': 'worker'}]
    tasks = [{'id': 's%02d' % i, 'run_id': 'rx', 'worker_id': 'w1', 'state': 'running', 'title': 'T' * 300}
             for i in range(12)]
    tasks.append({'id': 'so', 'run_id': 'ro', 'worker_id': 'wo', 'state': 'running', 'title': 'Other'})
    return FakeStore({'runs': runs, 'workers': workers, 'tasks': tasks})


class AttentionStateTests(unittest.TestCase):
    def test_empty_store_has_empty_attention_lists(self):
        state = ns.build_state(FakeStore(), 'a1', {}, now=1.0)
        self.assertEqual((state['awaiting_main'], state['stalled'], state['stale_context_tasks']), ([], [], []))
        json.dumps(state)

    def test_awaiting_main_pass_through_and_trimming(self):
        state = ns.build_state(awaiting_store(), 'a1', {}, now=1000.0)
        awaiting = state['awaiting_main']
        self.assertEqual(len(awaiting), ns.ATTENTION_LIMIT)
        self.assertEqual([r['id'] for r in awaiting], ['r%02d' % i for i in range(2, 12)])
        first = awaiting[0]
        self.assertEqual(set(first), {'id', 'goal', 'needs_main'})
        self.assertEqual(len(first['goal']), ns.ATTENTION_TEXT)
        self.assertEqual(len(first['needs_main']), ns.ATTENTION_LIMIT)
        self.assertEqual(first['needs_main'][0], {'task_id': 't0', 'reason': 'R' * ns.ATTENTION_TEXT})
        self.assertEqual(first['needs_main'][1], {'task_id': 't1', 'reason': 'verifier risk'})
        self.assertEqual(first['needs_main'][2], {'task_id': 't2', 'reason': ''})
        self.assertNotIn('ro', [r['id'] for r in awaiting])
        self.assertNotIn('rx', [r['id'] for r in awaiting])

    def test_stalled_from_server_memory_trimmed_and_scoped(self):
        stalled = {'s%02d' % i: {'last_progress_at': 1000.0 - 100 - i, 'worker_label': 'Coworker ' + 'L' * 200}
                   for i in range(12)}
        stalled['so'] = {'last_progress_at': 1.0, 'worker_label': 'Other'}
        stalled['gone'] = {'last_progress_at': 1.0}
        state = ns.build_state(awaiting_store(), 'a1', {}, now=1000.0, stalled=stalled)
        items = state['stalled']
        self.assertEqual(len(items), ns.ATTENTION_LIMIT)
        self.assertEqual(items[0]['task_id'], 's11')
        self.assertEqual(items[0]['stall_age_s'], 111)
        self.assertEqual(set(items[0]), {'task_id', 'title', 'stall_age_s', 'worker_label'})
        self.assertEqual(len(items[0]['title']), ns.ATTENTION_TEXT)
        self.assertLessEqual(len(items[0]['worker_label']), 80)
        self.assertNotIn('so', [i['task_id'] for i in items])
        self.assertNotIn('gone', [i['task_id'] for i in items])

    def test_stale_context_tasks_pass_through_and_trimming(self):
        fake = [{'id': 'c%02d' % i, 'state': 'checkpoint', 'title': 'C' * 300, 'worker_id': '',
                 'age_seconds': 9000 - i, 'worker_state': 'missing'} for i in range(15)]
        fake.append({'id': 'cx', 'state': 'checkpoint', 'title': 'X', 'worker_id': 'wo', 'age_seconds': 1,
                     'worker_state': 'ready'})
        with mock.patch('notiontool.stale_tasks.stale_context_tasks', return_value=fake):
            state = ns.build_state(awaiting_store(), 'a1', {}, now=1000.0)
        items = state['stale_context_tasks']
        self.assertEqual(len(items), ns.ATTENTION_LIMIT)
        self.assertEqual(items[0], {'id': 'c00', 'title': 'C' * ns.ATTENTION_TEXT, 'state': 'checkpoint',
                                    'age_seconds': 9000, 'worker_state': 'missing'})
        self.assertNotIn('cx', [i['id'] for i in items])

    def test_stale_context_failure_never_breaks_the_state(self):
        with mock.patch('notiontool.stale_tasks.stale_context_tasks', side_effect=RuntimeError('boom')):
            state = ns.build_state(awaiting_store(), 'a1', {}, now=1000.0)
        self.assertEqual(state['stale_context_tasks'], [])

    def test_state_handler_passes_server_stall_memory(self):
        server = FakeServer(awaiting_store())
        server.stalled_tasks = {'s00': {'last_progress_at': 900.0, 'worker_label': 'Coworker 1'}}
        handlers = ns.default_handlers(server, sleep=lambda s: None)
        with mock.patch('notiontool.harness_account.selected_account', return_value='a1'):
            state = handlers['state']({})
        self.assertEqual([i['task_id'] for i in state['stalled']], ['s00'])
        self.assertEqual(len(state['awaiting_main']), ns.ATTENTION_LIMIT)


class RunConfirmTests(unittest.TestCase):
    def test_route_exists_in_server(self):
        source = SERVER_PATH.read_text(encoding='utf-8')
        self.assertIn("path=='/api/run/confirm'", source)
        self.assertIn('app.confirm_run_request(self.read_body())', source)
        self.assertIn('def confirm_run_request(self, body):', source)

    def test_valid_id_calls_confirm_route(self):
        server = FakeServer(FakeStore())
        handlers = ns.default_handlers(server, sleep=lambda s: None)
        result = handlers['run_confirm']({'id': ' r1 '})
        self.assertEqual(server.confirms, [{'id': 'r1'}])
        self.assertEqual(result['state'], 'executing')
        self.assertEqual(server.calls, [])

    def test_invalid_ids_are_rejected_before_the_route(self):
        server = FakeServer(FakeStore())
        handlers = ns.default_handlers(server, sleep=lambda s: None)
        for bad in ({}, {'id': ''}, {'id': '   '}, {'id': None}, {'id': 5}, {'id': ['r1']}, {'id': {'x': 1}}):
            with self.assertRaises(ValueError):
                handlers['run_confirm'](bad)
        self.assertEqual(server.confirms, [])

    def test_server_refusal_becomes_an_error(self):
        server = FakeServer(FakeStore(), 409, {'error': 'Run is not awaiting Main confirmation (state: done)'})
        handlers = ns.default_handlers(server, sleep=lambda s: None)
        with self.assertRaisesRegex(ValueError, 'not awaiting Main confirmation'):
            handlers['run_confirm']({'id': 'r1'})

    def test_dispatch_through_the_shell(self):
        server = FakeServer(FakeStore())
        fake = mock.MagicMock()
        fake.handlers = ns.default_handlers(server, sleep=lambda s: None)
        fake._reply = lambda ident, payload: (ident, payload)
        ident, payload = ns.NotionShell.dispatch(fake, json.dumps({'id': 'x', 'action': 'run_confirm', 'args': {'id': 'r1'}}))
        self.assertTrue(payload['ok'], payload)
        ident, payload = ns.NotionShell.dispatch(fake, json.dumps({'id': 'y', 'action': 'run_confirm', 'args': {'id': ''}}))
        self.assertFalse(payload['ok'])
        self.assertEqual(server.confirms, [{'id': 'r1'}])


class AllowListTests(unittest.TestCase):
    def test_run_confirm_in_both_allow_lists(self):
        self.assertIn('run_confirm', ns.ACTIONS)
        js = JS_PATH.read_text(encoding='utf-8')
        match = re.search(r'var ACTIONS = \[(.*?)\];', js, re.S)
        self.assertEqual(tuple(re.findall(r"'([a-z_]+)'", match.group(1))), tuple(ns.ACTIONS))
        handlers = ns.default_handlers(FakeServer(FakeStore()), sleep=lambda s: None)
        self.assertEqual(tuple(handlers), ns.ACTIONS)

    def test_no_chat_creating_action(self):
        for action in ns.ACTIONS:
            self.assertNotRegex(action, r'create|new_|welcome|chat')
        js = JS_PATH.read_text(encoding='utf-8')
        shell = Path(ns.__file__).read_text(encoding='utf-8')
        for word in CREATE_WORDS:
            self.assertNotIn(word, js)
        self.assertNotIn("'/api/main/create'", shell)
        self.assertNotIn('/api/account/create-chat', shell)


class SettingsTests(unittest.TestCase):
    KEYS = ('coworker_rotate_mode', 'main_context_policy', 'coworker_delete_old_chat')

    def test_keys_allowed_and_defaults_shown(self):
        for key in self.KEYS:
            self.assertIn(key, ns.page_settings_keys())
        server = FakeServer(FakeStore(settings={'main_context_policy': 'auto'}))
        handlers = ns.default_handlers(server, sleep=lambda s: None)
        got = handlers['settings_get']({})
        self.assertEqual((got['coworker_rotate_mode'], got['main_context_policy'], got['coworker_delete_old_chat']),
                         ('new_welcome', 'auto', True))  # delete retired coworker chats: default on (store.py)

    def test_save_uses_the_existing_settings_route(self):
        server = FakeServer(FakeStore())
        handlers = ns.default_handlers(server, sleep=lambda s: None)
        handlers['settings_set']({'values': {'coworker_rotate_mode': 'same_thread', 'coworker_delete_old_chat': True}})
        self.assertEqual(server.calls, [('/api/settings', {'coworker_rotate_mode': 'same_thread',
                                                           'coworker_delete_old_chat': True})])

    def test_server_validates_the_same_values(self):
        source = SERVER_PATH.read_text(encoding='utf-8')
        for text in ('coworker_rotate_mode', 'new_welcome', 'same_thread', 'main_context_policy',
                     'coworker_delete_old_chat'):
            self.assertIn(text, source)


class OverlayStringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js_bytes = JS_PATH.read_bytes()
        cls.js = cls.js_bytes.decode('utf-8')

    def test_files_are_lf(self):
        for data in (self.js_bytes, Path(ns.__file__).read_bytes(), Path(__file__).read_bytes()):
            self.assertNotIn(b'\r', data)

    def test_attention_block(self):
        for text in ('function renderAttention(body, s)', 'renderAttention(body, s);', "'nt-attention'",
                     'Needs attention', 'Waiting for the Main: ', "text: 'Confirm'", "window.confirm('Confirm the run",
                     "call('run_confirm', { id: run.id })", "'Run confirmed'", 'Stalled: ', 'no progress for ',
                     'Stale context task: ', 's.awaiting_main', 's.stalled', 's.stale_context_tasks',
                     'if (!awaiting.length && !stalled.length && !stale.length) return;'):
            self.assertIn(text, self.js)
        live = self.js[self.js.index('function renderLive(body)'):]
        live = live[:live.index('// ---- Attention')]
        self.assertLess(live.index('renderAttention(body, s);'), live.index('if (!chats.length)'))

    def test_stalled_lines_are_report_only(self):
        block = self.js[self.js.index('function renderAttention'):self.js.index('function awaitingRow')]
        self.assertNotIn('button', block)
        self.assertNotIn('call(', block)

    def test_three_settings(self):
        match = re.search(r'var HARNESS_KEYS = \[(.*?)\];', self.js, re.S)
        keys = re.findall(r"'([a-z_]+)'", match.group(1))
        for key in SettingsTests.KEYS:
            self.assertIn(key, keys)
        for text in ("coworker_rotate_mode: ['new_welcome', 'same_thread']", "main_context_policy: ['hint', 'auto']",
                     'coworker_delete_old_chat: false', "el('select'", "call('settings_set', { values: values })"):
            self.assertIn(text, self.js)


if __name__ == '__main__':
    unittest.main()
