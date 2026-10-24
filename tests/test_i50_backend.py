"""I50 backend: GET /api/chat/ages, hard 23 h send block on /api/send, POST /api/chat/context_switch (real switch).

No real app, port or Notion call: the HTTP handler is called directly (same pattern as test_auth_routes.py)
with a fake store and a mocked engine.
"""
import email.message
import io
import json
import secrets
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from notiontool import server as server_mod

HOUR = 3600


class FakeHTTPServer:
    def __init__(self, address, handler, *args, **kwargs):
        self.server_address = address
        self.handler = handler
        self.server_port = address[1] or 40000
        self.daemon_threads = False

    def serve_forever(self):
        pass


class FakeStore:
    def __init__(self, chats=(), workers=(), accounts=(), tasks=()):
        self.data = {'chats': {c['id']: dict(c) for c in chats}, 'workers': {w['id']: dict(w) for w in workers},
                     'accounts': {a['id']: dict(a) for a in accounts}, 'tasks': {t['id']: dict(t) for t in tasks}}
        self._settings = {}
        self.events = []
        self.lock = threading.RLock()

    def batch(self, puts, deletes=()):
        for kind, value in puts:
            self.put(kind, value)
        for kind, ident in deletes:
            self.data.get(kind, {}).pop(ident, None)

    def settings(self):
        return dict(self._settings)

    def get(self, kind, ident, default=None):
        return self.data.get(kind, {}).get(ident, default)

    def all(self, kind):
        return list(self.data.get(kind, {}).values())

    def put(self, kind, value):
        if kind == 'settings':
            self._settings = dict(value)
        else:
            self.data.setdefault(kind, {})[value['id']] = value
        return value

    def event(self, kind, *args, **kwargs):
        self.events.append(kind)


class FakeAdapter:
    """Notion adapter for the real context switch: a new Welcome chat that Notion lists (or not)."""
    NEW = '3fea1b13e54d805cbc4700a9af92c91f'

    def __init__(self, listed=True):
        self.listed = listed
        self.renamed = []

    def client(self, account_id):
        return types.SimpleNamespace(info={'space_id': 'space1'}, account_id=account_id)

    def new_id(self, space_id, prefix):
        return self.NEW

    def create_welcome(self, client, tid, model):
        return self.listed, True

    def rename_thread(self, client, tid, title):
        self.renamed.append((tid, title))


def make_app(store):
    app = server_mod.Application.__new__(server_mod.Application)
    app.store = store
    app.adapter = mock.Mock()
    app.engine = mock.Mock()
    app.engine.lock = threading.RLock()
    app.engine.submit = mock.Mock(return_value={'id': 'r1', 'state': 'approach'})
    app.bridge = mock.Mock()
    app.token = 'dummy-ui-' + secrets.token_hex(8)
    app.bridge_token = 'dummy-bridge-' + secrets.token_hex(8)
    app.host_api_key = 'dummy-host-' + secrets.token_hex(8)
    app.mcp_http = None
    app.data_dir = tempfile.gettempdir()
    app.ensure_mcp_listener = mock.Mock(return_value=0)
    app.health = lambda: {'ok': True}
    with mock.patch.object(server_mod, 'ThreadingHTTPServer', FakeHTTPServer), \
            mock.patch.object(server_mod, 'threading'), \
            mock.patch.object(server_mod, 'Updater'), \
            mock.patch('notiontool.turn_watch.TurnWatch'):
        app.launch(port=0)
    return app


def call(app, method, path, body=None):
    """Call Handler.handle_request directly; returns (status, payload)."""
    handler_cls = app.handler
    handler = handler_cls.__new__(handler_cls)
    handler.path = path
    raw = json.dumps(body).encode() if body is not None else b''
    message = email.message.Message()
    message['Authorization'] = 'Bearer ' + app.host_api_key
    message['Content-Length'] = str(len(raw))
    handler.headers = message
    handler.rfile = io.BytesIO(raw)
    handler.server = types.SimpleNamespace(mcp_only=False)
    captured = []
    handler.response = lambda code, data, content_type='application/json': captured.append((code, data))
    handler.handle_request(method)
    return captured[0]


