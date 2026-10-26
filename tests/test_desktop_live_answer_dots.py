"""Live Answer (host action live_answer -> POST /api/send) and the one-dot-per-chat-row sidebar rules."""
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

from notiontool import desktop_shell

ASSETS = Path(__file__).resolve().parents[1] / 'notiontool' / 'web' / 'desktop'


class FakeStore:
    def __init__(self, data):
        self.data = data

    def get(self, kind, ident, default=None):
        return self.data.get(kind, {}).get(ident, default)

    def all(self, kind):
        return list(self.data.get(kind, {}).values())


class FakeServer:
    port = 8790
    token = 'tok'

    def __init__(self, data):
        self.store = FakeStore(data)
        self.sent = []

    def action(self, path, body):
        self.sent.append((path, body))
        return {'ok': True}


DATA = {
    'workers': {
        'w1': {'purpose': 'worker', 'task_id': 't1', 'label': 'Research'},
        'w2': {'purpose': 'worker', 'task_id': 't2', 'label': 'Lost'},
        'm1': {'purpose': 'main', 'task_id': 't1'},
    },
    'tasks': {'t1': {'run_id': 'r1'}, 't2': {'run_id': 'r2'}},
    'runs': {'r1': {'chat_id': 'chat-1'}, 'r2': {}},
}


class LiveAnswerHandlerTests(unittest.TestCase):
    def handlers(self):
        server = FakeServer(DATA)
        original = desktop_shell.notion_shell.default_handlers
        desktop_shell.notion_shell.default_handlers = lambda s: {}
        try:
            return server, desktop_shell.desktop_handlers(server, host_call=lambda *a, **k: {})
        finally:
            desktop_shell.notion_shell.default_handlers = original

    def test_registered_as_host_action(self):
        self.assertIn('live_answer', desktop_shell.DESK_ACTIONS)

    def test_reply_goes_through_api_send_to_the_owning_chat(self):
        server, h = self.handlers()
        out = h['live_answer']({'worker_id': 'w1', 'text': '  Use the v2 API ', 'request_id': 'live-abc'})
        self.assertTrue(out['sent'])
        self.assertEqual(server.sent[0][0], '/api/send')
        body = server.sent[0][1]
        self.assertEqual(body['chat_id'], 'chat-1')
        self.assertEqual(body['request_id'], 'live-abc')
        self.assertIn('Use the v2 API', body['text'])
        self.assertIn('Research', body['text'])

    def test_errors_are_plain_and_nothing_is_sent(self):
        server, h = self.handlers()
        for args, msg in (({'worker_id': 'w1', 'text': '  '}, 'Type a reply first'),
                          ({'worker_id': 'm1', 'text': 'x'}, 'Choose a coworker first'),
                          ({'worker_id': 'nope', 'text': 'x'}, 'Choose a coworker first'),
                          ({'worker_id': 'w2', 'text': 'x'}, 'no open request')):
            with self.assertRaises(ValueError) as ctx:
                h['live_answer'](args)
            self.assertIn(msg, str(ctx.exception))
        self.assertEqual(server.sent, [])


class SidebarDotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ASSETS / 'overlay.js').read_text(encoding='utf-8')
        cls.css = (ASSETS / 'overlay.css').read_text(encoding='utf-8')
        cls.theme = (ASSETS / 'theme.css').read_text(encoding='utf-8')

    def rule(self, selector):
        m = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(m, selector)
        return m.group(1)

    def test_one_dot_creator_per_row(self):
        self.assertEqual(self.js.count("'data-nt-desk': 'row-dot'"), 1)
        self.assertIn('function styleRowDot', self.js)
        self.assertIn('function trackUnread', self.js)
        for status in ('running', 'unread', 'waiting'):
            self.assertIn(status, self.js)

    def test_idle_hidden_running_pulses_unread_blue(self):
        self.assertIn('display: none', self.rule('.nt-desk-dot-idle'))
        self.assertIn('animation: nt-desk-pulse', self.rule('.nt-desk-dot-running'))
        self.assertIn('var(--nt-unread,', self.rule('.nt-desk-dot-unread'))
        self.assertIn('--nt-unread:', self.theme)
        self.assertIn('@keyframes nt-desk-pulse', self.css)
        reduced = self.css[self.css.index('prefers-reduced-motion'):]
        self.assertIn('.nt-desk-dot-running', reduced)

    def test_dot_rules_use_tokens_only(self):
        for sel in ('.nt-desk-dot', '.nt-desk-dot-running', '.nt-desk-dot-waiting', '.nt-desk-dot-unread'):
            body = self.rule(sel)
            self.assertNotRegex(body, r'#[0-9a-fA-F]{3,8}\b')
            self.assertNotIn('rgba(', body)

    def test_no_notion_class_names_in_dot_code(self):
        a = self.js.index('One status dot per chat row')
        block = self.js[a:a + 4000]
        self.assertNotIn('notion-', block)


