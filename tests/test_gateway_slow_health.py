"""User rule 26.10.2026: when NotionTool hangs, the NotionTool MCP must not break with it.

Root cause seen live 18:02-18:10: RAM was full, our own gateway answered /__gateway slower than 2 s,
ensure_gateway() took it for a foreign program, returned 0 and start_tunnel() pointed Tailscale
at the core MCP port directly - so the MCP went down together with the app.
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from notiontool import gateway as gw
from notiontool.server import Application


class SlowGatewayHealth(unittest.TestCase):
    def make(self, health, pid_alive=True, json_port=gw.DEFAULT_PORT):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gw.write_json(gw.runtime_dir(tmp.name) / 'gateway.json', {'pid': 4242, 'port': json_port, 'version': gw.VERSION})
        app = SimpleNamespace(
            store=SimpleNamespace(settings=Mock(return_value={}), event=Mock()),
            data_dir=tmp.name,
            gateway_health=Mock(side_effect=health),
            port_busy=Mock(return_value=True),
            pid_alive=Mock(return_value=pid_alive),
        )
        return app

    def events(self, app):
        return [c.args[0] for c in app.store.event.call_args_list]

    @patch('notiontool.server.subprocess.Popen')
    @patch('notiontool.server.time.sleep')
    def test_slow_own_gateway_keeps_tailscale_on_gateway(self, _sleep, popen):
        app = self.make(lambda *a, **k: None)
        self.assertEqual(Application.ensure_gateway(app), gw.DEFAULT_PORT)
        self.assertIn('mcp_gateway_slow', self.events(app))
        self.assertNotIn('mcp_gateway_port_busy', self.events(app))
        popen.assert_not_called()

    @patch('notiontool.server.subprocess.Popen')
    @patch('notiontool.server.time.sleep')
    def test_health_answers_on_retry(self, _sleep, popen):
        answers = iter([None, None, {'service': 'notiontool-gateway', 'version': gw.VERSION}])
        app = self.make(lambda *a, **k: next(answers))
        self.assertEqual(Application.ensure_gateway(app), gw.DEFAULT_PORT)
        self.assertEqual(self.events(app), [])
        popen.assert_not_called()

    @patch('notiontool.server.subprocess.Popen')
    @patch('notiontool.server.time.sleep')
    def test_really_foreign_program_still_reported(self, _sleep, popen):
        app = self.make(lambda *a, **k: None, pid_alive=False)
        self.assertEqual(Application.ensure_gateway(app), 0)
        self.assertIn('mcp_gateway_port_busy', self.events(app))
        popen.assert_not_called()

    def test_pid_alive_is_safe(self):
        self.assertTrue(Application.pid_alive(os.getpid()))
        self.assertFalse(Application.pid_alive(0))
        self.assertFalse(Application.pid_alive('x'))


if __name__ == '__main__':
    unittest.main()
