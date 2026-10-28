"""Round 27 (user 2026-10-28 07:46): the model menu the Auto guard opens on a chat switch is never seen."""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESK = os.path.join(ROOT, 'notiontool', 'web', 'desktop')


def read(name):
    with open(os.path.join(DESK, name), encoding='utf-8') as f:
        return f.read()


class AutoGuardQuietTest(unittest.TestCase):
    def setUp(self):
        js = read('studio.js')
        start = js.index('function switchFromAuto(chipNode) {')
        self.guard = js[start:js.index('function guardAuto() {', start)]
        self.js = js

    def test_menu_is_quiet_before_the_chip_is_clicked(self):
        self.assertLess(self.guard.index('classList.add(AUTO_QUIET)'), self.guard.index('chipNode.click()'))

    def test_every_exit_turns_quiet_off(self):
        finish = self.guard[self.guard.index('function finish(ok) {'):self.guard.index('function pick() {')]
        self.assertIn('quietOff();', finish)
        self.assertIn('if (stopped) { autoGuard.busy = false; quietOff(); return; }', self.guard)
        self.assertIn('catch (e) { autoGuard.busy = false; quietOff(); return; }', self.guard)

    def test_quiet_off_is_bounded(self):
        body = self.js[self.js.index('function quietOff() {'):self.js.index('function switchFromAuto(chipNode) {')]
        self.assertIn('frames < 30', body)
        self.assertIn('classList.remove(AUTO_QUIET)', body)

    def test_css_hides_by_role_only(self):
        css = read('studio.css')
        block = css[css.index('html.nt-desk-auto-quiet'):]
        self.assertIn('[role="menu"]', block)
        self.assertIn('opacity: 0 !important', block)
        self.assertNotIn('.notion-', block.split('}')[0])


if __name__ == '__main__':
    unittest.main()