class WaitingDotTests(unittest.TestCase):
    """Amber state: a coworker chat with an open question for the user."""
    NODE = 'C:/Program Files/nodejs/node.exe'

    @classmethod
    def setUpClass(cls):
        cls.js = (ASSETS / 'overlay.js').read_text(encoding='utf-8')
        cls.css = (ASSETS / 'overlay.css').read_text(encoding='utf-8')

    def test_row_order_running_waiting_unread_idle(self):
        a = self.js.index('function rowDotState')
        body = self.js[a:self.js.index('function rowRunning', a)]
        order = [body.index("'running'"), body.index("'waiting'"), body.index("'unread'"), body.index("'idle'")]
        self.assertEqual(order, sorted(order))
        self.assertIn('styleRowDot(row, rowDotState(state, key, unread));', self.js)
        self.assertIn('threadWaiting: threadWaiting', self.js)

    def test_amber_dot_uses_warning_token(self):
        m = re.search(r'\.nt-desk-dot-waiting\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(m)
        self.assertIn('var(--nt-warning,', m.group(1))

    def test_progress_fill_uses_accent_tokens(self):
        m = re.search(r'\.nt-desk-progress-fill\s*\{([^}]*)\}', self.css)
        self.assertIn('linear-gradient(90deg, var(--nt-accent, blueviolet), var(--nt-accent-2, cyan))', m.group(1))
        self.assertNotIn('#7c5cff', m.group(1))

    @unittest.skipUnless(os.path.exists(NODE), 'node not installed')
    def test_node_thread_waiting(self):
        a = self.js.index('function awaitingIds')
        b = self.js.index("// 'waiting' (the newest open run", a)
        script = (
            "function list(v){return Array.isArray(v)?v:[];}\n"
            "function threadKey(v){return String(v||'').replace(/-/g,'').toLowerCase();}\n"
            "var FINAL_RUN_STATES=['done','cancelled','canceled','failed','completed','aborted','error','archived'];\n"
            "var WAITING_RUN_STATES=['awaiting_main','awaiting_user','waiting_user','needs_user','needs_main'];\n"
            + self.js[a:b] +
            "var base={workers:[{id:'w1',thread_id:'aa-bb',task_id:'t1'}],tasks:[{id:'t1',run_id:'r1',worker_id:'w1'}],"
            "runs:[{id:'r1',state:'running'}]};\n"
            "var out={};\n"
            "out.quiet=threadWaiting(base,'aabb');\n"
            "out.other=threadWaiting(Object.assign({},base,{context_ask:{worker_id:'w1'}}),'ccdd');\n"
            "out.ask=threadWaiting(Object.assign({},base,{context_ask:{worker_id:'w1'}}),'aabb');\n"
            "out.state=threadWaiting(Object.assign({},base,{runs:[{id:'r1',state:'awaiting_main'}]}),'aabb');\n"
            "out.needs=threadWaiting(Object.assign({},base,{runs:[{id:'r1',state:'running',needs_main:['x']}]}),'aabb');\n"
            "out.list=threadWaiting(Object.assign({},base,{awaiting_main:['r1']}),'aabb');\n"
            "out.final=threadWaiting(Object.assign({},base,{runs:[{id:'r1',state:'done',needs_main:['x']}]}),'aabb');\n"
            "out.nokey=threadWaiting(base,'');\n"
            "console.log(JSON.stringify(out));\n"
        )
        result = subprocess.run([self.NODE, '-e', script], capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(out, {'quiet': False, 'other': False, 'ask': True, 'state': True, 'needs': True,
                               'list': True, 'final': False, 'nokey': False})


if __name__ == '__main__':
    unittest.main()
