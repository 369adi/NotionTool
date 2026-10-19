import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BacktestDepsTest(unittest.TestCase):
    def test_a_file_content(self):
        text = (ROOT / 'docs' / 'plan' / 'backtest_deps_a.txt').read_text(encoding='utf-8')
        self.assertEqual(text.strip(), 'deps-a 7')


if __name__ == '__main__':
    unittest.main()