class ChatAgesTests(unittest.TestCase):
    def setUp(self):
        now = time.time()
        self.store = FakeStore(
            chats=[dict(id='c_young', main_worker='w_young', created=now - 60),
                   dict(id='c_old', main_worker='w_old', created=now - 60),
                   dict(id='c_plain', main_worker='', created=now - 2 * HOUR),
                   dict(id='c_unknown', main_worker='w_missing')],
            workers=[dict(id='w_young', created_at=now - 1 * HOUR),
                     dict(id='w_old', created_at=(now - 24 * HOUR) * 1000)])  # Notion ms stamp
        self.app = make_app(self.store)

    def test_ages_for_every_known_chat(self):
        status, data = call(self.app, 'GET', '/api/chat/ages')
        self.assertEqual(status, 200)
        self.assertEqual((data['warn_h'], data['limit_h']), (20, 23))
        rows = {row['id']: row for row in data['chats']}
        self.assertEqual(set(rows), {'c_young', 'c_old', 'c_plain', 'c_unknown'})
        for row in rows.values():
            self.assertEqual(set(row), {'id', 'worker_id', 'thread_id', 'title', 'purpose', 'created_at', 'age_h', 'blocked'})
        # The Notion thread of the Main worker counts first, then the chat record.
        self.assertAlmostEqual(rows['c_young']['age_h'], 1, delta=0.01)
        self.assertAlmostEqual(rows['c_old']['age_h'], 24, delta=0.01)
        self.assertAlmostEqual(rows['c_plain']['age_h'], 2, delta=0.01)
        self.assertEqual((rows['c_old']['blocked'], rows['c_young']['blocked']), (True, False))
        self.assertEqual((rows['c_unknown']['age_h'], rows['c_unknown']['blocked']), (None, False))
        json.dumps(data)  # plain JSON

    def test_age_is_seconds_since_created(self):
        entry = server_mod.Application.chat_age(self.app, dict(created=1000.0), now=1000.0 + 90)
        self.assertEqual(entry, {'age_s': 90, 'created_at': 1000.0})


class SendBlockTests(unittest.TestCase):
    def setUp(self):
        now = time.time()
        self.store = FakeStore(
            chats=[dict(id='c_young', main_worker='w_young', created=now),
                   dict(id='c_old', main_worker='w_old', created=now),
                   dict(id='c_edge', main_worker='', created=now - 23 * HOUR - 1),
                   dict(id='c_unknown', main_worker='')],
            workers=[dict(id='w_young', created_at=now - 22 * HOUR),
                     dict(id='w_old', created_at=now - 30 * HOUR)])
        self.app = make_app(self.store)

    def test_send_to_old_chat_is_400_and_never_reaches_the_engine(self):
        status, data = call(self.app, 'POST', '/api/send', {'chat_id': 'c_old', 'text': 'hello'})
        self.assertEqual(status, 400)
        self.assertIn('chat_too_old', data['error'])
        self.assertIn('23 h', data['error'])
        self.app.engine.submit.assert_not_called()

    def test_send_at_23_hours_is_blocked(self):
        status, data = call(self.app, 'POST', '/api/send', {'chat_id': 'c_edge', 'text': 'hello'})
        self.assertEqual(status, 400)
        self.assertIn('chat_too_old', data['error'])
        self.app.engine.submit.assert_not_called()

    def test_send_to_young_chat_goes_to_the_engine(self):
        status, data = call(self.app, 'POST', '/api/send', {'chat_id': 'c_young', 'text': 'hello', 'request_id': 'q1'})
        self.assertEqual(status, 200)
        self.assertEqual(data['id'], 'r1')
        self.app.engine.submit.assert_called_once_with('c_young', 'hello', 'q1')

    def test_unknown_age_is_not_blocked(self):
        status, _ = call(self.app, 'POST', '/api/send', {'chat_id': 'c_unknown', 'text': 'hello'})
        self.assertEqual(status, 200)

    def test_action_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.app.action('/api/send', {'chat_id': 'c_old', 'text': 'hello'})


OLD_THREAD = 'aaaabbbbccccddddeeeeffff00001111'


def switch_store(tasks=()):
    now = time.time()
    return FakeStore(
        chats=[dict(id='c1', main_worker='w1', title='Main chat', created=now - 30 * HOUR),
               dict(id='c_plain', main_worker='', created=now)],
        workers=[dict(id='w1', account_id='a1', thread_id=OLD_THREAD, purpose='main', label='Main chat',
                      created_at=now - 30 * HOUR),
                 dict(id='w2', account_id='a1', thread_id='2222333344445555', purpose='worker', label='Cw',
                      created_at=now - 24 * HOUR)],
        accounts=[dict(id='a1', model='m1')], tasks=tasks)


