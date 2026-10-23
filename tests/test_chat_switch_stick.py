"""Chat switch keeps the newest message in view (user report 2026-10-24: view sometimes jumped up)."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OVERLAY = os.path.join(ROOT, 'notiontool', 'web', 'desktop', 'overlay.js')


def source():
    with open(OVERLAY, 'r', encoding='utf-8') as f:
        return f.read()


class ChatSwitchStickTests(unittest.TestCase):
    def setUp(self):
        self.src = source()

    def test_stick_phase_after_switch(self):
        self.assertIn('var STICK_MS = 15000;', self.src)
        self.assertIn('function holdBottom()', self.src)
        self.assertIn('startStick(key);', self.src)
        self.assertIn('if (holdBottom() && !scrollTimer)', self.src)

    def test_stick_ends_on_other_chat(self):
        block = self.src[self.src.index('function holdBottom()'):]
        block = block[:block.index('\n    }\n')]
        self.assertIn('stick.key !== threadFromHref(location.href)', block)
        self.assertIn('Date.now() > stick.until', block)

    def test_only_upward_moves_cancel(self):
        block = self.src[self.src.index('function userScrolls(ev)'):]
        block = block[:block.index('\n    }\n')]
        self.assertIn('ev.deltaY < 0', block)
        self.assertIn('UP_KEYS[ev.key]', block)
        self.assertIn('editable(ev.target)', block)
        self.assertIn('stick = null;', block)
        self.assertNotIn("addEventListener('keydown', cancelScroll", self.src)
        self.assertNotIn("addEventListener('wheel', cancelScroll", self.src)

    def test_late_jump_fixed_on_scroll_event(self):
        self.assertRegex(self.src, re.compile(r"document\.addEventListener\('scroll'[^\n]*\n[^\n]*holdBottom\(\)"))

    def test_overlay_keeps_lf(self):
        with open(OVERLAY, 'rb') as f:
            self.assertNotIn(b'\r\n', f.read())


if __name__ == '__main__':
    unittest.main()
