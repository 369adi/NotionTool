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

# Design tokens of the redesign (docs/plan/redesign/reference/HANDOVER.md, Design).
TOKENS = {
    '--nt-bg': '#09090b',
    '--nt-surface-1': '#0f0f13',
    '--nt-surface-2': '#16161d',
    '--nt-surface-3': '#1e1e27',
    '--nt-press': '#26262f',
    '--nt-border': '#26262f',
    '--nt-border-strong': '#34343f',
    '--nt-text': '#f4f4f5',
    '--nt-text-2': '#a1a1aa',
    '--nt-text-3': '#8a8a94',
    '--nt-placeholder': '#71717a',
    '--nt-accent': '#8b5cf6',
    '--nt-accent-hover': '#9d74f8',
    '--nt-accent-press': '#7c4deb',
    '--nt-accent-2': '#22d3ee',
    '--nt-accent-soft': 'rgba(139, 92, 246, .14)',
    '--nt-focus': '#a78bfa',
    '--nt-success': '#22c55e',
    '--nt-warning': '#f59e0b',
    '--nt-danger': '#ef4444',
    '--nt-danger-text': '#f87171',
    '--nt-radius-chip': '6px',
    '--nt-radius-sm': '8px',
    '--nt-radius': '12px',
    '--nt-radius-pill': '999px',
    '--nt-motion': '120ms ease',
    '--nt-blur': '12px',
    '--nt-rail-w': '84px',
}

# Colors of the first design; none of them may be left anywhere in theme.css.
OLD_COLORS = ('#7c5cff', '#0b0d12', '#181c26', '#161a24', '#232838', '#343b4f', '#8a90a2', '#05060a',
              'rgba(5, 6, 10', 'rgba(24, 28, 38')


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
        root = root[:root.index('\n}')]
        for name, value in TOKENS.items():
            self.assertIn('%s: %s;' % (name, value), root, name)
        self.assertIn('--nt-gradient: linear-gradient(135deg, #8b5cf6, #22d3ee);', root)
        self.assertIn('radial-gradient(900px 600px at 0 0, rgba(139, 92, 246, .10), transparent 60%)', root)
        self.assertIn('radial-gradient(800px 600px at 100% 100%, rgba(34, 211, 238, .06), transparent 60%)', root)
        self.assertIn('Inter', root)
        self.assertIn('Segoe UI Variable', root)
        self.assertIn('system-ui', root)

    def test_legacy_names_only_alias_the_tokens(self):
        # Parts that are not migrated yet still read the old names; they must point at the new tokens.
        root = self.css[self.css.index(':root'):]
        root = root[:root.index('\n}')]
        for line in ('--nt-surface: var(--nt-surface-1);', '--nt-raised: var(--nt-surface-2);',
                     '--nt-muted: var(--nt-text-3);', '--nt-accent-from: var(--nt-accent);',
                     '--nt-accent-to: var(--nt-accent-2);', '--nt-bg-glow: var(--nt-aurora);'):
            self.assertIn(line, root, line)

    def test_css_restyles_surfaces(self):
        for marker in ('backdrop-filter', '::selection', '::-webkit-scrollbar', 'scrollbar-width: thin',
                       '.notion-sidebar', '[role="dialog"]', ':focus', ':hover', 'var(--nt-ring)'):
            self.assertIn(marker, self.css, marker)

    def test_round3_premium_surfaces(self):
        for marker in ('--c-bacPri: #09090b;', '--c-bacEle: #16161d;', '--c-popBac: #16161d;',
                       '--ca-sidIteSelBac:', '--ca-butHovBac:', '--ca-bacIntTra:',
                       '--nt-sidebar: var(--nt-surface-1);', '--nt-sidebar-border: var(--nt-border);',
                       'radial-gradient(', '.layout-chat', '.notion-cursor-listener',
                       '[data-agent-chat-user-step-id]', 'a.notion-link-token',
                       '.notion-inline-code-container > span', 'blur(var(--nt-blur))'):
            self.assertIn(marker, self.css, marker)
        self.assertNotIn('#191919', self.css.split('/*')[0])

    def test_notion_color_variables_use_the_new_greys(self):
        for line in ('--c-graBacPri: #16161d;', '--c-graBacSec: #1e1e27;', '--c-graBacTer: #34343f;',
                     '--c-sidSecCol: #8a8a94;', '--ca-oveSmo: rgba(5, 5, 7, .78);',
                     '--ca-popOveBac: rgba(5, 5, 7, .6);', '--c-shaCol: #050507;',
                     'background: rgba(22, 22, 29, .38);'):
            self.assertIn(line, self.css, line)

    def test_no_leftover_old_colors(self):
        for old in OLD_COLORS:
            self.assertNotIn(old, self.css, old)

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
        # No glow on button hover, no hover lift, no composer glow animation; blur only on the top bar.
        self.assertNotIn('button:hover {\n  box-shadow', self.css)
        self.assertNotIn('translateY(-1px)', self.css)
        self.assertNotIn('var(--nt-accent-gradient) border-box', self.css)
        self.assertNotIn('inset: -10px', self.css)
        self.assertNotIn('nt-desk-composer-glow', self.css)
        self.assertNotIn('[data-notion-chat-input-container]::before', self.css)
        self.assertNotIn('@keyframes', self.css)

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
