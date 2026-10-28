"""Round 24 (user 2026-10-28 06:47): instant switch from Notion's own chat list, no 'Effort' pop on a switch."""
import os
import unittest

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'notiontool', 'web', 'desktop')


def read(name):
    with open(os.path.join(WEB, name), encoding='utf-8') as f:
        return f.read()


class EffortPopTests(unittest.TestCase):
    def setUp(self):
        self.js = read('v6.js')
        self.css = read('v6.css')

    def test_no_effort_placeholder_word(self):
        self.assertNotIn("esc(info.effort || 'Effort')", self.js)
        self.assertIn("esc(info.effort || '')", self.js)

    def test_backend_effort_is_cached_and_read_first(self):
        self.assertIn("localStorage.setItem('nt-effort-be:' + tid, e)", self.js)
        self.assertIn("localStorage.getItem('nt-effort-be:' + tid) || localStorage.getItem('nt-effort-pick:' + tid)", self.js)
        self.assertIn("localStorage.setItem('nt-effort-be:' + tid, lv)", self.js)

    def test_effort_prefetched_at_click(self):
        i = self.js.index('function feedSwitch(k, title)')
        self.assertIn('askEffort(k);', self.js[i:i + 1200])

    def test_native_effort_button_hidden_by_css(self):
        self.assertIn('html.nt-v6-on [aria-label^="Change effort"] { opacity: 0 !important;', self.css)


class LinkSwitchTests(unittest.TestCase):
    def setUp(self):
        self.js = read('v6.js')

    def test_native_links_use_instant_switch(self):
        self.assertIn('function feedLinkClick(ev)', self.js)
        self.assertIn('if (!r) { feedLinkClick(ev); return; }', self.js)
        i = self.js.index('function feedLinkClick(ev)')
        body = self.js[i:i + 1200]
        self.assertIn("closest('a[href]')", body)
        self.assertIn("a.closest('.nt-v6-feed')", body)
        self.assertIn('feedSwitch(key(t)', body)
        self.assertIn('ev.ctrlKey', body)


if __name__ == '__main__':
    unittest.main()
