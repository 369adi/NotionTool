"""Static checks for the desktop overlay part 'theme' (notiontool/web/desktop/theme.js/.css)."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from notiontool import desktop_shell

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / 'notiontool' / 'web' / 'desktop' / 'theme.js'
CSS = ROOT / 'notiontool' / 'web' / 'desktop' / 'theme.css'
NODE = r'C:\Program Files\nodejs\node.exe'

TOKENS = {
    '--nt-bg': '#0b0d12',
    '--nt-surface': '#12151d',
    '--nt-raised': '#181c26',
    '--nt-border': 'rgba(255, 255, 255, .07)',
    '--nt-text': '#e8eaf0',
    '--nt-muted': '#8a90a2',
    '--nt-accent-from': '#7c5cff',
    '--nt-accent-to': '#22d3ee',
    '--nt-success': '#22c55e',
    '--nt-warning': '#f59e0b',
    '--nt-danger': '#ef4444',
    '--nt-radius': '12px',
    '--nt-radius-sm': '8px',
    '--nt-transition': '150ms ease',
}


class ThemePartTests(unittest.TestCase):
    def setUp(self):
        self.js = JS.read_text(encoding='utf-8')
        self.css = CSS.read_text(encoding='utf-8')

    def test_registered_first(self):
        self.assertEqual(desktop_shell.PART_FILES[0], 'theme')
        self.assertEqual(desktop_shell.MOUNT_ORDER[0], 'theme')
        self.assertEqual(desktop_shell.part_files()[0][0], 'theme')

    def test_build_source_contains_theme_first(self):
        src = desktop_shell.build_source()
        self.assertIn('/* part: theme */', src)
        self.assertLess(src.index('/* part: theme */'), src.index('/* part: chrome */'))
        self.assertIn('--nt-bg', src)

    def test_css_tokens(self):
        root = self.css[self.css.index(':root'):]
        root = root[:root.index('}')]
        for name, value in TOKENS.items():
            self.assertIn('%s: %s;' % (name, value), root, name)
        self.assertIn('linear-gradient(135deg, #7c5cff 0%, #22d3ee 100%)', root)
        self.assertIn('Inter', root)
        self.assertIn('Segoe UI Variable', root)
        self.assertIn('system-ui', root)

    def test_css_restyles_surfaces(self):
        for marker in ('backdrop-filter', '::selection', '::-webkit-scrollbar', 'scrollbar-width: thin',
                       '.notion-sidebar', '[role="dialog"]', ':focus', ':hover', 'var(--nt-ring)'):
            self.assertIn(marker, self.css, marker)

    def test_round3_premium_surfaces(self):
        for marker in ('--c-bacPri: #0b0d12;', '--c-bacEle: #181c26;', '--c-popBac: #181c26;',
                       '--ca-sidIteSelBac:', '--ca-butHovBac:', '--ca-bacIntTra:',
                       '--nt-sidebar: #10131b;', '--nt-sidebar-border: rgba(255, 255, 255, .06);',
                       'radial-gradient(', '.layout-chat', '.notion-cursor-listener',
                       '[data-agent-chat-user-step-id]', 'a.notion-link-token',
                       '.notion-inline-code-container > span', 'blur(16px)'):
            self.assertIn(marker, self.css, marker)
        self.assertNotIn('#191919', self.css.split('/*')[0])

    def test_css_rules_are_scoped(self):
        for line in self.css.split('\n'):
            stripped = line.strip()
            if stripped.startswith('html') and '{' in stripped or stripped.startswith('html') and stripped.endswith(','):
                self.assertIn('html.nt-theme', stripped, stripped)

    def test_js_contract(self):
        self.assertIn('window.__ntDesk = window.__ntDesk || {}', self.js)
        self.assertIn('window.__ntDesk.theme = function (ctx)', self.js)
        self.assertIn("'nt-theme'", self.js)
        self.assertIn('stop: function', self.js)
        self.assertIn('classList.remove(ROOT_CLASS)', self.js)
        self.assertIn('MutationObserver', self.js)

    def test_js_safe(self):
        for bad in ('innerHTML', 'fetch(', 'XMLHttpRequest', 'WebSocket', 'setInterval', 'removeChild', '.remove()'):
            self.assertNotIn(bad, self.js, bad)

    def test_lf_utf8(self):
        for path in (JS, CSS, Path(__file__)):
            raw = path.read_bytes()
            raw.decode('utf-8')
            self.assertNotIn(b'\r', raw, path.name)

    @unittest.skipUnless(os.path.exists(NODE) or shutil.which('node'), 'node not installed')
    def test_node_check(self):
        node = NODE if os.path.exists(NODE) else shutil.which('node')
        out = subprocess.run([node, '--check', str(JS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
    def test_tool_groups_js(self):
        for marker in ('nt-desk-toolgroup-head', 'nt-desk-toolgroup-member', 'nt-desk-toolgroup-open',
                       'data-nt-tool-count', 'TOOL_MIN_GROUP', "addEventListener('click', onClick, true)",
                       "removeEventListener('click', onClick, true)", 'data-agent-chat-user-step-id'):
            self.assertIn(marker, self.js, marker)
        marks = self.js[self.js.index('var MARKS'):]
        marks = marks[:marks.index(';')]
        for name in ('GROUP_HEAD', 'GROUP_MEMBER', 'GROUP_OPEN'):
            self.assertIn(name, marks, name)

    def test_tool_groups_css(self):
        self.assertIn('.nt-desk-toolgroup-member:not(.nt-desk-toolgroup-open)', self.css)
        self.assertIn('.nt-desk-toolgroup-head:not(.nt-desk-toolgroup-open) > *', self.css)
        self.assertIn("attr(data-nt-tool-count) ' tool steps'", self.css)

    def test_calm_theme(self):
        # No glow on every button hover and no hover lift; the glow is only on the composer.
        self.assertNotIn('button:hover {\n  box-shadow', self.css)
        self.assertNotIn('translateY(-1px)', self.css)
        self.assertNotIn('var(--nt-accent-gradient) border-box', self.css)
        self.assertNotIn('inset: -10px', self.css)
        glow = self.css[self.css.index('[data-notion-chat-input-container]::before {'):]
        glow = glow[:glow.index('}')]
        self.assertIn('pointer-events: none;', glow)
        self.assertIn('inset: 4px 16px 2px 16px;', glow)

    @unittest.skipUnless(os.path.exists(NODE) or shutil.which('node'), 'node not installed')
    def test_tool_helpers_runtime(self):
        node = NODE if os.path.exists(NODE) else shutil.which('node')
        script = (
            'global.window = {}; require(process.argv[1]); var u = window.__ntDesk.themeUtil;'
            'console.log(JSON.stringify([u.isToolLabel("NNotionTool / nt_shell"), u.isToolLabel("Hello world"),'
            'u.toolRuns([true, true, true, false, true, true], 3), u.toolRuns([true, true, true, true], 3)]));'
        )
        out = subprocess.run([node, '-e', script, str(JS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), '[true,false,[[0,3]],[[0,4]]]')


if __name__ == '__main__':
    unittest.main()
