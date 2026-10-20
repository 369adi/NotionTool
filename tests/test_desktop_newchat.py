"""Desktop overlay: own New chat dialog, Chats section and gear menu (state/route contract)."""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'notiontool' / 'web' / 'desktop'
JS_PATH = ASSETS / 'overlay.js'
CSS_PATH = ASSETS / 'overlay.css'
NODE = 'C:/Program Files/nodejs/node.exe'

# Overlay action -> backend route (desktop_shell maps the action onto the POST route).
ACTIONS = {
    'app_chat_create': ('/api/app/chat/create', ('name', 'main', 'request_id')),
    'app_chat_rename': ('/api/app/chat/rename', ('thread_id', 'name')),
    'app_chat_promote': ('/api/app/chat/promote', ('main',)),
}

try:
    from notiontool import desktop_shell
except Exception:  # pragma: no cover - import problems are reported by the shell tests
    desktop_shell = None

WIRED = desktop_shell is not None and all(
    name in getattr(desktop_shell, 'DESK_ACTIONS', ()) for name in ACTIONS)


class DesktopNewChatStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js_bytes = JS_PATH.read_bytes()
        cls.css_bytes = CSS_PATH.read_bytes()
        cls.js = cls.js_bytes.decode('utf-8')
        cls.css = cls.css_bytes.decode('utf-8')
        start = cls.js.index('// ---- Named chats: own New chat dialog')
        cls.part = cls.js[start:cls.js.index('// ---- New chat control', start)]
        cls.helpers = cls.js[cls.js.index('// ---- Named chats (own New chat dialog'):
                             cls.js.index('window.__ntDesk.sidebar = function')]

    def section(self, start, end):
        return self.js[self.js.index(start):self.js.index(end)]

    def test_files_are_lf(self):
        for data in (self.js_bytes, self.css_bytes, Path(__file__).read_bytes()):
            self.assertNotIn(b'\r', data)
            self.assertFalse(data.startswith(b'\xef\xbb\xbf'))

    def test_uses_only_the_new_actions(self):
        for name in ACTIONS:
            self.assertIn("ctx.call('" + name + "'", self.part)
        calls = set(re.findall(r"ctx\.call\('([\w]+)'", self.part))
        self.assertEqual(calls, set(ACTIONS))
        self.assertIn('createArgs(values.name, values.main, requestId)', self.part)
        self.assertIn("{ name: cleanName(name), main: !!main, request_id: String(requestId || '') }", self.helpers)
        # Notion's own New chat routing stays untouched.
        self.assertIn("ctx.call('new_welcome_chat', {})", self.js)

    def test_request_id_once_per_dialog(self):
        create = self.section('function openCreate(opener)', '// -- opening a chat without a reload')
        self.assertEqual(create.count('newRequestId()'), 1)
        self.assertLess(create.index('var requestId = newRequestId();'), create.index('onSubmit'))
        dialog = self.section('function openDialog(opts)', 'function openCreate(opener)')
        self.assertNotIn('newRequestId', dialog)
        self.assertIn('if (self.busy) return;', dialog)  # double click while busy is ignored

    def test_never_reloads_sends_or_uses_network(self):
        for text in ('fetch(', 'XMLHttpRequest', 'WebSocket', '/api/', 'location.reload', 'location.href =',
                     'location.assign', 'location.replace', 'innerHTML', '.remove()', 'removeChild'):
            self.assertNotIn(text, self.part)
            self.assertNotIn(text, self.helpers)
        # Safety rule (test_desktop_no_lag): no history changes and no row clicks from desktop JS.
        self.assertNotIn('history.pushState', self.part)
        self.assertNotIn('.click()', self.part)

    def test_english_ui_texts(self):
        for text in ("'Chat name'", "label: 'Main'", "'Main chats can delegate work to coworkers'", "submit: 'Create'",
                     "text: 'Cancel'", "'Creating chat...'", "text: 'Chats'", "'No private chats'",
                     "'Remove Main rights?'", "'New chat: choose a name'", "'Chat settings'"):
            self.assertIn(text, self.part)
        for text in ("label: 'Rename'", "label: 'Make Main'", "label: 'Remove Main rights'"):
            self.assertIn(text, self.helpers)
        for text in re.findall(r"'([^'\\]*)'", self.part + self.helpers):
            self.assertNotRegex(text, r'[\u00c0-\u024f]', text)  # no German umlauts in shown texts

    def test_dialog_accessibility(self):
        dialog = self.section('function openDialog(opts)', 'function openCreate(opener)')
        for text in ("'aria-modal': 'true'", "'aria-labelledby': 'nt-desk-dlg-title'", "role: 'alert'",
                     "maxlength: CHAT_NAME_MAX", "ev.key === 'Escape'", "ev.key === 'Enter'", "ev.key === 'Tab'",
                     "'aria-disabled'", 'first.focus()'):
            self.assertIn(text, dialog)
        # Focus goes back to the opener when the dialog closes (closeDialog sits before openDialog).
        self.assertIn('d.opener.focus()', self.section('function closeDialog(', 'function openDialog(opts)'))
        self.assertIn("role: opts.danger ? 'alertdialog' : 'dialog'", dialog)
        menu = self.section('function openMenu(btn)', 'function promote(info, main)')
        for text in ("role: 'menu'", "role: 'menuitem'", 'ArrowDown', 'ArrowUp', 'Home', 'End', 'Escape'):
            self.assertIn(text, menu)

    def test_demote_is_confirmed(self):
        run = self.section('function runMenuItem(id, info, btn)', '// ---- New chat control')
        demote = run[run.index("id === 'demote'"):]
        self.assertIn('openDialog(', demote)
        self.assertIn('danger: true', demote)
        self.assertLess(demote.index('openDialog('), demote.index('promote(info, false)'))

    def test_wired_into_run_and_stop(self):
        run = self.section('function run()', 'function schedule')
        self.assertLess(run.index('applyRows(state, root);'), run.index('applyChats(state, root);'))
        self.assertLess(run.index('applyChats(state, root);'), run.index('applyCoworkers(state, root);'))
        self.assertIn('applyNewChatButton();', run)
        self.assertIn('applyPendingOpen();', run)
        stop = self.js[self.js.index('stop: function'):]
        self.assertIn('closeMenu(false);', stop)
        self.assertIn('closeDialog(false);', stop)
        self.assertIn('clearTimeout(pendingTimer)', stop)
        control = self.section('function newChatControl(target)', 'function block(ev)')
        self.assertEqual(control.count("closest('[data-nt-desk-own]')"), 2)  # our button is never intercepted

    def test_css(self):
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        self.assertNotRegex(css, r'font:[^;]*\binherit\s*;')
        for name in ('.nt-desk-scrim', '.nt-desk-dialog', '.nt-desk-input', '.nt-desk-field-msg', '.nt-desk-check',
                     '.nt-desk-banner', '.nt-desk-actions', '.nt-desk-btn-ghost', '.nt-desk-btn-primary', '.nt-desk-gear',
                     '.nt-desk-menu', '.nt-desk-menu-item', '.nt-desk-chats', '.nt-desk-chat', ':focus-visible',
                     'prefers-reduced-motion', 'z-index: 2147483000', 'width: 400px', 'max-width: calc(100vw - 32px)',
                     'top: 20vh', 'border-radius: 12px', 'min-width: 112px', 'height: 36px'):
            self.assertIn(name, css)
        new = css[css.index('.nt-desk-icon-btn'):]
        self.assertNotIn('linear-gradient', new)  # no glows or gradients in the new parts


class DesktopNewChatNodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = JS_PATH.read_text(encoding='utf-8')

    def section(self, start, end):
        return self.js[self.js.index(start):self.js.index(end)]

    def run_node(self, body):
        if not os.path.exists(NODE):
            self.skipTest('node is not installed at ' + NODE)
        consts = '\n'.join(line for line in self.js.split('\n')
                           if re.match(r'\s*var (THREAD_RE|THREAD_ID_RE|SPACE_PARAM_RE|FIBER_DEPTH) =', line))
        source = '\n'.join([
            'var window = { __ntDesk: {}, crypto: globalThis.crypto };',
            consts,
            self.section('function norm(text)', 'function message(err)'),
            self.section('function list(value)', 'function visibleLabel'),
            self.section('// ---- Named chats (own New chat dialog', 'window.__ntDesk.sidebar = function'),
            'var U = window.__ntDesk.newChatUtil;',
            body])
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
            handle.write(source)
        try:
            done = subprocess.run([NODE, handle.name], capture_output=True, text=True, timeout=40)
        finally:
            os.unlink(handle.name)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_request_ids_and_args(self):
        out = self.run_node(r"""
        var a = U.newRequestId(), b = U.newRequestId();
        var saved = window.crypto; window.crypto = undefined;
        var c = U.newRequestId(); window.crypto = saved;
        console.log(JSON.stringify({ a: a, b: b, c: c,
          args: U.createArgs('  Tax   return 2026 ', 1, a), plain: U.createArgs('x', 0, 'r1'),
          empty: U.nameError(U.cleanName('   ')), long: U.nameError(new Array(82).join('x')), ok: U.nameError('Chat'),
          rename: U.renameArgs({ thread_id: 'T1' }, ' New  name '),
          promote: U.promoteArgs({ chat_id: 'C1', thread_id: 'T1' }, true), demote: U.promoteArgs({ thread_id: 'T1' }, false)
        }));""")
        uuid = r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
        for name in ('a', 'b', 'c'):
            self.assertRegex(out[name], uuid)
        self.assertNotEqual(out['a'], out['b'])
        self.assertEqual(out['args'], {'name': 'Tax return 2026', 'main': True, 'request_id': out['a']})
        self.assertEqual(out['plain'], {'name': 'x', 'main': False, 'request_id': 'r1'})
        self.assertEqual(sorted(out['args']), sorted(ACTIONS['app_chat_create'][1]))
        self.assertEqual(out['empty'], 'Enter a chat name.')
        self.assertEqual(out['long'], 'Use at most 80 characters.')
        self.assertEqual(out['ok'], '')
        self.assertEqual(out['rename'], {'thread_id': 'T1', 'name': 'New name'})
        self.assertEqual(out['promote'], {'main': True, 'chat_id': 'C1', 'thread_id': 'T1'})
        self.assertEqual(out['demote'], {'main': False, 'thread_id': 'T1'})

    def test_private_rows_and_menu(self):
        out = self.run_node(r"""
        var D = '3fea1b13-e54d-805c-bc47-00a9af92c91f';
        var E = '3fda1b13-e54d-801c-b8c3-00a9ada51ec3';
        var fromState = U.privateChatRows({ private_chats: [
          { name: 'Tax return', thread_id: D, chat_id: 'c1', worker_id: 'w1', url: '/chat?t=' + D },
          { title: 'Dup', thread_id: D.replace(/-/g, '') }, { name: 'No thread' }] });
        var fromWorkers = U.privateChatRows({ workers: [
          { id: 'w2', purpose: 'private', label: 'Notes', thread_id: E }, { id: 'w3', purpose: 'worker', thread_id: D }] });
        console.log(JSON.stringify({ fromState: fromState, fromWorkers: fromWorkers, none: U.privateChatRows(null),
          priv: U.chatMenuItems({ kind: 'private' }),
          main: U.chatMenuItems(U.mainChatInfo({ title: 'Main 4', thread_id: E })),
          op: U.chatMenuItems(U.mainChatInfo({ title: 'Operator Main', thread_id: D, operator: true })) }));""")
        self.assertEqual(len(out['fromState']), 1)
        row = out['fromState'][0]
        self.assertEqual((row['name'], row['chat_id'], row['worker_id'], row['kind']), ('Tax return', 'c1', 'w1', 'private'))
        self.assertEqual(row['thread'], '3fea1b13e54d805cbc4700a9af92c91f')
        self.assertEqual([r['name'] for r in out['fromWorkers']], ['Notes'])  # coworkers never show as Chats
        self.assertEqual(out['none'], [])
        self.assertEqual([i['label'] for i in out['priv']], ['Rename', 'Make Main'])
        self.assertEqual([i['label'] for i in out['main']], ['Rename', 'Remove Main rights'])
        self.assertFalse(out['main'][1].get('disabled'))
        self.assertTrue(out['op'][1]['disabled'])


class DesktopNewChatBackendContractTests(unittest.TestCase):
    @unittest.skipUnless(WIRED, 'desktop_shell.DESK_ACTIONS has no app_chat_* actions yet '
                         '(desktop_shell.py/server.py are not on this task\'s file list)')
    def test_actions_post_to_new_routes(self):
        source = Path(desktop_shell.__file__).read_text(encoding='utf-8')
        for name, (route, _keys) in ACTIONS.items():
            self.assertIn(name, source)
            self.assertIn(route, source)
        self.assertIn('private_chats', source)


if __name__ == '__main__':
    unittest.main()
