"""Static checks for the desktop overlay part 'chrome' (notiontool/web/desktop/chrome.js/.css)."""
import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / 'notiontool' / 'web' / 'desktop' / 'chrome.js'
CSS = ROOT / 'notiontool' / 'web' / 'desktop' / 'chrome.css'
NODE = r'C:\Program Files\nodejs\node.exe'


def block(text, start):
    i = text.index(start)
    return text[i:text.index('];', i)]


def rule(css, selector):
    """Body of the first rule whose selector list is exactly `selector`."""
    match = re.search(r'(?:^|})\s*' + re.escape(selector) + r'\s*{([^}]*)}', css)
    assert match, selector
    return match.group(1)


class DesktopChromeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js_bytes = JS.read_bytes()
        cls.css_bytes = CSS.read_bytes()
        cls.js = cls.js_bytes.decode('utf-8')
        cls.css = cls.css_bytes.decode('utf-8')
        cls.css_plain = re.sub(r'/\*.*?\*/', '', cls.css, flags=re.S)

    def test_registers_part(self):
        self.assertIn('window.__ntDesk', self.js)
        self.assertIn('window.__ntDesk = window.__ntDesk || {}', self.js)
        self.assertIn('.chrome =', self.js)
        self.assertIn('selfcheck', self.js)
        self.assertIn('stop:', self.js)
        self.assertTrue(self.js.lstrip().startswith('/*'))
        self.assertTrue(self.js.rstrip().endswith('})();'))

    def test_observer_and_state(self):
        self.assertIn('MutationObserver', self.js)
        self.assertEqual(self.js.count('new MutationObserver'), 1)
        self.assertIn('250', self.js)
        self.assertIn('app_hide', self.js)
        self.assertIn('onState', self.js)
        self.assertNotIn('.remove()', self.js)  # never removes Notion nodes
        self.assertNotIn('removeChild', self.js)
        self.assertNotIn('replaceChild', self.js)
        self.assertNotIn('innerHTML', self.js)
        self.assertNotIn('outerHTML', self.js)

    def test_hide_list_english_and_german(self):
        hide = block(self.js, 'var HIDE = [')
        for label in ('home', 'startseite', 'meetings', 'besprechungen', "what's new", 'neuigkeiten',
                      'templates', 'vorlagen', 'marketplace', 'marktplatz', 'inbox', 'posteingang',
                      'get started', 'erste schritte', 'invite members', 'mitglieder einladen'):
            self.assertIn("'%s'" % label if "'" not in label else '"%s"' % label, hide)
        for ident in ('home', 'meetings', 'whats_new', 'templates', 'marketplace', 'inbox', 'get_started', 'invite'):
            self.assertIn("id: '%s'" % ident, hide)

    def test_keep_list(self):
        keep = block(self.js, 'var KEEP = [')
        for label in ('settings', 'einstellungen', 'search', 'suche', 'trash', 'papierkorb'):
            self.assertIn("'%s'" % label, keep)
        self.assertIn("id: 'account_switcher'", keep)
        self.assertNotIn('settings', block(self.js, 'var HIDE = ['))

    def test_css_only_own_classes(self):
        css = self.css_plain
        classes = re.findall(r'\.(-?[A-Za-z_][\w-]*)', css)
        self.assertTrue(classes)
        for name in classes:
            self.assertTrue(name.startswith('nt-desk-'), name)
        self.assertIn('.nt-desk-hidden', css)
        self.assertIn('display: none !important', css)
        self.assertNotIn('notion-', css)
        js_classes = re.findall(r"'(nt-[\w-]+)'", self.js)
        for name in js_classes:
            self.assertTrue(name.startswith('nt-desk-'), name)

    # ---- window chrome redesign -----------------------------------------------------------

    def test_title_branding_and_restore(self):
        self.assertIn("TITLE_PREFIX = 'NotionTool \\u00b7 '", self.js)
        self.assertIn('function brandTitle()', self.js)
        self.assertIn('document.title = TITLE_PREFIX +', self.js)
        self.assertIn('function restoreTitle()', self.js)
        self.assertIn('title.slice(TITLE_PREFIX.length)', self.js)
        stop = self.js[self.js.index('stop: function'):self.js.index('selfcheck: function')]
        self.assertIn('restoreTitle()', stop)

    def test_style_classes_marked_and_cleaned(self):
        for name in ('nt-desk-chrome-on', 'nt-desk-topbar', 'nt-desk-composer', 'nt-desk-composer-empty',
                     'nt-desk-message', 'nt-desk-code', 'nt-desk-upsell'):
            self.assertIn("'%s'" % name, self.js)
        self.assertIn('function setMarks(cls, list)', self.js)
        stop = self.js[self.js.index('stop: function'):self.js.index('selfcheck: function')]
        self.assertIn('setMarks(cls, [])', stop)
        self.assertIn('classList.remove(ROOT_ON)', stop)
        self.assertIn('hidden.forEach(function (row) { row.classList.remove(HIDDEN); })', stop)
        self.assertIn('styled:', self.js)

    def test_detection_selectors(self):
        self.assertIn('.notion-topbar', self.js)
        self.assertIn('[role="textbox"][contenteditable="true"]', self.js)
        self.assertIn("PAGE_SEL = '.notion-page-content'", self.js)  # page editor is never a composer
        self.assertIn('inside(box, PAGE_SEL)', self.js)
        for part in ('[class*="upsell" i]', '[class*="upgrade-banner" i]', '[class*="paywall" i]'):
            self.assertIn(part, self.js)

    def test_quota_banner_texts(self):
        quota = block(self.js, 'var QUOTA = [')
        self.assertIn("'verbraucht dein ki-kontingent'", quota)
        self.assertIn("'uses your ai credits faster'", quota)
        # "Opus 5.5 consumes your AI allowance faster than other models" (user 2026-10-24)
        self.assertIn("'consumes your ai allowance faster'", quota)
        self.assertIn("'ai allowance faster than other models'", quota)
        for phrase in re.findall(r"'([^']*)'", quota):
            self.assertEqual(phrase, phrase.lower(), phrase)
        self.assertIn('function bannerFor(node)', self.js)
        banner = self.js[self.js.index('function bannerFor(node)'):self.js.index('function safeBanner')]
        self.assertIn('hasTextbox(cur)', banner)  # never climbs into the composer
        self.assertIn('BANNER_MAX_TEXT', banner)
        self.assertIn('allow.upsell !== false', self.js)

    def test_css_redesign(self):
        css = self.css_plain
        self.assertIn('display: none !important', rule(css, '.nt-desk-upsell'))
        top = rule(css, '.nt-desk-chrome-on .nt-desk-topbar')
        self.assertIn('backdrop-filter: blur(', top)
        self.assertIn('var(--nt-', top)
        comp = rule(css, '.nt-desk-chrome-on .nt-desk-composer')
        self.assertIn('border-radius', comp)
        self.assertIn('var(--nt-radius, 12px)', comp)
        self.assertIn('var(--nt-raised, #181c26)', comp)
        focus = rule(css, '.nt-desk-chrome-on .nt-desk-composer:focus-within')
        self.assertIn('linear-gradient(135deg, var(--nt-accent-1, #7c5cff), var(--nt-accent-2, #22d3ee))', focus)
        hint = rule(css, '.nt-desk-chrome-on .nt-desk-composer-empty::after')
        self.assertIn("content: 'NotionTool'", hint)
        self.assertIn('pointer-events: none', hint)
        msg = rule(css, '.nt-desk-chrome-on .nt-desk-message')
        self.assertIn('max-width', msg)
        self.assertIn('line-height: 1.6', msg)
        code = rule(css, '.nt-desk-chrome-on .nt-desk-message .nt-desk-code')
        self.assertIn('var(--nt-raised, #181c26)', code)
        # every var() has a fallback so the part works without the theme part
        for call in re.findall(r'var\(--nt-[\w-]+[^)]*\)', css):
            self.assertIn(',', call, call)

    def test_lf_utf8(self):
        for data in (self.js_bytes, self.css_bytes, Path(__file__).read_bytes()):
            self.assertNotIn(b'\r', data)
            data.decode('utf-8')

    @unittest.skipUnless(os.path.isfile(NODE), 'node not installed')
    def test_node_check(self):
        res = subprocess.run([NODE, '--check', str(JS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == '__main__':
    unittest.main()
