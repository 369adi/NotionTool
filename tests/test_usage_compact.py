"""GET /api/usage (by_task / by_role / by_model) and GET /api/run?compact=1.

No port is opened: Application.launch() runs with a fake HTTP server class and the Handler is called
directly (same way as tests/test_auth_routes.py). The store is a real temporary Store, the engine is a
fake that returns fixed tasks.
"""
import email.message
import json
import os
import secrets
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from notiontool import operator_main, operator_roles, server as server_mod, token_budget
from notiontool.store import Store

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000.0


class FakeHTTPServer:
    def __init__(self, address, handler, *args, **kwargs):
        self.server_address = address
        self.handler = handler
        self.server_port = address[1] or 40000
        self.daemon_threads = False

    def serve_forever(self):
        pass


class FakeEngine:
    """launch() starts the engine and its loops; a Mock absorbs that, tasks() returns the fixed list."""

    def __init__(self, tasks):
        self._tasks = tasks
        self._absorb = mock.Mock()

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return getattr(self._absorb, name)

    def tasks(self, run_id):
        return [dict(t) for t in self._tasks if t.get('run_id') == run_id]


def big_task(ident='t1'):
    return dict(
        id=ident, run_id='r1', title='Build the thing', role='executor', state='done', model='opus',
        worker_id='w1', review_state='passed', patch_state='merged', fix_of=None,
        prompt='P' * 20000, submitted_prompt='S' * 20000, result='Summary: all done.\n' + 'R' * 20000,
        partial='x' * 5000, raw_response='y' * 5000, step_id='step', lease=dict(owner='w1'),
        proof=dict(files=['a.py'] * 200), events=[dict(kind='step', message='e' * 200) for _ in range(100)])


def make_app(directory, tasks, settings=None):
    app = server_mod.Application.__new__(server_mod.Application)
    app.store = Store(directory)
    if settings:
        app.store.put('settings', dict(settings, id='app'))
    app.adapter = mock.Mock()
    app.engine = FakeEngine(tasks)
    app.bridge = mock.Mock()
    app.token = 'dummy-ui-' + secrets.token_hex(8)
    app.bridge_token = 'dummy-bridge-' + secrets.token_hex(8)
    app.host_api_key = 'dummy-host-' + secrets.token_hex(8)
    app.mcp_http = None
    app.data_dir = str(directory)
    app.ensure_mcp_listener = mock.Mock(return_value=0)
    app.health = lambda: {'ok': True}
    with mock.patch.object(server_mod, 'ThreadingHTTPServer', FakeHTTPServer), \
            mock.patch.object(server_mod, 'threading'), \
            mock.patch.object(server_mod, 'Updater'), \
            mock.patch('notiontool.turn_watch.TurnWatch'), \
            mock.patch.dict(os.environ):
        os.environ.pop('NOTIONTOOL_TEST_INSTANCE', None)  # the test-instance branch would start the engine
        os.environ.pop('NOTIONTOOL_FAKE_NOTION', None)
        app.launch(port=0)
    return app


def call(app, path):
    handler = app.handler.__new__(app.handler)
    handler.path = path
    message = email.message.Message()
    message['Authorization'] = 'Bearer ' + app.host_api_key
    handler.headers = message
    handler.server = types.SimpleNamespace(mcp_only=False)
    captured = []
    handler.response = lambda code, data, content_type='application/json': captured.append((code, data))
    handler.handle_request('GET')
    return captured[0]


