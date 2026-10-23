"""I50 desktop UI: chat age per sidebar chat (GET /api/chat/ages, on load + every 60 s), orange >= warn_h,
red >= limit_h, and a Context Switch button per chat (POST /api/chat/context_switch, then a sidebar reload)."""
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path

from notiontool import desktop_shell

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'notiontool' / 'web' / 'desktop'
JS_PATH = ASSETS / 'overlay.js'
CSS_PATH = ASSETS / 'overlay.css'
NODE = 'C:/Program Files/nodejs/node.exe'

FAKE_DOM = r"""
function El(tag) { this.tagName = String(tag).toUpperCase(); this.attrs = {}; this.children = []; this.parentNode = null; this._text = ''; this.style = {}; }
El.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
var window = { __ntDesk: {} };
var document = { createElement: function (t) { return new El(t); }, querySelector: function () { return null; }, querySelectorAll: function () { return []; } };
var location = { href: '' };
"""


class ChatAgeStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = JS_PATH.read_text(encoding='utf-8')
        cls.css = CSS_PATH.read_text(encoding='utf-8')

    def section(self, start, end):
        a = self.js.index(start)
        return self.js[a:self.js.index(end, a)]

    def test_age_loading_logic(self):
        self.assertIn("CHAT_AGES_ROUTE = '/api/chat/ages'", self.js)
        self.assertIn('CHAT_AGE_REFRESH_MS = 60000', self.js)
        self.assertIn("ctx.call('chat_ages', {})", self.js)
        mount = self.section('    run();\n    refreshChatAges();', 'return {')
        # every 60 s on the existing watchdog timer: still exactly one setInterval in the file
        self.assertIn('Date.now() - agesFetchedAt >= CHAT_AGE_REFRESH_MS) refreshChatAges();', mount)
        self.assertEqual(self.js.count('setInterval('), 1)
        self.assertIn('applyChatAges(sidebarRoot())', self.section('function run()', 'function schedule()'))

    def test_highlight_and_switch_button(self):
        part = self.section('// ---- I50 chat age: age chip', '// ---- Named chats: own New chat dialog')
        self.assertIn("'nt-desk-chip nt-desk-chat-age nt-desk-chat-age-' + level", part)
        self.assertIn("'data-nt-age-level'", part)
        self.assertIn("text: 'Context Switch'", part)
        self.assertIn("ctx.call('chat_age_switch', chatSwitchArgs(entry))", part)
        self.assertIn("CHAT_SWITCH_ROUTE = '/api/chat/context_switch'", self.js)
        self.assertIn('reloadAfterSwitch(entry, result)', part)
        self.assertIn('ev.stopPropagation()', part)
        for rule in ('.nt-desk-chat-age-ok {', '.nt-desk-chat-age-warn {', '.nt-desk-chat-age-locked {',
                     '.nt-desk-age-switch {', '[data-nt-age-level="warn"] {', '[data-nt-age-level="locked"] {'):
            self.assertIn(rule, self.css)
        for token in ('--nt-age-warn-fg: #fb923c', '--nt-age-warn-bg: rgba(249, 115, 22, .16)',
                      '--nt-age-lock-bg: #dc2626', '--nt-age-lock-soft: rgba(220, 38, 38, .12)'):
            self.assertIn(token, self.css)

    def test_desktop_safety_rules_still_hold(self):
        # No network, reload or navigation from desktop JS (test_desktop_no_lag / test_desktop_sidebar).
        for banned in ('fetch(', 'XMLHttpRequest', 'location.reload', '127.0.0.1', 'innerHTML', '.remove()'):
            self.assertNotIn(banned, self.js, banned)
        self.assertIsNone(re.search(r'\bfetch\s*\(', self.js))
        self.assertEqual(self.js.count('removeChild'), 1)
        for data in (JS_PATH.read_bytes(), CSS_PATH.read_bytes(), Path(__file__).read_bytes()):
            self.assertNotIn(b'\r', data)

    def run_node(self, body):
        if not os.path.exists(NODE):
            self.skipTest('node is not installed at ' + NODE)
        source = '\n'.join([FAKE_DOM, self.js, 'var A = window.__ntDesk.chatAgeUtil;', body])
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
            handle.write(source)
        try:
            done = subprocess.run([NODE, handle.name], capture_output=True, text=True, timeout=40)
        finally:
            os.unlink(handle.name)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return json.loads(done.stdout)

    def test_labels_levels_and_map(self):
        out = self.run_node(r"""
        var m = A.chatAgeMap({ warn_h: 20, limit_h: 23, chats: [
          { worker_id: 'w1', thread_id: '3FEA-1B13', title: 'Main', purpose: 'main', age_h: 21.5, blocked: false },
          { worker_id: 'w2', thread_id: 'aabb', age_h: null, created_at: null },
          { thread_id: '' }] }, 1000);
        console.log(JSON.stringify({
          text: [A.chatAgeText(0.4), A.chatAgeText(2.2), A.chatAgeText(21.9), A.chatAgeText(23), A.chatAgeText(40), A.chatAgeText(null)],
          level: [A.chatAgeLevel(5, false, 20, 23), A.chatAgeLevel(20, false, 20, 23), A.chatAgeLevel(22.9, false, 20, 23),
                  A.chatAgeLevel(23, false, 20, 23), A.chatAgeLevel(3, true, 20, 23), A.chatAgeLevel(null, false, 20, 23),
                  A.chatAgeLevel(21, false), A.chatAgeLevel(19, false, 18, 22)],
          keys: Object.keys(m.chats), first: m.chats['3fea1b13'], limits: [m.warn_h, m.limit_h],
          now: A.chatAgeNow(m.chats['3fea1b13'], 1000 + 3600000, 1000),
          args: A.chatSwitchArgs(m.chats['3fea1b13']), route: A.route, switchRoute: A.switchRoute, ms: A.refreshMs,
          title: A.chatAgeTitle('locked', '23h+', 23)
        }));
        """)
        self.assertEqual(out['text'], ['24m', '2h', '21h', '23h+', '23h+', ''])
        self.assertEqual(out['level'], ['ok', 'warn', 'warn', 'locked', 'locked', 'unknown', 'warn', 'warn'])
        self.assertEqual(out['keys'], ['3fea1b13', 'aabb'])
        self.assertEqual(out['first']['worker_id'], 'w1')
        self.assertEqual(out['limits'], [20, 23])
        self.assertAlmostEqual(out['now'], 22.5)
        self.assertEqual(out['args'], {'worker_id': 'w1'})
        self.assertEqual((out['route'], out['switchRoute'], out['ms']), ('/api/chat/ages', '/api/chat/context_switch', 60000))
        self.assertIn('Send locked', out['title'])


