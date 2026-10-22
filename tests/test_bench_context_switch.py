"""Tests for bench/context_switch.py. MOCKED offline clients and temp paths only.

No model, no Notion, no chat: the clients here are the scripted offline clients
from bench/context_switch.py (built on bench/offline_client.py) or local stubs.
"""

import os
import shutil
import tempfile
import unittest

from bench import context_switch as cs
from bench.metrics import load_jsonl, validate_row
from bench.run_bench import RefusedAccount

BLOCKED_ACCOUNT = '971f379ce7b54562ab611631b19c324e'
LIVE_ACCOUNT = 'd84a51c708be46b78965f35089704b46'


class NonMockedStub(object):
    """Not mocked, never talks to anything: records every call so a refusal can be shown to happen first."""

    mocked = False

    def __init__(self):
        self.calls = []

    def prepare(self, level_tokens, variant):
        self.calls.append(('prepare', variant))
        return dict(context_tokens=1000, switch_s=0.01 if variant == 'fresh' else None, run_ref='stub')

    def send_b0(self, prompt):
        self.calls.append(('send_b0',))
        return 'b0'

    def send_harness(self, prompt):
        self.calls.append(('send_harness',))
        return 'final'

    def wait_done(self, handle):
        self.calls.append(('wait_done', handle))
        return dict(accepted=True, checklist=dict((k, True) for k in cs.CHECK_IDS),
                    ttfo_s=0.1, turn_s=0.1, errors=0, retries=0, run_ref='stub-' + handle)


class ContextSwitchBenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='test-cs-')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def out(self, name):
        return os.path.join(self.tmp, name)

    # 1. offline bench: 30 rows, all with quality 1.0
    def test_offline_bench_gives_30_rows_with_quality_1(self):
        path = self.out('rows.jsonl')
        rows = cs.run_cs_bench(cs.offline_client(), 'offline', path, cs.LEVELS, 3)
        self.assertEqual(len(rows), 30)
        self.assertEqual(len(cs.LEVELS) * 2 * 3, 30)
        for row in rows:
            self.assertIsNone(validate_row(row))
            self.assertEqual(row['quality'], 1.0)
            self.assertTrue(row['accepted'])
            self.assertTrue(row['mocked'])
            self.assertFalse(row['context_missing'])
        self.assertEqual(sorted(set(r['cs_variant'] for r in rows)), ['fresh', 'long'])
        self.assertEqual(len(load_jsonl(path)), 30)

    # 2. control client: Q4 fails in 5/5 fresh runs
    def test_control_client_fails_q4_in_all_5_fresh_runs(self):
        rows = cs.run_cs_bench(cs.offline_control_client(), 'offline', self.out('control.jsonl'), cs.LEVELS, 1)
        fresh = [r for r in rows if r['cs_variant'] == 'fresh']
        long_rows = [r for r in rows if r['cs_variant'] == 'long']
        self.assertEqual(len(fresh), 5)
        self.assertEqual(sum(1 for r in fresh if r['checklist']['Q4'] is False), 5)
        self.assertTrue(all(r['context_missing'] for r in fresh))
        self.assertTrue(all(not r['accepted'] for r in fresh))
        self.assertTrue(all(r['checklist']['Q4'] and not r['context_missing'] for r in long_rows))

    # 3a. blocked account 971f is refused, mocked or not
    def test_blocked_account_971f_is_refused(self):
        for client in (cs.offline_client(), NonMockedStub()):
            path = self.out('blocked.jsonl')
            with self.assertRaises(RefusedAccount):
                cs.run_cs_bench(client, BLOCKED_ACCOUNT, path, cs.LEVELS[:1], 1)
            self.assertFalse(os.path.exists(path))
            self.assertEqual(getattr(client, 'calls', []), [])

    # 3b. every non-mocked client outside d84a is refused before any call
    def test_non_mocked_client_outside_d84a_is_refused(self):
        for account in ('cbdec17c5f8e4f6a9d0b1c2d3e4f5a6b', '06e0398800000000000000000000000a',
                        'offline', '', None, 'xd84a51c708be46b78965f35089704b46', BLOCKED_ACCOUNT):
            stub = NonMockedStub()
            path = self.out('live.jsonl')
            with self.assertRaises(RefusedAccount, msg=repr(account)):
                cs.run_cs_bench(stub, account, path, cs.LEVELS[:1], 1)
            self.assertEqual(stub.calls, [], repr(account))
            self.assertFalse(os.path.exists(path))

    def test_non_mocked_client_on_d84a_is_allowed(self):
        stub = NonMockedStub()
        rows = cs.run_cs_bench(stub, LIVE_ACCOUNT, self.out('d84a.jsonl'), cs.LEVELS[:1], 1)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not r['mocked'] for r in rows))
        self.assertIn(('send_b0',), stub.calls)

    # 4. break-even T* for C=41.6 s, T0=12000, b=0, e x R=9.75 s/100k, N=1 is about 439k
    def test_break_even_is_about_439k(self):
        e = 9.75e-6          # extra error probability per token
        retry_cost = 10.0    # seconds lost per failed turn, so e x R = 9.75 s per 100k tokens
        be = cs.break_even(0.0, e, retry_cost, 41.6, 12000)
        self.assertTrue(be['valid'])
        self.assertAlmostEqual(be['per_token'] * 1e5, 9.75, places=9)
        t_star = be['thresholds'][1]
        self.assertAlmostEqual(t_star, 12000 + 41.6 / 9.75e-5, delta=1e-3)
        self.assertLessEqual(abs(t_star - 439000), 2000)
        self.assertEqual(cs.ktok(t_star), '439k')


if __name__ == '__main__':
    unittest.main()
