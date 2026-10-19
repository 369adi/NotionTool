import tempfile
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from notiontool.browser import Browser
from notiontool.server import Application
from notiontool.store import Store
from notiontool.notion import NotionAdapter
from notiontool.gateway import Gateway


class ManualOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.close)

    def test_default_login_preparation_does_not_connect_or_open_remote(self):
        app = Application.__new__(Application)
        app.store = self.store
        app.adapter = Mock()
        app.start_tunnel = Mock()
        self.assertEqual(app.prepare_account('a'), {'account_id': 'a'})
        app.adapter.ensure_main.assert_not_called()
        app.start_tunnel.assert_not_called()

    def test_manual_remote_start_publishes_listener_without_connecting_accounts(self):
        app = Application.__new__(Application)
        app.store = self.store
        app.ensure_mcp_listener = Mock()
        app.ensure_gateway = Mock()
        app.mcp_http = SimpleNamespace(server_port=9888)
        app.tailscale = Mock()
        app.tailscale.start.return_value = {'url': 'https://device.ts.net/mcp'}
        app.adapter = Mock()
        result = app.start_tunnel()
        app.ensure_gateway.assert_not_called()
        app.adapter.connect_bridge.assert_not_called()
        app.tailscale.start.assert_called_once_with(9888)
        self.assertEqual(result['connected_accounts'], 0)

    def test_finished_setup_does_not_connect_mcp(self):
        app = Application.__new__(Application)
        app.store = self.store
        app.adapter = Mock()
        app.start_tunnel = Mock()
        app.mcp_servers = Mock()
        self.store.put('accounts', {'id': 'a', 'state': 'ready'})
        app.finish_account_result({'account_id': 'a'})
        app.start_tunnel.assert_not_called()
        app.adapter.connect_bridge.assert_not_called()
        app.mcp_servers.connect.assert_not_called()

    def test_closed_browser_is_not_relaunched_by_request(self):
        browser = Browser(self.store)
        browser.process = Mock()
        browser.process.poll.return_value = 0
        browser.port = 9222
        with patch('notiontool.browser.subprocess.Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'Open it manually'):
                browser.ensure()
            launch.assert_not_called()

    def test_lost_reused_browser_is_not_relaunched_by_request(self):
        browser = Browser(self.store)
        browser.port = 9222
        browser.probe = Mock(return_value=None)
        with patch('notiontool.browser.subprocess.Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'Open it manually'):
                browser.ensure()
            launch.assert_not_called()

    def test_manual_defaults_disable_remote_automation_and_advertising(self):
        for key in ('automatic_setup', 'background_remote', 'bridge_auto',
                    'remote_mcp_enabled', 'advertise_project_tools'):
            self.assertFalse(self.store.settings()[key], key)

    def test_offline_gateway_does_not_advertise_cached_roblox_tools(self):
        gateway = Gateway(self.temp.name)
        gateway.accepted.add('auth')
        gateway.cache['tools/list'] = ([], json.dumps({'result': {'tools': [{'name': 'roblox_context'}]}}).encode())
        call = {'id': 7, 'method': 'tools/list'}
        status, _, body = gateway.backup(call, [call], 'auth')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['result']['tools'], [])

    def test_cleanup_removes_only_notiontool_default_preserving_roblox(self):
        self.store.put('accounts', {'id': 'a', 'bridge_integration': 'old', 'bridge_connected': True})
        modules = [{'pointer': {'table': 'workflow_module', 'id': ident}, 'defaultEnabled': True}
                   for ident in ('old', 'roblox')]
        client = Mock()
        client.info = {'space_id': 's', 'records': {'space_view': {
            'v': {'id': 'v', 'space_id': 's', 'settings': {'agent_chat_modules': modules}}}}}
        client.call.return_value = {'recordMap': {'workflow_module': {
            'old': {'data': {'name': 'NotionTool'}}, 'roblox': {'data': {'name': 'Roblox MCP'}}}}}
        adapter = NotionAdapter(self.store)
        adapter.client = Mock(return_value=client)
        adapter.transaction = Mock()
        result = adapter.disconnect_bridge('a')
        self.assertEqual(result['removed_defaults'], ['old'])
        operations = adapter.transaction.call_args.args[2]
        self.assertEqual(operations[0]['args']['agent_chat_modules'], [modules[1]])
        self.assertFalse(self.store.get('accounts', 'a')['bridge_connected'])
