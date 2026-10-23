"""I50 backend: GET /api/chat/ages, hard 23 h send block on /api/send, POST /api/chat/context_switch stub.

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
    def __init__(self, chats=(), workers=()):
        self.data = {'chats': {c['id']: dict(c) for c in chats}, 'workers': {w['id']: dict(w) for w in workers}}
        self._settings = {}
        self.events = []

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
        self.assertEqual(set(data), {'c_young', 'c_old', 'c_plain', 'c_unknown'})
        for entry in data.values():
            self.assertEqual(set(entry), {'age_s', 'created_at'})
        # The Notion thread of the Main worker counts first, then the chat record.
        self.assertAlmostEqual(data['c_young']['age_s'], HOUR, delta=5)
        self.assertAlmostEqual(data['c_old']['age_s'], 24 * HOUR, delta=5)
        self.assertAlmostEqual(data['c_plain']['age_s'], 2 * HOUR, delta=5)
        self.assertEqual(data['c_unknown'], {'age_s': None, 'created_at': None})
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


class ContextSwitchStubTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app(FakeStore(chats=[dict(id='c1', main_worker='', created=time.time())]))

    def test_stub_answers_ok(self):
        status, data = call(self.app, 'POST', '/api/chat/context_switch', {'chat_id': 'c1'})
        self.assertEqual((status, data), (200, {'ok': True, 'chat_id': 'c1'}))
        self.app.engine.submit.assert_not_called()

    def test_stub_needs_a_chat_id(self):
        status, _ = call(self.app, 'POST', '/api/chat/context_switch', {})
        self.assertEqual(status, 400)


if __name__ == '__main__':
    unittest.main()
