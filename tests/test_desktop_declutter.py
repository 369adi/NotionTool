"""Checks for the desktop overlay part 'declutter' (notiontool/web/desktop/declutter.js)."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from notiontool import desktop_settings, desktop_shell

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / 'notiontool' / 'web' / 'desktop' / 'declutter.js'
NODE = r'C:\Program Files\nodejs\node.exe'

RULES = r'''
global.window = {};
require(process.argv[2]);
var m = window.__ntDesk.declutterMatch;
console.log(JSON.stringify({
  fbDe: m.labelReason('Positives Feedback teilen', 'button'),
  fbDe2: m.labelReason('Negatives Feedback teilen', 'button'),
  fbEn: m.labelReason('Good response', 'button'),
  copy: m.labelReason('Antwort kopieren', 'button'),
  copyKeep: m.keepLabel('Text kopieren'),
  retry: m.keepLabel('Try again'),
  newsDe: m.labelReason('Aktuelle und neue Funktionen', 'button'),
  newsEn: m.labelReason('What\u2019s new', 'button'),
  help: m.labelReason('Hilfe', 'button'),
  meetingsTab: m.labelReason('Meetings', 'tab'),
  meetingsPage: m.labelReason('Meetings', 'treeitem'),
  search: m.labelReason('Suchen', 'button'),
  send: m.labelReason('KI-Nachricht senden', 'button'),
  newChat: m.labelReason('Neuer Chat', 'button'),
  trialDe: m.buttonReason('Probe-Abo starten'),
  upgrade: m.buttonReason(' Upgrade '),
  agent: m.buttonReason('Neuer Agent'),
  templates: m.buttonReason('Browse templates'),
  longText: m.buttonReason('Upgrade the parser to version 2 and run all tests before release please now'),
  onboarding: m.cardReason('Erste Schritte mit Notion AI: 3 von 5 erledigt'),
  banner: m.cardReason('New in Notion AI: research mode'),
  bigText: m.cardReason(new Array(60).join('onboarding text ')),
  plain: m.cardReason('Weekly sales pipeline review'),
  dialogNews: m.dialogReason('', 'Aktuelle und neue Funktionen Neu: Agents ...'),
  dialogTpl: m.dialogReason('Vorlagen', ''),
  dialogOther: m.dialogReason('Einstellungen', 'Konto')
}));
'''


class DesktopDeclutterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = JS.read_bytes()
        cls.js = cls.raw.decode('utf-8')

    def test_lf_and_part_contract(self):
        self.assertNotIn(b'\r\n', self.raw)
        self.assertTrue(self.js.lstrip().startswith('/*'))
        self.assertTrue(self.js.rstrip().endswith('})();'))
        self.assertIn('window.__ntDesk = window.__ntDesk || {}', self.js)
        self.assertIn('window.__ntDesk.declutter = function (ctx)', self.js)
        for name in ('selfcheck:', 'stop:', 'ctx.onState', 'ctx.getState()'):
            self.assertIn(name, self.js)

    def test_observer_throttle_and_class_only(self):
        self.assertEqual(self.js.count('new MutationObserver'), 1)
        self.assertIn('var THROTTLE_MS = 300;', self.js)
        self.assertIn('{ childList: true, subtree: true }', self.js)
        self.assertIn("var HIDDEN = 'nt-desk-hidden';", self.js)
        self.assertIn('state.declutter === false', self.js)
        for bad in ('.remove()', 'removeChild', 'replaceChild', 'innerHTML', 'outerHTML', 'setAttribute',
                    '.click()', 'location.reload', 'style.display'):
            self.assertNotIn(bad, self.js)

    def test_rules_in_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'rules.js'
            script.write_text(RULES, encoding='utf-8')
            out = subprocess.run([NODE, str(script), str(JS)], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        r = json.loads(out.stdout)
        self.assertEqual((r['fbDe'], r['fbDe2'], r['fbEn']), ('feedback',) * 3)
        self.assertEqual((r['copy'], r['search'], r['send'], r['newChat']), ('',) * 4)
        self.assertTrue(r['copyKeep'] and r['retry'])
        self.assertEqual((r['newsDe'], r['newsEn']), ('whats_new', 'whats_new'))
        self.assertEqual(r['help'], 'help')
        self.assertEqual((r['meetingsTab'], r['meetingsPage']), ('meetings', ''))
        self.assertEqual((r['trialDe'], r['upgrade'], r['agent'], r['templates']),
                         ('trial', 'trial', 'new_agent', 'templates'))
        self.assertEqual(r['longText'], '')
        self.assertEqual((r['onboarding'], r['banner']), ('onboarding', 'ai_banner'))
        self.assertEqual((r['bigText'], r['plain']), ('', ''))
        self.assertEqual((r['dialogNews'], r['dialogTpl'], r['dialogOther']), ('whats_new', 'templates', ''))

    def test_keeps_editor_composer_and_menus(self):
        keep = self.js[self.js.index('var KEEP_SEL'):]
        keep = keep[:keep.index(';')]
        for sel in ('[role="textbox"]', '[contenteditable="true"]', 'agent-send-message-button',
                    '.notion-page-content', '[aria-haspopup="menu"]', '[aria-haspopup="listbox"]'):
            self.assertIn(sel, keep)

    def test_registered_in_shell(self):
        self.assertIn('declutter', desktop_shell.PART_FILES)
        self.assertIn('declutter', desktop_shell.MOUNT_ORDER)
        self.assertEqual(desktop_shell.PART_FILES.index('declutter'), desktop_shell.PART_FILES.index('chrome') + 1)
        self.assertIn('declutter', [stem for stem, _js, _css in desktop_shell.part_files()])
        self.assertIn("declutter=app.get('app_declutter', True) is not False", Path(desktop_shell.__file__).read_text(encoding='utf-8'))

    def test_setting_default_on_and_saved(self):
        entry = [e for e in desktop_settings.HARNESS_SETTINGS if e['key'] == 'app_declutter'][0]
        self.assertEqual((entry['type'], entry['default'], entry['app_local']), ('bool', True, True))
        self.assertTrue(desktop_shell.app_defaults()['app_declutter'])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(desktop_shell.save_app_settings({'app_declutter': False}, tmp)['app_declutter'])
            self.assertFalse(desktop_shell.load_app_settings(tmp)['app_declutter'])


if __name__ == '__main__':
    unittest.main()