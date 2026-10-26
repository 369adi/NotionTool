"""Notion's composer model chip must never stay on 'Auto' (UI side, notiontool/web/desktop/studio.js).

Notion Auto routes to Haiku/Sonnet. studio.js switches the chip once per chip change to the last explicit
model (localStorage nt-last-model) or Opus 5.5, never while a menu is open, never in a loop and never sends.
The Auto entry of Notion's model menu is hidden by its text, and the status strip never says 'Auto model'.
The request pin in notion_shell.py (pin_config_body / _pin_request) stays in place.
Static checks like the other desktop UI tests, plus a node run of the pure helpers when node is installed.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUDIO = os.path.join(ROOT, 'notiontool', 'web', 'desktop', 'studio.js')
SHELL = os.path.join(ROOT, 'notiontool', 'notion_shell.py')
GUARD_FUNCS = ('composerChip', 'menuItems', 'hideAutoEntries', 'hideAutoSafe', 'rememberPick', 'pressDown',
               'closeMenu', 'switchFromAuto', 'guardAuto')


def read(path):
    with open(path, 'rb') as handle:
        return handle.read().decode('utf-8')


def code_of(path):
    """JS source without comments; string contents stay."""
    text = re.sub(r'/\*.*?\*/', lambda m: re.sub(r'[^\n]', ' ', m.group(0)), read(path), flags=re.S)
    return re.sub(r'(^|[\s;{}(),])//[^\n]*', r'\1', text, flags=re.M)


def body_of(code, name):
    """Body of the first `function name(` in code, by brace matching."""
    match = re.search(r'function\s+' + re.escape(name) + r'\s*\(', code)
    if not match:
        raise AssertionError('studio.js has no function ' + name)
    start = code.index('{', match.end())
    depth = 0
    for i in range(start, len(code)):
        if code[i] == '{':
            depth += 1
        elif code[i] == '}':
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
    raise AssertionError('unbalanced braces in ' + name)


class ModelNeverAutoUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = read(STUDIO)
        cls.code = code_of(STUDIO)

    def test_strip_never_says_auto_model(self):
        self.assertNotIn('Auto model', self.raw)
        strip = body_of(self.code, 'renderStrip')
        self.assertIn('isAutoText(dm)', strip)
        self.assertIn('pinnedModel()', strip)
        text = body_of(self.code, 'nextTurnText')
        self.assertIn('pinnedModel()', text)
        self.assertIn('isAutoText(code)', text)

    def test_last_model_key_and_default(self):
        self.assertIn("LAST_MODEL_KEY = 'nt-last-model'", self.code)
        self.assertIn("DEFAULT_MODEL = 'Opus 5.5'", self.code)
        pinned = body_of(self.code, 'pinnedModel')
        self.assertIn('getKey(LAST_MODEL_KEY)', pinned)
        self.assertIn('DEFAULT_MODEL', pinned)
        self.assertIn('isAutoText(last)', pinned, 'a stored Auto must never be used')

    def test_remembers_explicit_pick(self):
        pick = body_of(self.code, 'rememberPick')
        self.assertIn('setKey(LAST_MODEL_KEY', pick)
        self.assertIn('!isAutoText(name)', pick)
        self.assertIn("addEventListener('click', rememberPick, true)", self.code)
        self.assertIn("removeEventListener('click', rememberPick, true)", self.code)

    def test_once_per_chip_change_and_not_into_open_menu(self):
        guard = body_of(self.code, 'guardAuto')
        self.assertIn('key !== autoGuard.key', guard)
        self.assertIn('isAutoText(chip.text)', guard)
        self.assertIn('if (autoGuard.busy', guard)
        overlay = guard.index('overlayOpen()')
        reset = guard.index('autoGuard.pending = false')
        call = guard.index('switchFromAuto(')
        self.assertLess(overlay, call, 'must check for an open menu/dialog before clicking')
        self.assertLess(reset, call, 'pending is cleared before the one try, so a failure never retries')
        self.assertIn('AUTO_TRY_GAP_MS', guard)
        self.assertIn('guardAuto()', body_of(self.code, 'render'))

    def test_no_loop(self):
        for name in GUARD_FUNCS:
            self.assertNotIn('setInterval', body_of(self.code, name), name)
            self.assertNotIn('while', body_of(self.code, name), name)
        switch = body_of(self.code, 'switchFromAuto')
        self.assertIn('tries < AUTO_MENU_TRIES', switch)
        tries = int(re.search(r'AUTO_MENU_TRIES\s*=\s*(\d+)', self.code).group(1))
        self.assertLessEqual(tries, 10)
        gap = int(re.search(r'AUTO_TRY_GAP_MS\s*=\s*(\d+)', self.code).group(1))
        self.assertGreaterEqual(gap, 1000)

    def test_never_sends(self):
        banned = (r"'Enter'", r'ctx\.call', r'fetch\(', r'/api/', r'SEND_RE', r'blockSend', r'\.submit\(',
                  r'requestSubmit', r'XMLHttpRequest', r'__ntShell')
        for name in GUARD_FUNCS:
            body = body_of(self.code, name)
            for pattern in banned:
                self.assertIsNone(re.search(pattern, body), '%s uses %s' % (name, pattern))
        # The only element clicked from the chip side is the model chip itself (text matches MODEL_RE).
        self.assertIn('MODEL_RE.test(t)', body_of(self.code, 'composerChip'))
        self.assertIn('.nt-desk-composer [role="button"]', body_of(self.code, 'composerChip'))

    def test_auto_entry_hidden_by_text_only(self):
        hide = body_of(self.code, 'hideAutoEntries')
        self.assertIn('isAutoText(itemText(n))', hide)
        self.assertIn('modelName(itemText(n))', hide, 'only inside a menu that lists model names')
        self.assertIn("'aria-disabled', 'true'", hide)
        self.assertIn("style.display = 'none'", hide)
        self.assertNotIn('removeChild', hide)
        items = re.search(r"MENU_ITEMS\s*=\s*'([^']*)'", self.code).group(1)
        self.assertNotIn('.', items, 'menu entries are found by role, never by Notion class names')
        for name in GUARD_FUNCS:
            self.assertNotIn('.notion-', body_of(self.code, name), name)
        self.assertIn('!isAutoText(t)', body_of(self.code, 'switchFromAuto'))

    def test_request_pin_unchanged(self):
        shell = read(SHELL)
        self.assertRegex(shell, r'def\s+pin_config_body\b')
        self.assertRegex(shell, r'def\s+_pin_request\b')

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_helpers_with_node(self):
        script = (
            "const fs=require('fs');const store={};"
            "global.window={localStorage:{getItem:k=>(k in store?store[k]:null),setItem:(k,v)=>{store[k]=String(v);}}};"
            "eval(fs.readFileSync(process.argv[process.argv.length-1],'utf8'));"
            "const u=window.__ntDesk.studioUtil;const out={};"
            "out.auto=u.nextTurnText({model:'auto'});out.empty=u.nextTurnText({});"
            "out.opus=u.nextTurnText({model:'opus',effort:'medium'});"
            "store['nt-last-model']='Sonnet 5.5';out.last=u.nextTurnText({model:'Auto'});"
            "store['nt-last-model']='Auto';out.badLast=u.pinnedModel();"
            "out.isAuto=[u.isAutoText('Auto'),u.isAutoText(' auto '),u.isAutoText('Automatic'),u.isAutoText('Opus 5.5')];"
            "console.log(JSON.stringify(out));")
        proc = subprocess.run(['node', '-e', script, STUDIO], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        dot = '\u00b7'
        self.assertEqual(out['auto'], 'Next turn: Opus 5.5 %s High effort' % dot)
        self.assertEqual(out['empty'], 'Next turn: Opus 5.5 %s High effort' % dot)
        self.assertEqual(out['opus'], 'Next turn: Opus 5.5 %s Medium effort' % dot)
        self.assertEqual(out['last'], 'Next turn: Sonnet 5.5 %s High effort' % dot)
        self.assertEqual(out['badLast'], 'Opus 5.5')
        self.assertEqual(out['isAuto'], [True, True, False, False])
        self.assertEqual(out['liveAuto'], [True, True, False, False, False])
        for key in ('auto', 'empty', 'opus', 'last'):
            self.assertNotIn('Auto', out[key])


if __name__ == '__main__':
    unittest.main()