class FakeStore:
    def get(self, kind, ident, default=None):
        return default

    def all(self, kind):
        return []


class FakeServer:
    store = FakeStore()
    port = 8790
    token = 'tok'


class ChatAgeHandlerTests(unittest.TestCase):
    def handlers(self, host_call):
        original = desktop_shell.notion_shell.default_handlers
        desktop_shell.notion_shell.default_handlers = lambda s: {}
        try:
            return desktop_shell.desktop_handlers(FakeServer(), host_call=host_call)
        finally:
            desktop_shell.notion_shell.default_handlers = original

    def test_actions_are_registered(self):
        for name in ('chat_ages', 'chat_age_switch', 'chat_context_switch'):
            self.assertIn(name, desktop_shell.DESK_ACTIONS)

    def test_chat_ages_get(self):
        calls = []

        def host(method, path, body=None):
            calls.append((method, path, body))
            return {'warn_h': 20, 'limit_h': 23, 'chats': [
                {'worker_id': 'w1', 'thread_id': '3FEA-1b13', 'title': 'Cw1', 'purpose': 'worker',
                 'created_at': 1, 'age_h': 21.2, 'blocked': False}, 'junk', {'thread_id': ''}]}
        result = self.handlers(host)['chat_ages']({})
        self.assertEqual(calls, [('GET', '/api/chat/ages', None)])
        self.assertTrue(result['api_ok'])
        self.assertEqual((result['warn_h'], result['limit_h']), (20.0, 23.0))
        self.assertEqual(len(result['chats']), 1)
        self.assertEqual(result['chats'][0]['thread_id'], '3fea1b13')
        self.assertEqual(result['chats'][0]['age_h'], 21.2)

    def test_chat_ages_missing_route_is_no_error(self):
        def host(method, path, body=None):
            raise desktop_shell.HostApiError(404, 'Not found')
        result = self.handlers(host)['chat_ages']({})
        self.assertFalse(result['api_ok'])
        self.assertEqual(result['chats'], [])
        self.assertIn('not available yet', result['error'])

    def test_context_switch_post(self):
        calls = []

        def host(method, path, body=None):
            calls.append((method, path, body))
            return {'worker_id': 'w1', 'thread_id': '3fea1b13e54d805cbc4700a9af92c91f'}
        result = self.handlers(host)['chat_age_switch']({'worker_id': 'w1'})
        self.assertEqual(calls, [('POST', '/api/chat/context_switch', {'worker_id': 'w1'})])
        self.assertEqual(result['thread_id'], '3fea1b13e54d805cbc4700a9af92c91f')
        self.assertTrue(result['url'])

    def test_context_switch_errors(self):
        def missing(method, path, body=None):
            raise desktop_shell.HostApiError(404, 'Not found')
        h = self.handlers(missing)
        with self.assertRaisesRegex(ValueError, 'not available yet'):
            h['chat_age_switch']({'worker_id': 'w1'})
        with self.assertRaisesRegex(ValueError, 'worker id'):
            h['chat_age_switch']({})

    def test_host_api_uses_ui_token_and_maps_http_errors(self):
        seen = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"chats": []}'

        class Opener:
            def __init__(self, fail=None):
                self.fail = fail

            def open(self, req, timeout=None):
                seen.append(req)
                if self.fail:
                    raise urllib.error.HTTPError(req.full_url, self.fail, 'x', {}, io.BytesIO(b'{"error": "Not found"}'))
                return Resp()

        call = desktop_shell.host_api(FakeServer(), opener=Opener())
        self.assertEqual(call('GET', '/api/chat/ages'), {'chats': []})
        self.assertEqual(seen[0].full_url, 'http://127.0.0.1:8790/api/chat/ages')
        self.assertEqual(seen[0].get_header('X-notiontool-token'), 'tok')
        call = desktop_shell.host_api(FakeServer(), opener=Opener(404))
        with self.assertRaises(desktop_shell.HostApiError) as ctx:
            call('POST', '/api/chat/context_switch', {'worker_id': 'w1'})
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(json.loads(seen[1].data.decode('utf-8')), {'worker_id': 'w1'})


