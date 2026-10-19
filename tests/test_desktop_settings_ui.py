"""Static checks for the desktop overlay part 'settings' (notiontool/web/desktop/settings.js/.css)."""
import os
import re
import subprocess
import unittest
from pathlib import Path

from notiontool import desktop_settings as ds

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / 'notiontool' / 'web' / 'desktop' / 'settings.js'
CSS = ROOT / 'notiontool' / 'web' / 'desktop' / 'settings.css'
NODE = r'C:\Program Files\nodejs\node.exe'
SECTION_IDS = ('general', 'team', 'app', 'advanced')


class DesktopSettingsUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js_bytes = JS.read_bytes()
        cls.css_bytes = CSS.read_bytes()
        cls.js = cls.js_bytes.decode('utf-8')
        cls.css = cls.css_bytes.decode('utf-8')

    def block(self, start, end='};'):
        i = self.js.index(start)
        return self.js[i:self.js.index(end, i)]

    def test_registers_part(self):
        self.assertIn('window.__ntDesk = window.__ntDesk || {}', self.js)
        self.assertIn('.settings =', self.js)
        self.assertIn('open: open', self.js)
        self.assertIn('stop:', self.js)
        self.assertTrue(self.js.lstrip().startswith('/*'))
        self.assertTrue(self.js.rstrip().endswith('})();'))

    def test_actions(self):
        self.assertIn("call('settings_schema', {})", self.js)
        self.assertIn("call('settings_set', { values: data.server })", self.js)
        self.assertIn("call('app_settings_set', { values: data.app })", self.js)
        self.assertIn("indexOf('app_') === 0", self.js)
        self.assertIn('app_local', self.js)
        self.assertIn("'Settings saved'", self.js)
        self.assertIn("'No changes to save'", self.js)
        self.assertIn("'error'", self.js)

    def test_dialog_entry_and_panel(self):
        for text in ('NotionTool', 'Einstellungen', "'settings'", "'einstellungen'", "'my account'", "'mein konto'",
                     '[role="dialog"]', 'nt-desk-set-nav', 'nt-desk-set-panel', 'nt-desk-set-modal', 'insertBefore',
                     'hidden_notion', 'label_de', 'nt-desk-hidden', "'Comma'", 'ctrlKey', 'shiftKey'):
            self.assertIn(text, self.js)
        for kind in ("'checkbox'", "'number'", "'select'", "'text'"):
            self.assertIn(kind, self.js)

    def test_never_hidden_sections(self):
        never = self.block('var NEVER_HIDE = [', '];')
        for label in ('account', 'security', 'billing', 'notion ai', 'connections', 'sicherheit', 'abrechnung',
                      'verbindungen'):
            self.assertIn("'%s'" % label, never)

    def test_sections_in_order(self):
        sections = self.block('var SECTIONS = [', '];')
        labels = re.findall(r"label: '([^']+)'", sections)
        self.assertEqual(labels, ['General', 'Mains & Coworkers', 'Desktop app', 'Advanced'])
        self.assertEqual(tuple(re.findall(r"id: '([^']+)'", sections)), SECTION_IDS)
        self.assertEqual(sections.count('desc:'), 4)

    def test_every_setting_has_a_section(self):
        mapping = dict(re.findall(r"(\w+): '(\w+)'", self.block('var SECTION_OF_KEY = {')))
        for section in mapping.values():
            self.assertIn(section, SECTION_IDS)
        for entry in ds.settings_schema({})['harness']:
            key = entry['key']
            if key.startswith('app_') or entry.get('app_local'):
                continue  # sectionOf() sends unmapped app-local keys to 'app'
            self.assertIn(key, mapping, key)
        self.assertIn("if (isAppLocal(entry)) return 'app';", self.js)
        self.assertIn("return 'advanced';", self.js)

    def test_modern_controls(self):
        for text in ("role: 'switch'", "role: 'radiogroup'", "role: 'radio'", "role: 'tablist'", "role: 'tab'",
                     "role: 'tabpanel'", 'SEGMENT_MAX = 4', 'nt-desk-set-help', 'nt-desk-set-toast',
                     "'aria-live': 'polite'", 'ArrowDown', 'ArrowUp', "'aria-modal': 'true'"):
            self.assertIn(text, self.js)
        # Segmented controls keep the raw option value; only the label is prettified.
        self.assertIn("'data-value': String(option)", self.js)
        self.assertIn('node.value = String(option);', self.js)

    def test_keys_close_and_save(self):
        self.assertIn("ev.key === 'Escape' && modal", self.js)
        self.assertIn('if (modal) closeModal(); else open();', self.js)
        self.assertIn("(ev.key === 's' || ev.key === 'S')", self.js)
        self.assertIn('panel.contains(ev.target)', self.js)
        self.assertIn('if (ev.target === modal) closeModal();', self.js)

    def test_failed_save_keeps_edits(self):
        i = self.js.index('function doSave()')
        body = self.js[i:self.js.index('function showInDialog()', i)]
        fail = body[body.index('function (err)'):]
        self.assertIn('setSaving(false)', fail)
        self.assertNotIn('refreshPanel()', fail)

    def test_observer_and_safety(self):
        self.assertEqual(self.js.count('new MutationObserver'), 1)
        self.assertIn('DEBOUNCE_MS = 250', self.js)
        self.assertNotIn('innerHTML', self.js)
        self.assertNotIn('outerHTML', self.js)
        self.assertNotIn('insertAdjacentHTML', self.js)
        self.assertNotIn('removeChild', self.js)
        for text in ('fetch(', 'XMLHttpRequest', 'WebSocket', 'setInterval', '127.0.0.1', 'Bearer', 'localhost'):
            self.assertNotIn(text, self.js)

    def test_css_only_own_classes(self):
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        classes = re.findall(r'\.(-?[A-Za-z_][\w-]*)', css)
        self.assertTrue(classes)
        for name in classes:
            self.assertTrue(name.startswith('nt-desk-'), name)
        self.assertIn('.nt-desk-set-panel', css)
        self.assertIn('.nt-desk-set-modal', css)
        self.assertNotIn('notion-', css)
        for name in re.findall(r"'(nt-[\w-]+)'", self.js):
            self.assertTrue(name.startswith('nt-desk-'), name)

    def test_css_theme_tokens(self):
        for text in ('var(--nt-bg, #0b0d12)', 'var(--nt-surface, #12151d)', 'var(--nt-raised, #181c26)',
                     'var(--nt-radius, 12px)', '#7c5cff', '#22d3ee', 'backdrop-filter: blur(',
                     '.nt-desk-set-switch', '.nt-desk-set-seg', '.nt-desk-set-toast-show', '.nt-desk-set-side',
                     'prefers-reduced-motion'):
            self.assertIn(text, self.css)
        # Every custom property use has a fallback.
        for use in re.findall(r'var\((--nt-[\w-]+)\s*([,)])', self.css):
            if not use[0].startswith('--nt-set-'):
                self.assertEqual(use[1], ',', use[0])

    def test_lf_utf8(self):
        for data in (self.js_bytes, self.css_bytes, Path(__file__).read_bytes()):
            self.assertNotIn(b'\r', data)
            self.assertFalse(data.startswith(b'\xef\xbb\xbf'))
            data.decode('utf-8')

    @unittest.skipUnless(os.path.isfile(NODE), 'node not installed')
    def test_node_check(self):
        res = subprocess.run([NODE, '--check', str(JS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == '__main__':
    unittest.main()
