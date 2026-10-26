"""v6 desktop layer, user review 26.10. 01:19 (mockup 010922): the panels are patched in place, motion follows
the critique (120 ms hover/focus, 200 ms values), the steps groups share the tool timeline, the effort pill keeps
every word. Live check in the real app: work/inject_v6b.py + work/js/_probe_v6b.js.
"""
import pathlib
import re
import shutil
import subprocess
import unittest

DESK = pathlib.Path(__file__).resolve().parent.parent / 'notiontool' / 'web' / 'desktop'


class V6MotionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (DESK / 'v6.js').read_text(encoding='utf-8')
        cls.css = (DESK / 'v6.css').read_text(encoding='utf-8')

    def test_panels_are_patched_not_replaced(self):
        # A full innerHTML swap restarted every spinner, dropped hover and made the bars jump on each update.
        self.assertNotRegex(self.js, r'\b(sb|hd)\.innerHTML\s*=')
        self.assertIn('morph(sb, html)', self.js)
        self.assertIn('morph(hd, hh)', self.js)
        self.assertIn('morph: morph', self.js)

    def test_motion_timings_follow_the_critique(self):
        self.assertIn('--v6-fast: 120ms', self.css)
        self.assertIn('--v6-slow: 200ms', self.css)
        self.assertIn('prefers-reduced-motion', self.css)

    def test_live_dot_pulse_does_not_scale(self):
        # scale() on the coworker dot also moved its twin dot (::after), so the row jumped.
        frames = re.findall(r'@keyframes ntv6pulse \{(.*?)\}\s*\}', self.css, re.S)
        self.assertTrue(frames)
        self.assertNotIn('scale', frames[-1])

    def test_steps_rows_share_the_tool_timeline(self):
        rule = self.css.split('html.nt-v6-on [data-nt-v6="steps"][data-nt-v6="steps"] {', 1)[1].split('}', 1)[0]
        self.assertIn('padding: 0 28px 0 63.4px', rule)
        self.assertIn('6.1px 0 / 1px 100%', rule)

    def test_coworker_label_always_fits(self):
        self.assertIn('.nt-v6-cw .nt-v6-bar { flex: 1 1 auto;', self.css)

    def test_effort_label_keeps_every_word(self):
        # Notion's button says "Change effort, currently extra high": the pill showed only "Extra".
        m = re.search(r'\.match\(/(currently[^/]*)/i\)', self.js)
        self.assertIsNotNone(m)
        got = re.search(m.group(1), 'Change effort, currently extra high', re.I)
        self.assertEqual(got.group(1), 'extra high')
        self.assertEqual(re.search(m.group(1), 'Change effort, currently high', re.I).group(1), 'high')

    def test_v6_js_parses(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        res = subprocess.run([node, '--check', str(DESK / 'v6.js')], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == '__main__':
    unittest.main()
