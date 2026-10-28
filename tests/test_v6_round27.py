"""Round 27 (user 2026-10-28 07:46): a chat switch from the v6 sidebar stays inside the page; bigger composer."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESK = os.path.join(ROOT, 'notiontool', 'web', 'desktop')


def read(name):
    with open(os.path.join(DESK, name), encoding='utf-8') as f:
        return f.read()


class OwnChatLinkClickTest(unittest.TestCase):
    def setUp(self):
        js = read('v6.js')
        start = js.index('function onClick(e) {')
        self.click = js[start:js.index("var act = t.getAttribute('data-act')", start)]

    def test_plain_click_on_an_own_chat_link_does_not_load_the_page(self):
        branch = self.click[self.click.index("t.tagName === 'A'"):]
        self.assertIn('e.preventDefault();', branch)
        self.assertIn('openChatRow(ownTitle)', branch)
        self.assertLess(branch.index('findRow(ownTitle)'), branch.index('e.preventDefault();'),
                        'the link is only stopped when Notion has a row to press')

    def test_modifier_clicks_keep_the_normal_link(self):
        self.assertRegex(self.click, r'if \(e\.button \|\| e\.ctrlKey \|\| e\.metaKey \|\| e\.shiftKey \|\| e\.altKey\) return;')

    def test_no_page_load_or_history_change(self):
        js = read('v6.js')
        self.assertNotRegex(js, r'\bhistory\.pushState\s*\(')
        self.assertNotRegex(js, r'\blocation\.(?:href\s*=(?!=)|assign\s*\(|replace\s*\(|reload\s*\()')


class ComposerSizeTest(unittest.TestCase):
    def test_composer_is_bigger_and_grows(self):
        css = read('v6.css')
        block = css[css.index('round 27 (user 07:46: the chat box is too small)'):]
        self.assertIn('min-height: 88px !important', block)
        self.assertIn('max-height: 40vh !important', block)
        self.assertIn('font-size: 14.5px !important', block)

    def test_round27_block_is_last_so_it_wins(self):
        css = read('v6.css')
        rules = [m.start() for m in re.finditer(r'\[contenteditable\]\[role="textbox"\]\s*\{', css)]
        self.assertTrue(rules)
        self.assertGreater(css.index('round 27 (user 07:46: the chat box is too small)'), rules[-2] if len(rules) > 1 else -1)


if __name__ == '__main__':
    unittest.main()
