import threading
import unittest

from notiontool import token_budget
from notiontool.turn_watch import charge_main_usage


class FakeStore:
    def __init__(self, **settings):
        self.data, self.lock, self.config = {}, threading.RLock(), dict(settings)

    def settings(self):
        return dict(self.config)

    def get(self, kind, ident, default=None):
        return self.data.get((kind, ident), default)

    def put(self, kind, doc):
        self.data[(kind, doc['id'])] = dict(doc)

    def used(self, now):
        return (self.get(token_budget.KIND, str(int(token_budget.window_start(now)))) or {}).get('used', 0)


WORKER = dict(id='acc:main', purpose='main', label='Main')


class BudgetCap(unittest.TestCase):
    def test_main_turn_on_huge_history_is_capped(self):
        store, now = FakeStore(), 1_792_600_000.0
        charged = charge_main_usage(store, WORKER, dict(estimated_tokens=10_092_034, turns=15),
                                    dict(estimated_tokens=10_099_444, turns=16), now)
        self.assertEqual(charged, 7_410 + token_budget.CONTEXT_CAP)
        self.assertEqual(store.used(now), charged)

    def test_cap_setting(self):
        store, now = FakeStore(token_budget_context_cap=50_000), 1_792_600_000.0
        charged = charge_main_usage(store, WORKER, dict(estimated_tokens=900_000, turns=1),
                                    dict(estimated_tokens=900_000, turns=3), now)
        self.assertEqual(charged, 100_000)

    def test_small_context_is_charged_fully(self):
        store, now = FakeStore(), 1_792_600_000.0
        charged = charge_main_usage(store, WORKER, dict(estimated_tokens=80_000, turns=1),
                                    dict(estimated_tokens=81_000, turns=2), now)
        self.assertEqual(charged, 81_000)

    def test_send_is_capped(self):
        store, now = FakeStore(), 1_792_600_000.0
        worker = dict(WORKER, context_usage=dict(estimated_tokens=12_000_000))
        token_budget.charge_send(store, worker, 'hello', now=now)
        self.assertLess(store.used(now), token_budget.CONTEXT_CAP + 100)


if __name__ == '__main__':
    unittest.main()