class UsageAndCompactTests(unittest.TestCase):
    def setUp(self):
        token_budget.clear_hard_stop_cache()
        self.addCleanup(token_budget.clear_hard_stop_cache)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks = [big_task('t1'), big_task('t2')]

    def app(self, settings=None):
        app = make_app(Path(self.tmp.name) / ('d%d' % len(list(Path(self.tmp.name).iterdir()))), self.tasks, settings)
        app.store.put('runs', dict(id='r1', state='running', title='Run one', context=dict(big='c' * 5000)))
        self.addCleanup(app.store.db.close)
        return app

    # ---- /api/run ----
    def test_compact_keys_are_exactly_the_listed_ones(self):
        status, body = call(self.app(), '/api/run?id=r1&compact=1')
        self.assertEqual(status, 200)
        self.assertEqual(len(body['tasks']), 2)
        for task in body['tasks']:
            self.assertEqual(set(task), set(server_mod.COMPACT_TASK_KEYS))
        self.assertEqual(server_mod.COMPACT_TASK_KEYS, ('id', 'title', 'role', 'state', 'stage', 'review_state',
                                                         'patch_state', 'model', 'worker_id', 'summary'))
        first = body['tasks'][0]
        self.assertEqual((first['id'], first['role'], first['model'], first['worker_id']), ('t1', 'executor', 'opus', 'w1'))
        self.assertEqual(first['stage'], operator_roles.stage_of(self.tasks[0]))
        self.assertEqual(first['summary'], operator_main.plain_task_summary(self.tasks[0]))
        self.assertNotIn('context', body['run'])

    def test_compact_is_at_least_half_smaller(self):
        app = self.app()
        _, compact = call(app, '/api/run?id=r1&compact=1')
        _, full = call(app, '/api/run?id=r1')
        small, large = len(json.dumps(compact)), len(json.dumps(full))
        self.assertLess(small, large * 0.5)
        self.assertLess(small, 3000)
        for banned in ('P' * 50, 'R' * 50, 'e' * 50):
            self.assertNotIn(banned, json.dumps(compact))

    def test_normal_output_is_unchanged(self):
        app = self.app()
        run = app.store.get('runs', 'r1')
        expected = {'run': {k: v for k, v in run.items() if k != 'context'},
                    'tasks': [server_mod.Application.safe_task(t) for t in app.engine.tasks('r1')]}
        for path in ('/api/run?id=r1', '/api/run?id=r1&compact=0'):
            status, body = call(app, path)
            self.assertEqual(status, 200)
            self.assertEqual(body, expected)
        self.assertIn('proof', body['tasks'][0])
        self.assertIn('events', body['tasks'][0])

    def test_setting_off_gives_full_output(self):
        app = self.app({'api_run_compact': False})
        _, off = call(app, '/api/run?id=r1&compact=1')
        _, plain = call(app, '/api/run?id=r1')
        self.assertEqual(off, plain)
        self.assertIn('proof', off['tasks'][0])

    def test_setting_defaults_to_on(self):
        self.assertIs(self.app().store.settings().get('api_run_compact', True), True)

    def test_unknown_run_is_404(self):
        status, _ = call(self.app(), '/api/run?id=nope&compact=1')
        self.assertEqual(status, 404)

    # ---- /api/usage ----
    def test_usage_has_real_ledger_data_and_keeps_old_fields(self):
        app = self.app()
        token_budget.charge(app.store, dict(id='w1', purpose='worker'), 1000, 200, turn=True, now=NOW,
                            task_id='t1', role='executor', model='opus')
        token_budget.charge(app.store, dict(id='w1', purpose='worker'), 300, 0, turn=True, now=NOW + 5,
                            task_id='t2', role='reviewer', model='sonnet')
        status, body = call(app, '/api/usage')
        self.assertEqual(status, 200)
        from notiontool.backup import usage_report
        old = usage_report(app.store)
        for key, value in old.items():
            self.assertIn(key, body)
        for key in ('by_task', 'by_role', 'by_model'):
            self.assertTrue(body[key], key)
        self.assertEqual(body['by_task']['t1']['tokens'], 1200)
        self.assertEqual(body['by_task']['t2']['tokens'], 300)
        self.assertEqual(body['by_role']['executor']['tokens'], 1200)
        self.assertEqual(body['by_role']['reviewer']['tokens'], 300)
        self.assertEqual(body['by_model']['opus']['tokens'], 1200)
        self.assertEqual(body['by_model']['sonnet']['tokens'], 300)

    def test_usage_response_matches_usage_by_task(self):
        app = self.app()
        token_budget.charge(app.store, dict(id='w1', purpose='worker'), 50, 5, turn=True, now=NOW, task_id='t9',
                            role='executor', model='opus')
        ledger = token_budget.usage_by_task(app.store)
        body = server_mod.usage_response(app.store)
        for key in ('by_task', 'by_role', 'by_model'):
            self.assertEqual(body[key], ledger[key])

    def test_usage_without_ledger_data_has_empty_groups(self):
        status, body = call(self.app(), '/api/usage')
        self.assertEqual(status, 200)
        for key in ('by_task', 'by_role', 'by_model'):
            self.assertEqual(body[key], {})

    def test_imports_exist(self):
        self.assertTrue(callable(token_budget.usage_by_task))
        self.assertTrue(callable(operator_main.plain_task_summary))
        self.assertTrue(callable(operator_roles.stage_of))

    # ---- server.py line endings ----
    def test_server_py_is_pure_crlf(self):
        data = (ROOT / 'notiontool' / 'server.py').read_bytes()
        self.assertEqual(data.count(b'\r\n'), data.count(b'\n'))
        self.assertEqual(data.count(b'\r'), data.count(b'\r\n'))


if __name__ == '__main__':
    unittest.main()
