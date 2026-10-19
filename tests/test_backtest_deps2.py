"""Backtest: task B reads the file task A merged (docs/plan/backtest_deps2_a.txt)."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BacktestDeps2Tests(unittest.TestCase):
    def test_task_a_value(self):
        text = (ROOT / 'docs' / 'plan' / 'backtest_deps2_a.txt').read_text(encoding='utf-8')
        self.assertEqual(text.strip(), 'deps2-a 2000')


if __name__ == '__main__':
    unittest.main()
