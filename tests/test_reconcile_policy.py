import itertools
import unittest
from types import SimpleNamespace

from notiontool import reconcile_policy as rp

T0 = 1_000_000.0


def task(**kw):
    base = {'state': 'uncertain', 'uncertain_since': T0, 'reconcile_attempts': 0, 'output': None}
    base.update(kw)
    return base


def remote(state='unknown', had_output=None, error_before_output=None):
    return {'state': state, 'had_output': had_output, 'error_before_output': error_before_output}


class ReconcilePolicyTests(unittest.TestCase):
    def check(self, result, action):
        self.assertEqual(result[0], action, result)
        self.assertIsInstance(result[1], str)
        self.assertTrue(result[1].strip())

    def test_wait_before_90s(self):
        self.check(rp.decide(task(), remote(), T0 + 89.9, None), rp.WAIT)

    def test_reconcile_at_90s(self):
        self.check(rp.decide(task(), remote(), T0 + 90, None), rp.RECONCILE)
        self.check(rp.decide(task(), remote('missing'), T0 + 90, {}), rp.RECONCILE)

    def test_three_attempt_limit(self):
        self.check(rp.decide(task(reconcile_attempts=2), remote(), T0 + 120, None), rp.RECONCILE)
        self.check(rp.decide(task(reconcile_attempts=3), remote(), T0 + 120, None), rp.ESCALATE)
        self.check(rp.decide(task(reconcile_attempts=3), remote(), T0 + 10, None), rp.WAIT)

    def test_escalate_at_900s(self):
        self.check(rp.decide(task(), remote(), T0 + 899, None), rp.RECONCILE)
        self.check(rp.decide(task(), remote(), T0 + 900, None), rp.ESCALATE)

    def test_missing_attempts_defaults_to_zero(self):
        t = task()
        del t['reconcile_attempts']
        self.check(rp.decide(t, remote(), T0 + 100, None), rp.RECONCILE)
        self.check(rp.decide(task(reconcile_attempts=None), remote(), T0 + 100, None), rp.RECONCILE)

    def test_accept_output_for_done(self):
        self.check(rp.decide(task(), remote('done', had_output=None), T0 + 1, None), rp.ACCEPT_OUTPUT)

    def test_accept_existing_output(self):
        self.check(rp.decide(task(output='result'), remote('unknown'), T0 + 1, None), rp.ACCEPT_OUTPUT)
        self.check(rp.decide(task(output='result'), None, T0 + 1, None), rp.ACCEPT_OUTPUT)
        self.check(rp.decide(task(), remote('running', had_output=True), T0 + 1, None), rp.ACCEPT_OUTPUT)

    def test_requeue_only_with_proven_error_and_no_output(self):
        self.check(rp.decide(task(), remote('error', False, True), T0 + 1, None), rp.REQUEUE_PROVEN_ERROR)
        self.check(rp.decide(task(), remote('error', False, False), T0 + 1, None), rp.ESCALATE)
        self.check(rp.decide(task(), remote('error', False, None), T0 + 1, None), rp.ESCALATE)
        self.check(rp.decide(task(output='x'), remote('error', False, True), T0 + 1, None), rp.ACCEPT_OUTPUT)

    def test_error_with_had_output_none_escalates(self):
        self.check(rp.decide(task(), remote('error', None, True), T0 + 1, None), rp.ESCALATE)

    def test_error_after_output(self):
        self.check(rp.decide(task(), remote('error', True, False), T0 + 1, None), rp.ACCEPT_OUTPUT)
        self.check(rp.decide(task(), remote('error', True, True), T0 + 1, None), rp.ACCEPT_OUTPUT)
        self.check(rp.decide(task(), remote('error', 1, True), T0 + 1, None), rp.ESCALATE)
        self.check(rp.decide(task(), remote('error', 0, True), T0 + 1, None), rp.ESCALATE)

    def test_running_waits(self):
        self.check(rp.decide(task(), remote('running', False), T0 + 5000, None), rp.WAIT)

    def test_missing_remote_status(self):
        self.check(rp.decide(task(), None, T0 + 100, None), rp.ESCALATE)
        self.check(rp.decide(task(), {}, T0 + 100, None), rp.ESCALATE)
        self.check(rp.decide(None, remote(), T0 + 100, None), rp.ESCALATE)

    def test_bad_timestamp(self):
        for since in (None, 'yesterday', float('nan'), True, T0 + 50):
            self.check(rp.decide(task(uncertain_since=since), remote(), T0, None), rp.ESCALATE)
        self.check(rp.decide(task(), remote(), 'now', None), rp.ESCALATE)

    def test_custom_settings(self):
        s = {'reconcile_after_s': 10, 'escalate_after_s': 60, 'max_reconcile_attempts': 1}
        self.check(rp.decide(task(), remote(), T0 + 9, s), rp.WAIT)
        self.check(rp.decide(task(), remote(), T0 + 10, s), rp.RECONCILE)
        self.check(rp.decide(task(reconcile_attempts=1), remote(), T0 + 10, s), rp.ESCALATE)
        self.check(rp.decide(task(), remote(), T0 + 60, SimpleNamespace(**s)), rp.ESCALATE)
        self.check(rp.decide(task(), remote(), T0 + 100, {'reconcile_after_s': -1}), rp.ESCALATE)
        self.check(rp.decide(task(), remote(), T0 + 100, {'reconcile_after_s': 1000}), rp.ESCALATE)

    def test_unexpected_states_escalate(self):
        self.check(rp.decide(task(state='done'), remote(), T0 + 100, None), rp.ESCALATE)
        self.check(rp.decide(task(state='blocked'), remote(), T0 + 100, None), rp.ESCALATE)
        self.check(rp.decide(task(state='blocked', remote_unknown=True), remote(), T0 + 100, None), rp.RECONCILE)
        self.check(rp.decide(task(), remote('weird'), T0 + 100, None), rp.ESCALATE)
        self.check(rp.decide(task(), remote(None), T0 + 100, None), rp.ESCALATE)

    def test_attribute_objects_are_supported(self):
        t = SimpleNamespace(state='uncertain', uncertain_since=T0, reconcile_attempts=0, output=None)
        r = SimpleNamespace(state='error', had_output=False, error_before_output=True)
        self.check(rp.decide(t, r, T0 + 1, None), rp.REQUEUE_PROVEN_ERROR)

    def test_sweep_never_requeues_without_proof(self):
        task_states = ['uncertain', 'remote_unknown', 'blocked', 'running', None, 'done', 7]
        remote_states = list(rp.REMOTE_STATES) + [None, 'ERROR', '', 3]
        tri = [True, False, None, 0, 1, 'yes']
        outputs = [None, '', '   ', 'out', [], ['x']]
        attempts = [None, 0, 2, 3, 9, -1, 'x']
        elapsed = [-5, 0, 89, 90, 899, 900, 10000, None]
        settings_list = [None, {}, {'reconcile_after_s': 0, 'escalate_after_s': 0, 'max_reconcile_attempts': 0}, {'max_reconcile_attempts': 'x'}]
        count = 0
        for ts, rs, ho, ebo, out, at, el, st in itertools.product(
                task_states, remote_states, tri, tri, outputs, attempts, elapsed, settings_list):
            t = {'state': ts, 'reconcile_attempts': at, 'output': out, 'remote_unknown': True,
                 'uncertain_since': None if el is None else T0 - el}
            r = {'state': rs, 'had_output': ho, 'error_before_output': ebo}
            action, reason = rp.decide(t, r, T0, st)
            count += 1
            self.assertIn(action, rp.ACTIONS)
            self.assertTrue(isinstance(reason, str) and reason.strip())
            if action == rp.REQUEUE_PROVEN_ERROR:
                self.assertEqual(rs, 'error')
                self.assertIs(ebo, True)
                self.assertIs(ho, False)
                self.assertFalse(isinstance(out, str) and out.strip() or isinstance(out, list) and out)
        for at, el in itertools.product(attempts, elapsed):
            t = {'state': 'uncertain', 'reconcile_attempts': at, 'output': None,
                 'uncertain_since': None if el is None else T0 - el}
            self.assertNotEqual(rp.decide(t, None, T0, None)[0], rp.REQUEUE_PROVEN_ERROR)
        self.assertGreater(count, 100000)

    def test_exception_in_task_object_escalates(self):
        class Broken:
            def __getattr__(self, name):
                raise RuntimeError('boom')
        self.check(rp.decide(Broken(), remote('error', False, True), T0, None), rp.ESCALATE)


if __name__ == '__main__':
    unittest.main()
