"""Round 18b (user 2026-10-28 04:39 / 04:41): calm tool steps, Notion bar kept hidden, cheaper chat mirror."""
import pathlib
import unittest

from notiontool import tabbar_skin

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS = (ROOT / "notiontool" / "web" / "desktop" / "v6.css").read_text(encoding="utf-8")
JS = (ROOT / "notiontool" / "web" / "desktop" / "v6.js").read_text(encoding="utf-8")
R18B = CSS[CSS.index("round 18b (user 2026-10-28 04:39"):]
TAB = dict(type='page', title='Tab Bar', id='AAA', url='file:///C:/x/tabs.html',
           webSocketDebuggerUrl='ws://127.0.0.1:9333/devtools/page/AAA')


class FakeSession:
    def __init__(self, fail=False):
        self.sent, self.closed, self.fail = [], False, fail

    def send(self, method, params=None, timeout=None):
        if self.fail and method == 'Runtime.evaluate':
            raise OSError('socket gone')
        self.sent.append(method)
        return {}

    def close(self):
        self.closed = True


class ToolStepsTest(unittest.TestCase):
    def test_tool_rows_compact_without_neon(self):
        self.assertIn("min-height: 30px !important; padding: 0 24px 0 50px !important;", R18B)
        self.assertNotIn("#18f5ba", R18B)
        self.assertNotIn("#1fefbf", R18B)
        self.assertIn("stroke='%237f8aa0'", R18B)

    def test_tool_steps_label_is_plain_text(self):
        block = R18B[R18B.index(".nt-desk-toolgroup-head::before {"):]
        block = block[: block.index("}")]
        self.assertIn("background: transparent !important; border: 0 !important;", block)

    def test_model_working_small_and_grey(self):
        self.assertIn(".nt-v6-fwork.nt-v6-fwork { margin: 8px 0 4px 26px !important", R18B)


class MirrorCostTest(unittest.TestCase):
    def test_full_pass_skips_unchanged_rows(self):
        self.assertIn("var cs = n.textContent.length + ':' + n.getElementsByTagName('*').length;", JS)
        self.assertIn("e && !fdDirty.has(n) && e.cs === cs ? e.sig : n.innerHTML.length + ':' + cs", JS)
        self.assertIn("e = { sig: sig, cs: cs, c: c };", JS)


class TabBarKeepAliveTest(unittest.TestCase):
    def test_css_hides_every_bar_element(self):
        self.assertIn('body>*,body>*>*{opacity:0 !important}', tabbar_skin.CSS)

    def test_keep_alive_refreshes_the_live_session(self):
        s = FakeSession()
        holder = {'session': s}
        tabbar_skin.keep_alive(lambda u: [TAB], lambda ws: FakeSession(), 9333, holder, sleep=lambda x: None, rounds=2)
        self.assertEqual(s.sent, ['Runtime.evaluate', 'Runtime.evaluate'])
        self.assertIs(holder['session'], s)

    def test_keep_alive_reconnects_when_the_session_died(self):
        dead, fresh = FakeSession(fail=True), FakeSession()
        holder = {'session': dead}
        tabbar_skin.keep_alive(lambda u: [TAB], lambda ws: fresh, 9333, holder, sleep=lambda x: None, rounds=1)
        self.assertTrue(dead.closed)
        self.assertIs(holder['session'], fresh)
        self.assertEqual(holder['result'], dict(ok=True, target_id='AAA', attempt='keep'))

    def test_keep_alive_stops(self):
        holder = {'session': None, 'stop': True}
        tabbar_skin.keep_alive(lambda u: [], lambda ws: None, 9333, holder, sleep=lambda x: None)
        self.assertNotIn('result', holder)


if __name__ == "__main__":
    unittest.main()
