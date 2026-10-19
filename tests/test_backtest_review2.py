import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BacktestReview2Test(unittest.TestCase):
    def test_file_text(self):
        text = (ROOT / 'docs' / 'plan' / 'backtest_review2.txt').read_text(encoding='utf-8')
        self.assertEqual(text.strip(), 'review2 ok')


if __name__ == '__main__':
    unittest.main()
