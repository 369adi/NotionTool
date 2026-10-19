import copy
import unittest
from unittest import mock

from notiontool import fix_loop
from notiontool.verifier import merge_verdicts, run_verifier


def crit(cid, method, required=True, **extra):
    return dict(id=cid, text=f'{cid} text', method=method, target=[], required=required, **extra)


def passing():
    return mock.Mock(return_value=True)


def failing(detail='broken'):
    return mock.Mock(return_value=dict(passed=False, detail=detail))


ALL = [crit('m', 'model_review'), crit('s', 'screenshot'), crit('r', 'requirements'),
       crit('d', 'diff'), crit('st', 'static'), crit('t', 'tests')]


class VerifierTest(unittest.TestCase):
    def test_order_is_cheapest_first(self):
        checks = {m: passing() for m in ('tests', 'static', 'diff', 'requirements')}
        verdict = run_verifier('T2', ALL, checks, 'high', capture_verify=passing(), model_review=passing())
        self.assertEqual(verdict['checks_run'], ['tests:t', 'static:st', 'diff:d', 'requirements:r',
                                                 'screenshot:s', 'model_review:m'])
        self.assertTrue(verdict['passed'])

    def test_stops_at_first_failure(self):
        checks = dict(tests=passing(), static=failing('lint error'), diff=passing(), requirements=passing())
        capture, review = passing(), passing()
        verdict = run_verifier('T2', ALL, checks, 'high', capture_verify=capture, model_review=review)
        self.assertFalse(verdict['passed'])
        self.assertEqual(verdict['checks_run'], ['tests:t', 'static:st'])
        self.assertEqual(verdict['failed_criteria'], ['st'])
        self.assertEqual(verdict['defects'], [dict(criterion_id='st', task_id='T2', detail='lint error')])
        checks['diff'].assert_not_called()
        capture.assert_not_called()
        review.assert_not_called()
        self.assertIn('model_review:m', verdict['skipped'])

    def test_model_pass_never_overrides_objective_fail(self):
        review = mock.Mock(return_value=dict(passed=True, detail='looks great'))
        verdict = run_verifier('T2', [crit('t', 'tests'), crit('m', 'model_review')],
                               dict(tests=failing()), 'high', model_review=review)
        self.assertFalse(verdict['passed'])
        review.assert_not_called()
        self.assertEqual(verdict['failed_criteria'], ['t'])

    def test_model_fail_after_objective_pass_fails(self):
        verdict = run_verifier('T2', [crit('t', 'tests'), crit('m', 'model_review')],
                               dict(tests=passing()), 'high', model_review=failing('unclear naming'))
        self.assertFalse(verdict['passed'])
        self.assertEqual(verdict['defects'][0]['detail'], 'unclear naming')

    def test_model_review_skipped_at_low_risk(self):
        review = passing()
        verdict = run_verifier('T2', [crit('t', 'tests'), crit('m', 'model_review')],
                               dict(tests=passing()), 'low', model_review=review)
        review.assert_not_called()
        self.assertTrue(verdict['passed'])
        self.assertEqual(verdict['checks_run'], ['tests:t'])
        self.assertEqual(verdict['skipped'], ['model_review:m'])

    def test_model_review_gets_read_only_fresh_context(self):
        seen = {}

        def review(context):
            seen['context'] = context
            with self.assertRaises(TypeError):
                context['task_id'] = 'other'
            return True

        criteria = [crit('t', 'tests')]
        verdict = run_verifier('T2', criteria, dict(tests=passing()), 'HIGH', model_review=review)
        self.assertTrue(verdict['passed'])
        self.assertEqual(verdict['checks_run'], ['tests:t', 'model_review:model_review'])
        self.assertEqual(seen['context']['task_id'], 'T2')
        self.assertEqual(seen['context']['objective_results'][0]['passed'], True)
        self.assertEqual(criteria, [crit('t', 'tests')])

    def test_screenshot_through_injected_capture_verify(self):
        capture = mock.Mock(return_value=dict(changed=False, distance=0, diff_ratio=0.0,
                                              summary='No visible change'))
        fallback = passing()
        verdict = run_verifier('T2', [crit('s', 'screenshot')], dict(screenshot=fallback), 'low',
                               capture_verify=capture)
        capture.assert_called_once()
        fallback.assert_not_called()
        self.assertFalse(verdict['passed'])
        self.assertEqual(verdict['defects'][0]['detail'], 'No visible change')
        ok = run_verifier('T2', [crit('s', 'screenshot', expect_change=False)], {}, 'low', capture_verify=capture)
        self.assertTrue(ok['passed'])

    def test_missing_check_is_defect_not_crash(self):
        verdict = run_verifier('T2', [crit('d', 'diff'), crit('t', 'tests')], dict(tests=passing()), 'low')
        self.assertFalse(verdict['passed'])
        self.assertEqual(verdict['checks_run'], ['tests:t', 'diff:d'])
        self.assertIn("no check registered for method 'diff'", verdict['defects'][0]['detail'])

    def test_raising_check_counts_as_failure(self):
        boom = mock.Mock(side_effect=RuntimeError('disk on fire'))
        verdict = run_verifier('T2', [crit('t', 'tests'), crit('st', 'static')],
                               dict(tests=boom, static=passing()), 'low')
        self.assertFalse(verdict['passed'])
        self.assertEqual(verdict['defects'][0]['detail'], 'check raised RuntimeError: disk on fire')
        self.assertEqual(verdict['checks_run'], ['tests:t'])
        self.assertEqual(verdict['cost'], 1)

    def test_cost_sum_with_default(self):
        checks = dict(tests=mock.Mock(return_value=dict(passed=True, cost=5)),
                      static=passing(),
                      diff=mock.Mock(return_value=dict(passed=True, cost=2.5)))
        verdict = run_verifier('T2', [crit('t', 'tests'), crit('st', 'static'), crit('d', 'diff')],
                               checks, 'low')
        self.assertEqual(verdict['cost'], 8.5)

    def test_all_pass_verdict_shape(self):
        checks = {m: passing() for m in ('tests', 'static', 'diff', 'requirements')}
        verdict = run_verifier('T2', ALL[1:], checks, 'low', capture_verify=passing())
        self.assertEqual(verdict, dict(passed=True, failed_criteria=[], defects=[],
                                       checks_run=['tests:t', 'static:st', 'diff:d', 'requirements:r',
                                                   'screenshot:s'],
                                       cost=5, skipped=[]))

    def test_optional_failure_does_not_stop_or_fail(self):
        verdict = run_verifier('T2', [crit('t', 'tests', required=False), crit('st', 'static')],
                               dict(tests=failing('flaky'), static=passing()), 'low')
        self.assertTrue(verdict['passed'])
        self.assertEqual(verdict['checks_run'], ['tests:t', 'static:st'])
        self.assertEqual(verdict['failed_criteria'], ['t'])

    def test_malformed_input_is_defect(self):
        self.assertFalse(run_verifier('T2', [], {}, 'low')['passed'])
        verdict = run_verifier('T2', [crit('x', 'magic'), 'junk'], {}, 'low')
        self.assertFalse(verdict['passed'])
        self.assertEqual(verdict['failed_criteria'], ['x', 'c2'])
        self.assertEqual(verdict['checks_run'], [])

    def test_high_risk_without_model_callable_is_defect(self):
        verdict = run_verifier('T2', [crit('t', 'tests'), crit('m', 'model_review')],
                               dict(tests=passing()), 'high')
        self.assertFalse(verdict['passed'])
        self.assertEqual(verdict['failed_criteria'], ['m'])


