"""Auth and bind checks for the host API, the MCP route, the gateway and the bridge.

No real app is started and no port is opened: every HTTP server class is replaced
by a fake that only records its bind address. All tokens are dummy values created
here. Line references point to notiontool/server.py, bridge.py and gateway.py as
read for Phase 10 batch 1.
"""
import email.message
import secrets
import tempfile
import types
import unittest
from unittest import mock

from notiontool import gateway as gateway_mod
from notiontool import server as server_mod
from notiontool.bridge import Bridge


class FakeHTTPServer:
    """Stands in for ThreadingHTTPServer; records the bind address, opens no socket."""
    created = []

    def __init__(self, address, handler, *args, **kwargs):
        self.server_address = address
        self.handler = handler
        self.server_port = address[1] or 40000
        self.daemon_threads = False
        FakeHTTPServer.created.append(self)

    def serve_forever(self):
        pass


class FakeStore:
    def __init__(self, settings=None):
        self._settings = dict(settings or {})
        self.events = []

    def settings(self):
        return dict(self._settings)

    def put(self, kind, value):
        if kind == 'settings':
            self._settings = dict(value)
        return value

    def all(self, kind):
        return []

    def event(self, kind, *args, **kwargs):
        self.events.append(kind)


def dummy(label):
    return 'dummy-' + label + '-' + secrets.token_hex(8)


def make_app(settings=None):
    app = server_mod.Application.__new__(server_mod.Application)
    app.store = FakeStore(settings)
    app.adapter = mock.Mock()
    app.engine = mock.Mock()
    app.bridge = mock.Mock()
    app.token = dummy('ui')
    app.bridge_token = dummy('bridge')
    app.host_api_key = dummy('host')
    app.mcp_http = None
    app.data_dir = tempfile.gettempdir()
    app.ensure_mcp_listener = mock.Mock(return_value=0)
    app.health = lambda: {'ok': True}
    return app


def launch(app):
    """Run Application.launch() with every server, thread and updater mocked."""
    FakeHTTPServer.created = []
    with mock.patch.object(server_mod, 'ThreadingHTTPServer', FakeHTTPServer), \
            mock.patch.object(server_mod, 'threading') as fake_threading, \
            mock.patch.object(server_mod, 'Updater'), \
            mock.patch('notiontool.turn_watch.TurnWatch'):
        app.launch(port=0)
        thread_targets = [c.kwargs.get('target') for c in fake_threading.Thread.call_args_list]
    return FakeHTTPServer.created[0], thread_targets


def call(app, handler_cls, method, path, headers=None, mcp_only=False):
    """Call Handler.handle_request directly with a mocked request; returns (status, body)."""
    handler = handler_cls.__new__(handler_cls)
    handler.path = path
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    handler.headers = message
    handler.server = types.SimpleNamespace(mcp_only=mcp_only)
    captured = []
    handler.response = lambda code, data, content_type='application/json': captured.append((code, data))
    handler.handle_request(method)
    return captured[0]


class HostApiAuthTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.http, _ = launch(self.app)
        self.handler = self.app.handler

    def test_api_without_bearer_is_rejected(self):
        # server.py:129-132
        status, _ = call(self.app, self.handler, 'GET', '/api/health')
        self.assertEqual(status, 401)

    def test_api_with_wrong_bearer_is_rejected(self):
        status, _ = call(self.app, self.handler, 'GET', '/api/health',
                         {'Authorization': 'Bearer ' + dummy('wrong')})
        self.assertEqual(status, 401)

    def test_api_with_correct_bearer_is_accepted(self):
        status, body = call(self.app, self.handler, 'GET', '/api/health',
                            {'Authorization': 'Bearer ' + self.app.host_api_key})
        self.assertEqual((status, body), (200, {'ok': True}))

    def test_api_rejects_the_worker_bridge_token(self):
        # The worker bridge token is only valid on /mcp (server.py:113), not on /api/ (server.py:129).
        status, _ = call(self.app, self.handler, 'GET', '/api/health',
                         {'Authorization': 'Bearer ' + self.app.bridge_token})
        self.assertEqual(status, 401)

    def test_ui_token_with_foreign_origin_is_rejected(self):
        # server.py:133-135
        status, _ = call(self.app, self.handler, 'GET', '/api/health',
                         {'X-NotionTool-Token': self.app.token, 'Origin': 'http://evil.example'})
        self.assertEqual(status, 403)

    def test_ui_token_with_own_origin_is_accepted(self):
        origin = 'http://127.0.0.1:%d' % self.app.port
        status, _ = call(self.app, self.handler, 'GET', '/api/health',
                         {'X-NotionTool-Token': self.app.token, 'Origin': origin})
        self.assertEqual(status, 200)

    def test_bearer_skips_the_origin_check(self):
        # Finding: with the Bearer key a foreign Origin is accepted (server.py:134 checks origin only without bearer).
        status, _ = call(self.app, self.handler, 'GET', '/api/health',
                         {'Authorization': 'Bearer ' + self.app.host_api_key, 'Origin': 'http://evil.example'})
        self.assertEqual(status, 200)


class McpRouteAuthTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        launch(self.app)
        self.handler = self.app.handler

    def test_mcp_without_bearer_is_rejected(self):
        # server.py:111-114
        status, _ = call(self.app, self.handler, 'POST', '/mcp', mcp_only=True)
        self.assertEqual(status, 401)
        self.app.bridge.rpc.assert_not_called()

    def test_mcp_with_wrong_bearer_is_rejected(self):
        status, _ = call(self.app, self.handler, 'POST', '/mcp',
                         {'Authorization': 'Bearer ' + dummy('wrong')}, mcp_only=True)
        self.assertEqual(status, 401)
        self.app.bridge.rpc.assert_not_called()

    def test_mcp_accepts_bridge_token_and_host_key(self):
        # GET after a passed auth check answers 405 (server.py:115-116), not 401.
        for token in (self.app.bridge_token, self.app.host_api_key):
            status, _ = call(self.app, self.handler, 'GET', '/mcp',
                             {'Authorization': 'Bearer ' + token}, mcp_only=True)
            self.assertEqual(status, 405)

    def test_mcp_only_listener_hides_the_host_api(self):
        # server.py:108-109: the public MCP listener serves only /mcp, even with a valid key.
        status, _ = call(self.app, self.handler, 'GET', '/api/health',
                         {'Authorization': 'Bearer ' + self.app.host_api_key}, mcp_only=True)
        self.assertEqual(status, 404)


class BindAddressTests(unittest.TestCase):
    def test_host_api_binds_loopback_by_default(self):
        # server.py:207
        http, _ = launch(make_app())
        self.assertEqual(http.server_address[0], '127.0.0.1')

    def test_bind_setting_is_ignored_without_network_opt_in(self):
        # server.py:208-210: host_api_bind counts only with host_api_allow_network.
        http, _ = launch(make_app({'host_api_bind': '0.0.0.0'}))
        self.assertEqual(http.server_address[0], '127.0.0.1')

    def test_network_opt_in_allows_a_non_loopback_bind(self):
        # Finding (documented, not a pass of 'never 0.0.0.0'): with host_api_allow_network the
        # host API binds the configured address, e.g. 0.0.0.0 (server.py:208-214).
        http, _ = launch(make_app({'host_api_allow_network': True, 'host_api_bind': '0.0.0.0'}))
        self.assertEqual(http.server_address[0], '0.0.0.0')

    def test_public_mcp_listener_binds_loopback(self):
        # server.py:775
        app = make_app({'public_mcp_port': 0})
        app.handler = object
        FakeHTTPServer.created = []
        with mock.patch.object(server_mod, 'ExclusiveHTTPServer', FakeHTTPServer), \
                mock.patch.object(server_mod, 'threading'), \
                mock.patch('notiontool.gateway.publish_core'):
            server_mod.Application.ensure_mcp_listener(app)
        self.assertEqual(FakeHTTPServer.created[0].server_address[0], '127.0.0.1')
        self.assertTrue(app.mcp_http.mcp_only)

    def test_gateway_binds_loopback(self):
        # gateway.py:216-225
        FakeHTTPServer.created = []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(gateway_mod, 'ThreadingHTTPServer', FakeHTTPServer):
            gateway_mod.Gateway(tmp, port=0).make_server()
        self.assertEqual(FakeHTTPServer.created[0].server_address[0], '127.0.0.1')

    @unittest.skip('tailscale_bridge.py:31 only targets http://127.0.0.1:<port>; the public side is the '
                   'Tailscale Funnel run through the tailscale CLI, which a unit test cannot exercise.')
    def test_tailscale_bridge_exposure(self):
        pass


class BridgeAssignmentTests(unittest.TestCase):
    def make_bridge(self, lease):
        bridge = Bridge.__new__(Bridge)
        tasks = [{'id': 't1', 'run_id': 'r1', 'lease': lease, 'state': 'running'}]
        bridge.store = types.SimpleNamespace(
            all=lambda kind: tasks if kind == 'tasks' else [],
            get=lambda kind, ident, default=None: {'id': 'r1', 'state': 'executing'})
        return bridge

    def test_wrong_or_empty_assignment_token_is_rejected(self):
        # bridge.py:204-211
        lease = dummy('lease')
        bridge = self.make_bridge(lease)
        for token in ('', dummy('wrong')):
            self.assertFalse(bridge.has_assignment(token))
            with self.assertRaises(ValueError):
                bridge.assignment(token)

    def test_correct_assignment_token_is_accepted(self):
        lease = dummy('lease')
        task, run = self.make_bridge(lease).assignment(lease)
        self.assertEqual((task['id'], run['id']), ('t1', 'r1'))

    @unittest.skip('gateway.py:198 checks client_address for /__gateway/stop inside a Handler class '
                   'created in Gateway.make_server; reaching it needs a live socket request.')
    def test_gateway_stop_needs_loopback_and_control(self):
        pass


if __name__ == '__main__':
    unittest.main()
