import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BacktestReviewTest(unittest.TestCase):
    def test_file_content(self):
        text = (ROOT / 'docs' / 'plan' / 'backtest_review.txt').read_text(encoding='utf-8')
        self.assertEqual(text.strip(), 'review ok')


if __name__ == '__main__':
    unittest.main()