class MergeVerdictsTest(unittest.TestCase):
    def verdicts(self):
        good = run_verifier('T1', [crit('t', 'tests')], dict(tests=passing()), 'low')
        bad_static = run_verifier('T2', [crit('t', 'tests'), crit('st', 'static')],
                                  dict(tests=passing(), static=failing('lint error')), 'low')
        bad_tests = run_verifier('T3', [crit('t', 'tests')],
                                 dict(tests=mock.Mock(return_value=dict(passed=False, detail='2 failed', cost=3))),
                                 'low')
        return good, bad_static, bad_tests

    def test_merge_all_passed(self):
        good = run_verifier('T1', [crit('t', 'tests')], dict(tests=passing()), 'low')
        other = run_verifier('T2', [crit('st', 'static')], dict(static=passing()), 'low')
        merged = merge_verdicts({'T1': good, 'T2': other})
        self.assertTrue(merged['passed'])
        self.assertEqual(merged['passed_tasks'], ['T1', 'T2'])
        self.assertEqual(merged['failed_criteria'], [])
        self.assertEqual(merged['defects'], [])
        self.assertEqual(merged['checks_run'], ['tests:t', 'static:st'])
        self.assertEqual(merged['cost'], 2)

    def test_merge_mixed_combines_and_sums(self):
        good, bad_static, bad_tests = self.verdicts()
        originals = copy.deepcopy([good, bad_static, bad_tests])
        merged = merge_verdicts([('T1', good), ('T2', bad_static), ('T3', bad_tests)])
        self.assertFalse(merged['passed'])
        self.assertEqual(merged['passed_tasks'], ['T1'])
        self.assertEqual(merged['failed_criteria'], ['st', 't'])
        self.assertEqual([(d['task_id'], d['criterion_id']) for d in merged['defects']],
                         [('T2', 'st'), ('T3', 't')])
        self.assertEqual(merged['cost'], 1 + 2 + 3)
        self.assertEqual(merged['checks_run'], ['tests:t', 'tests:t', 'static:st', 'tests:t'])
        self.assertEqual([good, bad_static, bad_tests], originals)

    def test_merged_verdict_feeds_fix_loop_only_failed_tasks(self):
        merged = merge_verdicts([('T1', v) if i == 0 else v for i, v in enumerate(self.verdicts())])
        action, reason, state = fix_loop.decide(fix_loop.new_state(), merged)
        self.assertEqual(action, 'fix', reason)
        self.assertEqual(set(state['fixes']), {'T2', 'T3'})
        self.assertNotIn('T1', state['fixes'])
        self.assertEqual(state['fixes']['T2'], [dict(criterion_id='st', detail='lint error')])

    def test_merge_empty_input(self):
        for empty in ([], (), {}, None, 'junk'):
            merged = merge_verdicts(empty)
            self.assertFalse(merged['passed'])
            self.assertEqual(merged['passed_tasks'], [])
            self.assertEqual(merged['cost'], 0)
            self.assertEqual(merged['checks_run'], [])
            self.assertEqual(merged['failed_criteria'], ['none'])
            self.assertIsNone(merged['defects'][0]['task_id'])
        action, _, _ = fix_loop.decide(fix_loop.new_state(), merge_verdicts([]))
        self.assertEqual(action, 'escalate')

    def test_merge_malformed_and_missing_skipped(self):
        minimal = dict(passed=True, failed_criteria=[], defects=[], checks_run=['tests:t'], cost=1, task_id='T4')
        merged = merge_verdicts([minimal, 'junk', ('T5', dict(failed_criteria=[]))])
        self.assertFalse(merged['passed'])
        self.assertEqual(merged['passed_tasks'], ['T4'])
        self.assertEqual(merged['skipped'], [])
        self.assertEqual(merged['failed_criteria'], ['verdict2', 'verdict3'])
        self.assertEqual(merged['defects'][1]['task_id'], 'T5')
        self.assertEqual(merged['cost'], 1)


if __name__ == '__main__':
    unittest.main()
