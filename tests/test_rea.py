import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from notiontool.mcp_servers import McpServers
from notiontool.rea import COMMAND, Rea


class ReaTests(unittest.TestCase):
    def client(self):
        client = Rea()
        client._ensure = Mock()
        client.schemas = {'list_targets': {'name': 'list_targets'}}
        client._request = Mock(return_value={'content': []})
        return client

    def test_known_tool_forwards_exact_arguments(self):
        client = self.client()
        client.call('list_targets', {'url': 'https://example.test'})
        client._request.assert_called_once_with(
            'tools/call', {'name': 'list_targets', 'arguments': {'url': 'https://example.test'}})

    def test_unknown_tool_is_not_forwarded(self):
        client = self.client()
        with self.assertRaisesRegex(ValueError, 'rea_tools'):
            client.call('unknown', {})
        client._request.assert_not_called()

    def test_timeout_closes_only_owned_process_without_resending(self):
        client = Rea(timeout=0.01)
        process = Mock()
        process.poll.return_value = None
        client.process = process
        with self.assertRaisesRegex(RuntimeError, 'not repeated'):
            client._request('tools/call', {'name': 'list_targets', 'arguments': {}})
        self.assertEqual(process.stdin.write.call_count, 1)
        process.terminate.assert_called_once()
        self.assertIsNone(client.process)

    def test_launch_uses_the_pinned_npx_command(self):
        launched = []

        def launch(argv, **kw):
            launched.append(argv)
            raise OSError('not started in tests')
        with self.assertRaises(OSError):
            Rea(launch=launch).tools()
        self.assertEqual(launched, [COMMAND])
        self.assertIn('rea-agents@6.5.0', COMMAND)

    def test_settings_offer_the_local_builtins_disabled_by_default(self):
        store = SimpleNamespace(settings=lambda: {}, all=lambda kind: [])
        servers = McpServers(SimpleNamespace(store=store)).public()
        self.assertEqual([s['id'] for s in servers], ['builtin_rea', 'builtin_roblox', 'builtin_blender', 'builtin_web', 'builtin_codebase_memory'])
        self.assertFalse(any(s['enabled'] for s in servers))
        self.assertEqual([s['toggle'] for s in servers], ['rea_enabled', 'roblox_enabled', 'blender_enabled', 'web_enabled', 'codebase_memory_enabled'])

    def test_bridge_tools_follow_the_toggles(self):
        from notiontool.bridge import Bridge
        settings = {}
        bridge = Bridge(SimpleNamespace(settings=lambda: settings), None)
        self.assertNotIn('roblox_tools', bridge.tools())
        settings.update(roblox_enabled=True, blender_enabled=True)
        names = bridge.tools()
        for name in ('roblox_tools', 'roblox_call', 'blender_tools', 'blender_call'):
            self.assertIn(name, names)
        self.assertNotIn('rea_tools', names)
        settings['roblox_enabled'] = False
        self.assertNotIn('roblox_call', bridge.tools())

    def test_refresh_restarts_enabled_servers_and_reports_tools(self):
        from notiontool.bridge import Bridge, OPERATOR_NAMES
        self.assertIn('nt_local_mcp', OPERATOR_NAMES)
        self.assertIn('nt_local_mcp_call', OPERATOR_NAMES)
        bridge = Bridge(SimpleNamespace(settings=lambda: {'blender_enabled': True}), None)
        fake = Mock()
        fake.tools.return_value = [{'name': 'get_scene_info', 'description': 'Scene'}]
        bridge.close_local = Mock()
        bridge.local_client = Mock(return_value=fake)
        result = {e['server']: e for e in bridge.local_refresh()}
        bridge.close_local.assert_called_once_with(['blender'])
        self.assertEqual(result['blender']['count'], 1)
        self.assertFalse(result['roblox']['enabled'])

    def test_enabled_rea_entrypoints_are_discoverable_without_starting_process(self):
        from notiontool.bridge import Bridge
        settings = {'rea_enabled': True}
        bridge = Bridge(SimpleNamespace(settings=lambda: settings), None)
        bridge.local_client = Mock()
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        names = {t['name'] for t in bridge.rpc(request)['result']['tools']}
        self.assertIn('rea_tools', names)
        self.assertIn('rea_call', names)
        bridge.local_client.assert_not_called()
        settings['rea_enabled'] = False
        names = {t['name'] for t in bridge.rpc(request)['result']['tools']}
        self.assertNotIn('rea_tools', names)

    def test_rea_schema_discovery_does_not_require_worker_assignment(self):
        from notiontool.bridge import Bridge
        bridge = Bridge(SimpleNamespace(settings=lambda: {'rea_enabled': True}), None)
        client = Mock()
        client.tools.return_value = [{'name': 'inspect_web_page'}]
        bridge.local_client = Mock(return_value=client)
        result = bridge.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'rea_tools', 'arguments': {}}})
        self.assertFalse(result['result'].get('isError'))
        self.assertIn('inspect_web_page', result['result']['content'][0]['text'])

    def test_direct_rea_execution_preserves_owner_permission_requirement(self):
        from notiontool.bridge import Bridge
        bridge = Bridge(SimpleNamespace(settings=lambda: {'rea_enabled': True}), None)
        bridge.local_client = Mock()
        result = bridge.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'rea_call', 'arguments': {'name': 'some_tool'}}})
        self.assertTrue(result['result']['isError'])
        bridge.local_client.assert_not_called()


