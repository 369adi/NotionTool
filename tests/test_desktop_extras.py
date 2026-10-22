"""Static checks for the desktop overlay part 'extras' (notiontool/web/desktop/extras.js/.css)."""
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

from notiontool import desktop_shell

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / 'notiontool' / 'web' / 'desktop' / 'extras.js'
CSS = ROOT / 'notiontool' / 'web' / 'desktop' / 'extras.css'
NODE = r'C:\Program Files\nodejs\node.exe'


class DesktopExtrasTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js_bytes = JS.read_bytes()
        cls.css_bytes = CSS.read_bytes()
        cls.js = cls.js_bytes.decode('utf-8')
        cls.css = cls.css_bytes.decode('utf-8')

    def test_registers_part(self):
        self.assertIn('window.__ntDesk = window.__ntDesk || {}', self.js)
        self.assertIn('.extras =', self.js)
        self.assertIn('stop:', self.js)
        self.assertTrue(self.js.rstrip().endswith('})();'))

    def test_markers(self):
        for marker in ('main_switch_start', 'context_ask', '#nt-live', 'nt-desk-off', 'ntDeskOff', 'not connected',
                       'nt-desk-brand-dot', 'nt-desk-banner', 'sessionStorage', 'localStorage', 'onState',
                       'Switch now', 'Later', 'Switch started', 'Switching Main chat: ',
                       'This Main chat is getting long (', 'Switch to a fresh Main chat?',
                       'NotionTool lost connection to the harness - retrying',
                       'NotionTool layer off - press Ctrl+Alt+Shift+N again to turn it on',
                       'brief_requested', 'brief_ready', 'prompt_sent', 'attention'):
            self.assertIn(marker, self.js)
        self.assertIn('15000', self.js)
        self.assertIn('20000', self.js)
        self.assertIn('30 * 60 * 1000', self.js)

    def test_no_html_parsing(self):
        for bad in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval('):
            self.assertNotIn(bad, self.js)

    def test_own_classes_only(self):
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        classes = re.findall(r'\.(-?[A-Za-z_][\w-]*)', css)
        self.assertTrue(classes)
        for name in classes:
            self.assertTrue(name.startswith('nt-desk-'), name)
        for match in re.findall(r"cls: '([^']+)'", self.js):
            for name in match.split():
                self.assertTrue(name.startswith('nt-desk-'), name)

    def test_kill_switch_css(self):
        self.assertIn('html.nt-desk-off .nt-desk-hidden { display: revert !important; }', self.css)
        self.assertIn('html.nt-desk-off .nt-desk-brand-dot, html.nt-desk-off .nt-desk-banner { display: none !important; }',
                      self.css)
        self.assertIn('prefers-color-scheme: dark', self.css)

    # ---- emergency stop ------------------------------------------------------------------
    def test_kill_switch_hotkey_kept(self):
        self.assertIn('event.ctrlKey && event.altKey && event.shiftKey', self.js)
        self.assertIn("key !== 'n' && event.code !== 'KeyN'", self.js)
        self.assertIn("window.addEventListener('keydown', onKey, true)", self.js)
        self.assertIn("window.removeEventListener('keydown', onKey, true)", self.js)
        self.assertIn('Emergency stop: turn NotionTool layer off', self.js)

    # ---- toasts ----------------------------------------------------------------------------
    def test_toast_cards(self):
        for marker in ('ctx.toast = fancyToast', 'ctx.toast = originalToast', 'nt-desk-tcard',
                       'nt-desk-tcard-bar', 'nt-desk-tcard-icon', 'nt-desk-tcard-close', 'nt-desk-tcard-out',
                       "'Dismiss notification'", 'TOAST_MS = 6000', 'TOAST_ERROR_MS = 6000', 'mouseenter',
                       'friendly(text)'):
            self.assertIn(marker, self.js, marker)
        for kind in ('info', 'success', 'warning', 'error'):
            self.assertIn('.nt-desk-tcard-' + kind, self.css)

    def test_toast_css(self):
        for marker in ('html .nt-desk-toasts > .nt-desk-toast', 'backdrop-filter', '@keyframes nt-desk-toast-in',
                       '@keyframes nt-desk-toast-out', 'translateX(28px)', 'prefers-reduced-motion'):
            self.assertIn(marker, self.css, marker)
        for token, fallback in (('--nt-raised', '#181c26'), ('--nt-success', '#22c55e'),
                                ('--nt-warning', '#f59e0b'), ('--nt-danger', '#ef4444'),
                                ('--nt-accent-gradient', '#7c5cff 0%, #22d3ee 100%')):
            self.assertRegex(self.css, r'var\(%s, [^;]*%s' % (re.escape(token), re.escape(fallback)))

    def test_friendly_status_map(self):
        for code, text in (('brief_requested', 'Preparing handoff brief'), ('brief_ready', 'Handoff brief ready'),
                           ('prompt_sent', 'Handing over to the new Main chat')):
            self.assertIn("%s: '%s'" % (code, text), self.js)
        self.assertIn("'Switching Main chat: ' + statusText(running.state)", self.js)

    # ---- command palette ---------------------------------------------------------------
    def test_palette_markers(self):
        for marker in ("key === 'k' || event.code === 'KeyK'", 'isLinkContext', 'nt-desk-pal-backdrop',
                       'nt-desk-pal-input', 'nt-desk-pal-item', "role: 'listbox'", "role: 'option'",
                       'aria-activedescendant', "'ArrowDown'", "'ArrowUp'", "'Enter'", "'Escape'",
                       'fuzzyScore', 'Open Live', 'Open NotionTool settings', 'New Welcome chat',
                       'Switch to a fresh Main chat', 'Turn NotionTool layer on', 'No matching commands'):
            self.assertIn(marker, self.js, marker)
        for marker in ('.nt-desk-pal ', '.nt-desk-pal-active', 'z-index: 2147483100'):
            self.assertIn(marker, self.css, marker)

    def test_only_existing_actions(self):
        match = re.search(r'var PALETTE_ACTIONS = \[([^\]]*)\]', self.js)
        self.assertIsNotNone(match)
        allowed = re.findall(r"'([a-z_]+)'", match.group(1))
        self.assertTrue(allowed)
        for action in allowed:
            self.assertIn(action, desktop_shell.DESK_ACTIONS)
        called = set(re.findall(r"\bcall\('([a-z_]+)'", self.js))
        called |= set(re.findall(r"ctx\.call\('([a-z_]+)'", self.js))
        self.assertTrue(called)
        for action in called:
            self.assertIn(action, allowed, action)
            self.assertIn(action, desktop_shell.DESK_ACTIONS, action)
        self.assertIn("PALETTE_ACTIONS.indexOf(action) < 0", self.js)
        self.assertNotIn('ctx.call(action', self.js.replace('return ctx.call(action, args || {});', ''))

    def test_lf_utf8(self):
        for data in (self.js_bytes, self.css_bytes, Path(__file__).read_bytes()):
            self.assertNotIn(b'\r', data)
            data.decode('utf-8')

    @unittest.skipUnless(os.path.isfile(NODE), 'node not installed')
    def test_node_check(self):
        res = subprocess.run([NODE, '--check', str(JS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)

    @unittest.skipUnless(os.path.isfile(NODE), 'node not installed')
    def test_node_helpers(self):
        script = (
            "global.window = {}; require(%s); var u = window.__ntDesk.extrasUtil;"
            "console.log(JSON.stringify({"
            "f: u.friendly('Switching Main chat: brief_requested'),"
            "g: u.friendly('keep some_unknown_code'),"
            "s: u.statusText('done'),"
            "k: [u.toastKind('ok'), u.toastKind('error'), u.toastKind('warn'), u.toastKind()],"
            "exact: u.fuzzyScore('live', 'Open Live') > u.fuzzyScore('live', 'Refresh MCP tools'),"
            "fuzzy: u.fuzzyScore('nwc', 'New Welcome chat') > 0,"
            "miss: u.fuzzyScore('xyz', 'Open Live'),"
            "empty: u.fuzzyScore('', 'Open Live'),"
            "part: typeof window.__ntDesk.extras}))" % json.dumps(str(JS)))
        res = subprocess.run([NODE, '-e', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        got = json.loads(res.stdout.strip().splitlines()[-1])
        self.assertEqual(got['f'], 'Switching Main chat: Preparing handoff brief')
        self.assertEqual(got['g'], 'keep some_unknown_code')
        self.assertEqual(got['s'], 'Switch complete')
        self.assertEqual(got['k'], ['success', 'error', 'warning', 'info'])
        self.assertTrue(got['exact'])
        self.assertTrue(got['fuzzy'])
        self.assertEqual(got['miss'], 0)
        self.assertEqual(got['empty'], 1)
        self.assertEqual(got['part'], 'function')


    # ---- single Live entry point -----------------------------------------------------------
    def test_no_floating_live_bar(self):
        for bad in ('nt-desk-bar', 'dockBar', 'dockPosition', 'sidebarFooter', 'nt-desk-badge'):
            self.assertNotIn(bad, self.js, bad)
            self.assertNotIn(bad, self.css, bad)
        self.assertIn('[data-nt-desk="live-pill"] { display: none !important; }', self.css)
        self.assertIn("document.querySelector('[data-nt-desk=\"brand\"]')", self.js)
        self.assertIn('placeDot();', self.js)
        self.assertIn('window.__ntDesk.counts = counts', self.js)

    @unittest.skipUnless(os.path.isfile(NODE), 'node not installed')
    def test_node_counts(self):
        script = (
            "global.window = {}; require(%s); var c = window.__ntDesk.counts; var w = [];"
            "for (var i = 0; i < 10; i++) w.push({id: 'w' + i, purpose: 'worker', state: i ? 'idle' : 'busy'});"
            "w.push({id: 'm', purpose: 'main', state: 'busy'}, {id: 'o', purpose: 'operator'},"
            " {id: 'x', purpose: 'worker', enabled: false, state: 'busy'});"
            "var s = {workers: w, attention: [1, 2, 3, 4, 5], chats: Array(13).fill({role: 'coworker'}),"
            " runs: [{id: 'r0', state: 'done', tasks_total: 1, tasks_done: 1},"
            " {id: 'r1', state: 'executing', tasks_total: 3, tasks_done: 2}]};"
            "var a = c(s), e = c({});"
            "var p = c({runs: [{id: 'r', state: 'paused', tasks_total: 4, tasks_done: 1}]});"
            "var f = c({runs: [{id: 'r', state: 'failed', tasks_total: 2, tasks_done: 2}]});"
            "console.log(JSON.stringify({a: a, e: e, p: p.runText, f: f.runText,"
            " same: window.__ntDesk.extrasUtil.counts === c}))" % json.dumps(str(JS)))
        res = subprocess.run([NODE, '-e', script], capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        got = json.loads(res.stdout.strip().splitlines()[-1])
        a, e = got['a'], got['e']
        self.assertEqual((a['coworkers'], a['busy'], a['attention'], a['liveBadge']), (10, 1, 5, '5'))
        self.assertEqual(a['coworkersText'], 'Coworkers 1 of 10 busy')
        self.assertEqual(a['runText'], 'Run 2 of 3 steps')
        self.assertEqual(a['run']['percent'], 67)
        self.assertEqual((e['coworkers'], e['liveBadge'], e['coworkersText'], e['runText']),
                         (0, '', 'No coworkers', 'No run yet'))
        self.assertEqual(got['p'], 'Run paused \u00b7 1 of 4 steps')
        self.assertEqual(got['f'], 'Last run failed \u00b7 2 of 2 steps')
        self.assertTrue(got['same'])


if __name__ == '__main__':
    unittest.main()
