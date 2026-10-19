"""Static checks for the Notion page overlay (notiontool/web/notion_shell/overlay.js and overlay.css)."""
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'notiontool' / 'web' / 'notion_shell'
JS_PATH = ASSETS / 'overlay.js'
CSS_PATH = ASSETS / 'overlay.css'
SHARED_ACTIONS = ('state', 'runs', 'run_pause', 'run_cancel', 'run_retry', 'task_reconcile', 'settings_get',
                  'settings_set', 'mcp_list', 'mcp_refresh', 'run_confirm')
FORBIDDEN = ('Bearer', '127.0.0.1', 'fetch(', 'X-NotionTool-Token', 'token_v2')
NODE_CANDIDATES = ('C:/Program Files/nodejs/node.exe',)


def node_path():
    for candidate in NODE_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return shutil.which('node')


class OverlayFilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js_bytes = JS_PATH.read_bytes()
        cls.css_bytes = CSS_PATH.read_bytes()
        cls.js = cls.js_bytes.decode('utf-8')
        cls.css = cls.css_bytes.decode('utf-8')

    def test_files_are_lf_without_bom(self):
        for data in (self.js_bytes, self.css_bytes, Path(__file__).read_bytes()):
            self.assertNotIn(b'\r', data)
            self.assertFalse(data.startswith(b'\xef\xbb\xbf'))

    def test_contract_strings_exist(self):
        for text in ('window.ntHarness(JSON.stringify({ id: id, action: action, args: args || {} }))',
                     'window.__ntHarnessReply = function (id, reply)', 'window.__ntHarnessPush = function (state)',
                     'reply.ok', 'reply.result', 'reply.error'):
            self.assertIn(text, self.js)

    def test_own_install_guard(self):
        self.assertIn('if (window.__ntOverlayInstalled) return;', self.js)
        self.assertIn('window.__ntOverlayInstalled = true;', self.js)
        self.assertNotIn('nt-shell-style', self.js)
        self.assertNotIn('__ntShellInstalled', self.js)

    def test_action_list_matches_shared_contract(self):
        match = re.search(r'var ACTIONS = \[(.*?)\];', self.js, re.S)
        self.assertIsNotNone(match)
        self.assertEqual(tuple(re.findall(r"'([a-z_]+)'", match.group(1))), SHARED_ACTIONS)

    def test_actions_used_are_a_subset_of_the_whitelist(self):
        used = set(re.findall(r"call\('([a-z_]+)'", self.js))
        self.assertTrue(used <= set(SHARED_ACTIONS), used - set(SHARED_ACTIONS))
        for action in ('run_pause', 'run_cancel', 'run_retry', 'task_reconcile', 'settings_get', 'settings_set',
                       'mcp_list', 'mcp_refresh'):
            self.assertIn(action, used)

    def test_matches_python_side_when_available(self):
        try:
            from notiontool import notion_shell
        except Exception as exc:  # the Python part is optional for this module
            self.skipTest('notion_shell not importable: %s' % exc)
        self.assertEqual(tuple(notion_shell.ACTIONS), SHARED_ACTIONS)
        self.assertEqual(notion_shell.BINDING, 'ntHarness')
        for marker in getattr(notion_shell, 'INJECT_MARKERS', ()):
            self.assertNotIn(marker, self.js)
            self.assertNotIn(marker, self.css)

    def test_no_secrets_local_host_or_network_calls(self):
        for text in FORBIDDEN:
            self.assertNotIn(text, self.js)
            self.assertNotIn(text, self.css)
        for text in ('XMLHttpRequest', 'WebSocket', 'EventSource', 'sendBeacon', 'setInterval', 'localhost'):
            self.assertNotIn(text, self.js)

    def test_hiding_uses_observer_and_protect_list(self):
        self.assertIn('new MutationObserver(scheduleScan)', self.js)
        self.assertIn('[data-nt-hidden="1"] { display: none !important; }', self.css)
        for label in ("'home'", "'inbox'", "'search'", "'meetings'", "'agents'", "'new agent'", "'help'",
                      "'notifications'"):
            self.assertIn(label, self.js)
        for keep in ("'new chat'", "'model'", "'reasoning'", "'attach'", 'contenteditable', 'role="textbox"'):
            self.assertIn(keep, self.js)

    def test_new_chat_is_intercepted_in_capture_phase(self):
        self.assertIn("document.addEventListener('click', onClick, true);", self.js)
        self.assertIn('ev.preventDefault();', self.js)
        # Phase 8b: the button is blocked and explained; the page never asks the harness for a new chat.
        self.assertNotIn("call('new_welcome_chat'", self.js)
        self.assertNotIn('new_welcome_chat', self.js)
        self.assertIn('newChatRefused();', self.js)

    def test_panel_tabs_and_reconcile_ids(self):
        for tab in ("['live', 'Live']", "['runs', 'Runs']", "['harness', 'Harness']", "['mcp', 'MCP']",
                    "['settings', 'Settings']"):
            self.assertIn(tab, self.js)
        self.assertIn('run.reconcile_task_ids', self.js)
        self.assertIn("call('task_reconcile', { id: taskId })", self.js)
        self.assertIn('window_used', self.js)
        self.assertIn('requests_per_min', self.js)
        self.assertIn('nt-collapsed', self.css)

    def test_node_check(self):
        node = node_path()
        if not node:
            self.skipTest('node is not installed')
        done = subprocess.run([node, '--check', str(JS_PATH)], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == '__main__':
    unittest.main()
