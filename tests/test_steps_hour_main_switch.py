import pathlib
import re
import unittest
from unittest import mock

from notiontool import desktop_shell, step_guard

ROOT = pathlib.Path(__file__).resolve().parents[1]
OVERLAY = (ROOT / 'notiontool' / 'web' / 'desktop' / 'overlay.js').read_text(encoding='utf-8')
SERVER = (ROOT / 'notiontool' / 'server.py').read_text(encoding='utf-8')


class StepsHourTest(unittest.TestCase):
    def test_left_is_limit_minus_used(self):
        fake = dict(level='ok', wait=0, ai_steps_60m=120, steps_15m=40,
                    limits=dict(step_guard.DEFAULTS))
        with mock.patch.object(step_guard, 'state', return_value=fake):
            out = desktop_shell._steps_hour(object(), 'acc', 0)
        self.assertEqual(out['used'], 120)
        self.assertEqual(out['limit'], step_guard.DEFAULTS['inf_hard_60m'])
        self.assertEqual(out['left'], step_guard.DEFAULTS['inf_hard_60m'] - 120)
        self.assertEqual(out['limit_15m'], step_guard.DEFAULTS['step_hard_15m'])

    def test_never_negative_and_fail_safe(self):
        fake = dict(level='pause', wait=60, ai_steps_60m=999, limits=dict(step_guard.DEFAULTS))
        with mock.patch.object(step_guard, 'state', return_value=fake):
            self.assertEqual(desktop_shell._steps_hour(object(), 'acc', 0)['left'], 0)
        with mock.patch.object(step_guard, 'state', side_effect=RuntimeError('x')):
            self.assertIsNone(desktop_shell._steps_hour(object(), 'acc', 0))


class OverlayTest(unittest.TestCase):
    def test_main_rows_get_visible_switch_button(self):
        self.assertIn('function mainSwitchButton(info)', OVERLAY)
        self.assertIn("'data-nt-desk': 'main-switch'", OVERLAY)
        self.assertIn("runMenuItem('switch'", OVERLAY)

    def test_steps_chip_in_coworker_head(self):
        self.assertIn('function stepsChip(state)', OVERLAY)
        self.assertIn("'AI steps left: '", OVERLAY)
        self.assertIn('stepsChip(state)', OVERLAY)


class ResetCoworkersTest(unittest.TestCase):
    def test_reset_also_deletes_renamed_pool_chats(self):
        self.assertEqual(SERVER.count("re.match(r'^Coworker \\d+$'"), 2)
        pat = r'^Coworker \d+$'
        self.assertTrue(re.match(pat, 'Coworker 17'))
        self.assertFalse(re.match(pat, 'Roblox 1'))
        self.assertFalse(re.match(pat, 'Coworker notes'))


if __name__ == '__main__':
    unittest.main()
