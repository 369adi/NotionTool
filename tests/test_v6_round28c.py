"""Round 28c: Switch context dimmed in private chats, no back arrow on the Main."""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS_PATH = ROOT / 'notiontool' / 'web' / 'desktop' / 'v6.js'
CSS_PATH = ROOT / 'notiontool' / 'web' / 'desktop' / 'v6.css'
JS = JS_PATH.read_text(encoding='utf-8')
CSS = CSS_PATH.read_text(encoding='utf-8')


class PrivateSwitchButtonTest(unittest.TestCase):
    def test_collect_marks_private_and_main(self):
        self.assertIn('var own = w ? null : switchTarget(title);', JS)
        self.assertIn("isPrivate: !!(own && own.purpose === 'private'),", JS)
        self.assertIn("isMain: !!((w && (w.purpose === 'main' || w.purpose === 'operator'))"
                      " || (own && own.purpose === 'main')),", JS)

    def test_switch_button_dimmed_for_private(self):
        self.assertIn("(swPriv ? ' nt-v6-swoff'", JS)
        self.assertIn('Make this a Main&quot; to use Switch context', JS)
        self.assertIn('.nt-v6-si.nt-v6-swoff', CSS)

    def test_private_click_skips_confirm(self):
        i = JS.index("if (act === 'switch') {")
        block = JS[i:i + 700]
        self.assertLess(block.index("sw0.purpose === 'private'"), block.index('confirmUntil <= Date.now()'))


class MainBackArrowTest(unittest.TestCase):
    def test_main_has_no_back_arrow(self):
        self.assertIn('if (!info.isMain) h.push(\'<div class="nt-v6-back', JS)


class LineEndingTest(unittest.TestCase):
    def test_crlf_kept(self):
        for path in (JS_PATH, CSS_PATH):
            raw = path.read_bytes()
            self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'), path.name)


if __name__ == '__main__':
    unittest.main()
