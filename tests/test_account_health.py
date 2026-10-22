import unittest

from notiontool import account_health
from notiontool.main_wake import build_wake_message, WAKE_CANCELLED


class FakeStore:
    def __init__(self):
        self.t = {'accounts': {'acc1': dict(id='acc1', state='ready')},
                  'workers': {'acc1:th1': dict(id='acc1:th1', account_id='acc1')}}
        self.events = []

    def get(self, table, ident, default=None):
        r = self.t.get(table, {}).get(ident)
        return dict(r) if r else default

    def put(self, table, row):
        self.t.setdefault(table, {})[row['id']] = dict(row)

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, run_id, fields))


class AccountHealthTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()

    def test_first_error_short_then_escalates(self):
        now = 1000.0
        self.assertEqual(account_health.turn_error_cooldown(self.store, 'acc1', now), 20)
        self.assertLessEqual(self.store.get('accounts', 'acc1').get('cooldown_until', 0), now)
        self.assertEqual(self.store.events, [])
        self.assertEqual(account_health.turn_error_cooldown(self.store, 'acc1', now + 30), 120)
        self.assertGreaterEqual(self.store.get('accounts', 'acc1')['cooldown_until'], now + 150)
        self.assertEqual(self.store.events[-1][0], 'account_degraded')
        self.assertEqual(account_health.turn_error_cooldown(self.store, 'acc1', now + 60), 300)

    def test_old_errors_expire(self):
        account_health.turn_error_cooldown(self.store, 'acc1', 1000.0)
        self.assertEqual(account_health.turn_error_cooldown(self.store, 'acc1', 1000.0 + 700), 20)

    def test_account_of_and_unknown_account(self):
        self.assertEqual(account_health.account_of(self.store, 'acc1:th1'), 'acc1')
        self.assertEqual(account_health.account_of(self.store, 'zz:t'), 'zz')
        self.assertEqual(account_health.turn_error_cooldown(self.store, 'missing', 5.0), 20)


class WakeCancelledTests(unittest.TestCase):
    def test_cancelled_run_has_no_main_steps(self):
        text = build_wake_message([(dict(id='r1', state='cancelled'), [])])
        self.assertIn(WAKE_CANCELLED, text)
        self.assertNotIn('run tests', text)

    def test_blocked_run_keeps_main_steps(self):
        text = build_wake_message([(dict(id='r1', state='blocked'), [])])
        self.assertIn('run tests', text)


if __name__ == '__main__':
    unittest.main()
