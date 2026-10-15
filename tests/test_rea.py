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

    def test_settings_offer_only_the_rea_builtin(self):
        store = SimpleNamespace(settings=lambda: {}, all=lambda kind: [])
        servers = McpServers(SimpleNamespace(store=store)).public()
        self.assertEqual([s['id'] for s in servers], ['builtin_rea'])
        self.assertFalse(servers[0]['enabled'])


if __name__ == '__main__':
    unittest.main()