class BackendContractTests(unittest.TestCase):
    """End to end: the real backend replies (Application.chat_ages and the real context switch through the
    HTTP handler) run through the shell actions chat_ages and chat_age_switch."""

    def setUp(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_i50_backend as backend
        self.backend = backend
        self.store = backend.switch_store()
        self.app = backend.make_app(self.store)
        self.app.adapter = backend.FakeAdapter()

    def host(self, method, path, body=None):
        status, payload = self.backend.call(self.app, method, path, body)
        if status >= 400:
            raise desktop_shell.HostApiError(status, str((payload or {}).get('error') or ''))
        return payload

    def handlers(self):
        original = desktop_shell.notion_shell.default_handlers
        desktop_shell.notion_shell.default_handlers = lambda s: {}
        try:
            return desktop_shell.desktop_handlers(FakeServer(), host_call=self.host)
        finally:
            desktop_shell.notion_shell.default_handlers = original

    def test_real_ages_and_real_switch_through_the_shell(self):
        h = self.handlers()
        ages = h['chat_ages']({})
        self.assertTrue(ages['api_ok'], ages)
        self.assertEqual((ages['warn_h'], ages['limit_h']), (20.0, 23.0))
        self.assertGreaterEqual(len(ages['chats']), 1)
        main = [row for row in ages['chats'] if row['thread_id'] == self.backend.OLD_THREAD]
        self.assertEqual(len(main), 1)
        self.assertGreaterEqual(main[0]['age_h'], 23)
        result = h['chat_age_switch']({'worker_id': main[0]['worker_id']})
        self.assertEqual(result['worker_id'], 'w1')
        self.assertTrue(result['thread_id'])
        self.assertNotEqual(result['thread_id'], self.backend.OLD_THREAD)
        self.assertTrue(result['url'])
        after = {row['thread_id']: row for row in h['chat_ages']({})['chats']}
        self.assertIn(result['thread_id'], after)
        self.assertNotIn(self.backend.OLD_THREAD, after)
        self.assertLess(after[result['thread_id']]['age_h'], 1)

    def test_failed_backend_switch_is_an_error_in_the_shell(self):
        self.app.adapter = self.backend.FakeAdapter(listed=False)
        with self.assertRaisesRegex(ValueError, 'Context switch failed'):
            self.handlers()['chat_age_switch']({'worker_id': 'w1'})


if __name__ == '__main__':
    unittest.main()