class ContextSwitchTests(unittest.TestCase):
    def setUp(self):
        self.store = switch_store()
        self.app = make_app(self.store)
        self.app.adapter = FakeAdapter()

    def test_switch_by_worker_id_moves_the_chat_to_a_fresh_one(self):
        status, data = call(self.app, 'POST', '/api/chat/context_switch', {'worker_id': 'w1'})
        self.assertEqual(status, 200, data)
        self.assertEqual((data['worker_id'], data['thread_id'], data['old_thread_id']), ('w1', FakeAdapter.NEW, OLD_THREAD))
        worker = self.store.get('workers', 'w1')
        self.assertEqual(worker['thread_id'], FakeAdapter.NEW)
        self.assertIn(OLD_THREAD, worker['retired_threads'])
        self.assertEqual(worker['label'], 'Main chat')  # a Main keeps its name
        self.assertIn('chat_context_switch', self.store.events)
        row = [r for r in self.app.chat_ages()['chats'] if r['id'] == 'c1'][0]
        self.assertEqual(row['thread_id'], FakeAdapter.NEW)
        self.assertLess(row['age_h'], 0.01)  # the age restarts in the fresh chat
        self.assertFalse(row['blocked'])
        # The switch sends 'weiter' at once, so the handoff reaches the new chat and it continues.
        self.app.engine.submit.assert_called_once()
        self.assertEqual(self.app.engine.submit.call_args[0][0], 'c1')
        self.assertTrue(data['continued'])
        self.assertIn('Continuation checkpoint', worker['handoff'])  # real state, not only the one-line note

    def test_switch_by_chat_id_uses_the_main_worker(self):
        status, data = call(self.app, 'POST', '/api/chat/context_switch', {'chat_id': 'c1'})
        self.assertEqual(status, 200, data)
        self.assertEqual((data['worker_id'], data['chat_id'], data['thread_id']), ('w1', 'c1', FakeAdapter.NEW))

    def test_switch_of_a_coworker(self):
        status, data = call(self.app, 'POST', '/api/chat/context_switch', {'worker_id': 'w2'})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.store.get('workers', 'w2')['thread_id'], FakeAdapter.NEW)

    def test_main_switch_renames_old_and_new_chat(self):
        status, data = call(self.app, 'POST', '/api/chat/context_switch', {'worker_id': 'w1'})
        self.assertEqual(status, 200, data)
        self.assertTrue(data['renamed'])
        self.assertIn((OLD_THREAD, 'Main chat (archived)'), self.app.adapter.renamed)
        self.assertIn((FakeAdapter.NEW, 'Main chat'), self.app.adapter.renamed)

    def test_manual_switch_never_deletes_the_old_chat(self):
        deleted = []
        self.app.adapter.delete_thread = lambda client, tid: deleted.append(tid)
        for worker_id in ('w1', 'w2'):
            status, data = call(self.app, 'POST', '/api/chat/context_switch', {'worker_id': worker_id})
            self.assertEqual(status, 200, data)
        self.assertEqual(deleted, [])  # rename instead of delete: the old chats stay in the sidebar archive
        titles = [title for tid, title in self.app.adapter.renamed]
        self.assertIn('Main chat (archived)', titles)
        self.assertIn('Cowork (retired)', titles)  # a Coworker keeps the normal retire title
        self.assertIsNone(data['renamed'])  # a Coworker's new chat is named by the pool, not here

    def test_failed_rename_of_the_new_chat_keeps_the_switch(self):
        adapter = FakeAdapter()
        def rename(client, tid, title):
            if tid == FakeAdapter.NEW:
                raise RuntimeError('rename endpoint down')
            adapter.renamed.append((tid, title))
        adapter.rename_thread = rename
        self.app.adapter = adapter
        status, data = call(self.app, 'POST', '/api/chat/context_switch', {'worker_id': 'w1'})
        self.assertEqual(status, 200, data)
        self.assertFalse(data['renamed'])
        self.assertEqual(self.store.get('workers', 'w1')['thread_id'], FakeAdapter.NEW)
        self.assertIn('chat_context_switch_rename_failed', self.store.events)

    def test_manual_switch_adapter_hides_delete_only(self):
        adapter = FakeAdapter()
        adapter.delete_thread = lambda client, tid: None
        view = server_mod.ManualSwitchAdapter(adapter, 'X (archived)')
        self.assertIsNone(getattr(view, 'delete_thread', None))
        self.assertEqual(view.new_id('s', '00a9'), FakeAdapter.NEW)
        view.rename_thread(None, 'old', 'Cowork (retired)')
        self.assertEqual(adapter.renamed, [('old', 'X (archived)')])
        self.assertEqual(server_mod.archived_title(''), 'Main (archived)')

    def test_bad_requests_are_400(self):
        for body in ({}, {'chat_id': 'c_plain'}, {'chat_id': 'nope'}, {'worker_id': 'nope'}):
            status, data = call(self.app, 'POST', '/api/chat/context_switch', body)
            self.assertEqual(status, 400, body)
        self.assertEqual(self.store.get('workers', 'w1')['thread_id'], OLD_THREAD)

    def test_not_listed_new_chat_keeps_the_old_one(self):
        self.app.adapter = FakeAdapter(listed=False)
        status, data = call(self.app, 'POST', '/api/chat/context_switch', {'worker_id': 'w1'})
        self.assertEqual(status, 400)
        self.assertIn('Context switch failed', data['error'])
        self.assertEqual(self.store.get('workers', 'w1')['thread_id'], OLD_THREAD)

    def test_busy_chat_is_not_switched(self):
        self.store = switch_store(tasks=[dict(id='t1', worker_id='w1', state='running')])
        self.app = make_app(self.store)
        self.app.adapter = FakeAdapter()
        status, data = call(self.app, 'POST', '/api/chat/context_switch', {'worker_id': 'w1'})
        self.assertEqual(status, 400)
        self.assertIn('working on a task', data['error'])
        self.assertEqual(self.store.get('workers', 'w1')['thread_id'], OLD_THREAD)


if __name__ == '__main__':
    unittest.main()
