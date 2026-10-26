import pathlib
import unittest

V6 = pathlib.Path(__file__).resolve().parents[1] / 'notiontool' / 'web' / 'desktop' / 'v6.js'


class EffortMemoryUiTest(unittest.TestCase):
    """Owner 2026-10-26: effort shows Medium again after a chat switch; keep the user's pick per chat."""

    def setUp(self):
        self.src = V6.read_text(encoding='utf-8')

    def test_pick_is_remembered_per_chat(self):
        self.assertIn("localStorage.setItem('nt-effort-pick:' + tid, lvl)", self.src)
        self.assertIn("document.addEventListener('pointerdown', onEffortPick, true)", self.src)
        self.assertIn("document.removeEventListener('pointerdown', onEffortPick, true)", self.src)

    def test_header_prefers_the_pick_over_notions_reset(self):
        self.assertIn("effort: effortPick(currentThread()) || (em ? cap(em[1]) : ''),", self.src)

    def test_only_effort_items_in_a_notion_menu_count(self):
        body = self.src[self.src.index('function onEffortPick'):]
        body = body[:body.index('var pillDownOpen')]
        self.assertIn('.notion-overlay-container [role="dialog"]', body)
        self.assertIn('EFFORT_RE.test(text(it))', body)
        self.assertIn('currentThread() !== tid', body)


if __name__ == '__main__':
    unittest.main()
