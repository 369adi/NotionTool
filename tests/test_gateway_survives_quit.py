"""User rule 25.10.2026: the MCP gateway (process 2) must outlive every end of the app, also a full quit.

Root cause seen live at 18:15:48: the window was closed after the app hung (desktop build 293 s),
main.py called stop_gateway(app) on the full quit and Notion lost the MCP together with the app.
"""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent


def calls_in(func_node, name):
    found = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            target = node.func
            ident = target.id if isinstance(target, ast.Name) else getattr(target, 'attr', '')
            if ident == name:
                found.append(node.lineno)
    return found


class GatewaySurvivesQuit(unittest.TestCase):
    def test_main_never_stops_gateway(self):
        tree = ast.parse((ROOT / 'main.py').read_text('utf-8'))
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        self.assertEqual(calls_in(main, 'stop_gateway'), [], 'main() must not stop process 2 on quit')

    def test_no_core_module_stops_gateway_on_close(self):
        hits = []
        for path in (ROOT / 'notiontool').glob('*.py'):
            text = path.read_text('utf-8', 'replace')
            if '/__gateway/stop' in text and path.name not in ('gateway.py', 'server.py'):
                hits.append(path.name)
        self.assertEqual(hits, [], 'only the gateway itself and the one-time version upgrade may stop it')

    def test_server_stops_gateway_only_for_version_upgrade(self):
        text = (ROOT / 'notiontool' / 'server.py').read_text('utf-8', 'replace')
        self.assertEqual(text.count('/__gateway/stop'), 1)
        start = text.index('def ensure_gateway')
        body = text[start:text.index('def start_tunnel', start)]
        self.assertIn('/__gateway/stop', body)
        self.assertIn("health.get('version')", body.replace('health.get("version")', "health.get('version')"))


if __name__ == '__main__':
    unittest.main()
