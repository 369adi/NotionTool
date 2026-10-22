import unittest
from unittest import mock

from notiontool import token_budget
from notiontool.turn_watch import charge_main_usage, MAX_MAIN_TURNS_PER_CHECK


class _Store:
    def settings(self):
        return {}


class MainChargeTests(unittest.TestCase):
    def run_charge(self, before, after):
        calls = []
        with mock.patch.object(token_budget, 'charge', lambda *a, **k: calls.append((a, k))):
            tokens = charge_main_usage(_Store(), dict(id='w', purpose='main'), before, after, now=1.0)
        return tokens, calls

    def test_one_new_turn_is_charged_once(self):
        tokens, calls = self.run_charge(dict(estimated_tokens=300_000, turns=10),
                                        dict(estimated_tokens=310_000, turns=11))
        self.assertEqual(tokens, 10_000 + 200_000)
        self.assertEqual(len(calls), 1)

    def test_window_drop_is_a_new_baseline(self):
        tokens, calls = self.run_charge(dict(estimated_tokens=900_000, turns=40),
                                        dict(estimated_tokens=150_000, turns=6))
        self.assertEqual(tokens, 0)
        self.assertEqual(calls, [])

    def test_turn_jump_is_capped(self):
        tokens, _ = self.run_charge(dict(estimated_tokens=5_000_000, turns=2),
                                    dict(estimated_tokens=5_000_000, turns=40))
        self.assertEqual(tokens, MAX_MAIN_TURNS_PER_CHECK * 200_000)

    def test_turn_drop_with_token_growth_is_not_charged(self):
        tokens, _ = self.run_charge(dict(estimated_tokens=100_000, turns=30),
                                    dict(estimated_tokens=400_000, turns=5))
        self.assertEqual(tokens, 0)


if __name__ == '__main__':
    unittest.main()
