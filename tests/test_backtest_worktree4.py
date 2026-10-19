import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BacktestWorktree4Test(unittest.TestCase):
    def test_marker_file(self):
        text = (ROOT / 'docs' / 'plan' / 'backtest_worktree4.txt').read_text(encoding='utf-8')
        self.assertEqual(text.strip(), 'worktree ok')


if __name__ == '__main__':
    unittest.main()