class WebSearchServerTests(unittest.TestCase):
    def bridge(self, settings):
        from notiontool.bridge import Bridge
        return Bridge(SimpleNamespace(settings=lambda: settings), None)

    def test_web_is_off_by_default(self):
        from notiontool import local_mcp
        server = local_mcp.BY_KEY['web']
        self.assertEqual(server['setting'], 'web_enabled')
        self.assertFalse(local_mcp.enabled({}, server))
        self.assertIn('web_enabled', local_mcp.SETTINGS)
        bridge = self.bridge({})
        for role in (None, 'researcher', 'implementer'):
            names = bridge.tools(role)
            self.assertNotIn('web_tools', names)
            self.assertNotIn('web_call', names)

    def test_web_is_offered_only_to_research_roles_when_on(self):
        bridge = self.bridge({'web_enabled': True})
        for role in ('researcher',):
            names = bridge.tools(role)
            self.assertIn('web_tools', names)
            self.assertIn('web_call', names)
        for role in ('implementer', 'reviewer', 'planner', 'architect', 'quality_planner'):
            names = bridge.tools(role)
            self.assertNotIn('web_tools', names, role)
            self.assertNotIn('web_call', names, role)
        self.assertIn('web_tools', bridge.tools())  # owner / operator view is unchanged
        self.assertNotIn('blender_tools', bridge.tools('researcher'))  # other servers stay toggle-only

    def test_servers_without_roles_stay_offered_to_every_role(self):
        bridge = self.bridge({'blender_enabled': True})
        self.assertIn('blender_call', bridge.tools('implementer'))
        self.assertIn('blender_call', bridge.tools('researcher'))

    def test_missing_package_reports_not_installed_and_does_not_fail(self):
        from unittest.mock import patch
        from notiontool import local_mcp
        with patch.object(local_mcp, 'web_search_path', return_value=''):
            entry = next(s for s in local_mcp.public({'web_enabled': True}) if s['id'] == 'builtin_web')
            self.assertFalse(entry['installed'])
            self.assertTrue(entry['enabled'])
            self.assertIn('Not installed', entry['hint'])
            self.assertEqual(local_mcp.BY_KEY['web']['command'](), ['duckduckgo-mcp-server'])
            bridge = self.bridge({'web_enabled': True})
            bridge.close_local = Mock()
            bridge.local_client = Mock(side_effect=OSError('not found'))
            result = {e['server']: e for e in bridge.local_refresh('web')}
            self.assertFalse(result['web']['ok'])
            self.assertIn('not found', result['web']['error'])

    def test_installed_check_that_raises_is_reported_as_not_installed(self):
        from unittest.mock import patch
        from notiontool import local_mcp
        with patch.object(local_mcp, 'web_search_path', side_effect=RuntimeError('boom')):
            entry = next(s for s in local_mcp.public({}) if s['id'] == 'builtin_web')
        self.assertFalse(entry['installed'])
        self.assertFalse(entry['enabled'])

    def test_installed_package_is_reported_ready(self):
        from unittest.mock import patch
        from notiontool import local_mcp
        with patch.object(local_mcp, 'web_search_path', return_value='C:/tools/duckduckgo-mcp-server.exe'):
            entry = next(s for s in local_mcp.public({}) if s['id'] == 'builtin_web')
            self.assertTrue(entry['installed'])
            self.assertEqual(local_mcp.BY_KEY['web']['command'](), ['C:/tools/duckduckgo-mcp-server.exe'])


if __name__ == '__main__':
    unittest.main()
