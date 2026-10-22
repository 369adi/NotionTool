"""Problem card shows why a run waits for the Main (needs_main_reasons)."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class LiveProblemReasonsTest(unittest.TestCase):
    def test_run_rows_carry_reasons(self):
        src = (ROOT / 'notiontool' / 'desktop_shell.py').read_text(encoding='utf-8')
        self.assertIn('needs_main_reasons={', src)

    def test_live_card_shows_reasons(self):
        js = (ROOT / 'notiontool' / 'web' / 'desktop' / 'live.js').read_text(encoding='utf-8')
        self.assertIn('obj(r.needs_main_reasons)', js)
        self.assertIn("waits for your OK' +", js)


if __name__ == '__main__':
    unittest.main()
