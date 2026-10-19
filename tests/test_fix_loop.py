import copy
import unittest

from notiontool import fix_loop
from notiontool.fix_loop import decide, new_state

GUARD = 20  # hard upper bound on decide() calls in every loop test


def verdict(failed, task='t1', cost=1, passed=False, extra_defects=None):
    defects = [dict(criterion_id=cid, task_id=task, detail=f'{cid} failed') for cid in failed]
    defects += extra_defects or []
    return dict(passed=passed, failed_criteria=list(failed) + [d['criterion_id'] for d in extra_defects or []],
                defects=defects, checks_run=[], cost=cost)


def run_loop(verdicts, limits=None, times=None):
    """Drive decide() with a verdict factory; returns list of actions. Explicit loop guard."""
    state = new_state(limits)
    actions = []
    for i in range(GUARD):
        now = times(i) if times else None
        action, reason, state = decide(state, verdicts(i), now=now)
        actions.append((action, reason))
        if action != 'fix':
            return actions, state
    raise AssertionError(f'loop guard hit after {GUARD} decide calls: {actions[-3:]}')


class DecideBasics(unittest.TestCase):
    def test_passed_verdict_is_done(self):
        action, reason, _ = decide(new_state(), dict(passed=True, failed_criteria=[], defects=[], cost=1))
        self.assertEqual(action, 'done')
        self.assertIn('passed', reason)

    def test_failing_verdict_gives_fix_with_reason(self):
        action, reason, state = decide(new_state(), verdict(['c1']))
        self.assertEqual(action, 'fix')
        self.assertTrue(reason)
        self.assertEqual(state['fixes'], {'t1': [{'criterion_id': 'c1', 'detail': 'c1 failed'}]})

    def test_passed_tasks_never_appear_in_fix_list(self):
        v = dict(passed=False, failed_criteria=['c1', 'c2'], cost=1, passed_tasks=['t_ok'],
                 defects=[dict(criterion_id='c1', task_id='t_bad', detail='x'),
                          dict(criterion_id='c2', task_id='t_ok', detail='stale')])
        action, _, state = decide(new_state(), v)
        self.assertEqual(action, 'fix')
        self.assertEqual(list(state['fixes']), ['t_bad'])
        self.assertNotIn('t_ok', state['fixes'])
        self.assertNotIn('t_other', state['fixes'])

    def test_defects_grouped_by_task(self):
        v = dict(passed=False, failed_criteria=['a', 'b', 'c'], cost=1, defects=[
            dict(criterion_id='a', task_id='t1', detail='1'),
            dict(criterion_id='b', task_id='t2', detail='2'),
            dict(criterion_id='c', task_id='t1', detail='3')])
        _, _, state = decide(new_state(), v)
        self.assertEqual(sorted(state['fixes']), ['t1', 't2'])
        self.assertEqual([d['criterion_id'] for d in state['fixes']['t1']], ['a', 'c'])

    def test_input_state_and_verdict_not_mutated(self):
        state = new_state()
        v = verdict(['c1'])
        s_before, v_before = copy.deepcopy(state), copy.deepcopy(v)
        _, _, new = decide(state, v, now=10)
        self.assertEqual(state, s_before)
        self.assertEqual(v, v_before)
        self.assertIsNot(new, state)

    def test_improvement_resets_counter_and_fix_continues(self):
        actions, _ = run_loop(lambda i: verdict(['c1', 'c2', 'c3'][: 3 - i]) if i < 3 else
                              dict(passed=True, defects=[], failed_criteria=[], cost=1))
        self.assertEqual([a for a, _ in actions], ['fix', 'fix', 'fix', 'done'])

    def test_malformed_verdict_stops(self):
        for bad in (None, 'x', {}, dict(passed='yes')):
            action, reason, _ = decide(new_state(), bad)
            self.assertEqual(action, 'stop', bad)
            self.assertIn('malformed', reason)

    def test_finished_state_stops(self):
        _, _, state = decide(new_state(), dict(passed=True, defects=[], failed_criteria=[], cost=0))
        action, reason, _ = decide(state, verdict(['c1']))
        self.assertEqual(action, 'stop')
        self.assertIn('already finished', reason)

    def test_failure_without_task_escalates(self):
        v = dict(passed=False, failed_criteria=['c1'], defects=[dict(criterion_id='c1', detail='x')], cost=1)
        action, reason, _ = decide(new_state(), v)
        self.assertEqual(action, 'escalate')
        self.assertIn('no defect names a task', reason)

    def test_bad_limits_fall_back_to_defaults(self):
        limits = fix_loop.normalise_limits({'max_iterations': 0, 'request_budget': -3, 'time_budget_s': True})
        self.assertEqual(limits, fix_loop.DEFAULT_LIMITS)


class StopRules(unittest.TestCase):
    def test_max_iterations_escalates(self):
        n = [5]
        def improving(i):
            n[0] = max(n[0] - 1, 0)
            return verdict([f'c{k}' for k in range(n[0] + 1)])
        actions, state = run_loop(improving, limits={'max_iterations': 3})
        self.assertEqual(actions[-1][0], 'escalate')
        self.assertIn('max iterations', actions[-1][1])
        self.assertEqual(state['iteration'], 3)

    def test_request_budget_counted_from_cost(self):
        actions, state = run_loop(lambda i: verdict([f'c{k}' for k in range(9 - i)], cost=4),
                                  limits={'request_budget': 10})
        self.assertEqual(actions[-1][0], 'escalate')
        self.assertIn('request budget', actions[-1][1])
        self.assertEqual(state['requests_used'], 12)

    def test_time_budget(self):
        actions, _ = run_loop(lambda i: verdict([f'c{k}' for k in range(9 - i)]),
                              limits={'time_budget_s': 100}, times=lambda i: 1000 + i * 60)
        self.assertEqual(actions[-1][0], 'escalate')
        self.assertIn('time budget', actions[-1][1])
        self.assertEqual(len(actions), 3)

    def test_two_rounds_without_improvement(self):
        actions, _ = run_loop(lambda i: verdict(['c1', 'c2']))
        self.assertEqual([a for a, _ in actions], ['fix', 'fix', 'escalate'])
        self.assertIn('no improvement for 2 rounds', actions[-1][1])


CASCADE = [('tests', 'c_tests'), ('static', 'c_static'), ('diff', 'c_diff'), ('requirements', 'c_req')]


def cascade_verdict(round_no, task='t1'):
    """Verifier-shaped verdict: checks run in cascade order, stop at the first failure."""
    if round_no >= len(CASCADE):
        return dict(passed=True, failed_criteria=[], defects=[],
                    checks_run=[f'{m}:{c}' for m, c in CASCADE], cost=1)
    method, cid = CASCADE[round_no]
    return dict(passed=False, failed_criteria=[cid],
                defects=[dict(criterion_id=cid, task_id=task, detail=f'{method} check failed')],
                checks_run=[f'{m}:{c}' for m, c in CASCADE[: round_no + 1]], cost=1)


class ImprovementRule(unittest.TestCase):
    def test_moving_failure_reaches_done(self):
        actions, state = run_loop(cascade_verdict)
        self.assertEqual([a for a, _ in actions], ['fix', 'fix', 'fix', 'fix', 'done'])
        for (_, reason), cid in zip(actions[1:4], ['c_tests', 'c_static', 'c_diff']):
            self.assertIn('newly passed ' + cid, reason)
        self.assertEqual(state['last_action'], 'done')
        self.assertEqual(state['iteration'], 5)

    def test_same_single_criterion_three_times_escalates(self):
        same = dict(passed=False, failed_criteria=['c_tests'], checks_run=['tests:c_tests'], cost=1,
                    defects=[dict(criterion_id='c_tests', task_id='t1', detail='tests check failed')])
        actions, state = run_loop(lambda i: copy.deepcopy(same))
        self.assertEqual([a for a, _ in actions], ['fix', 'fix', 'escalate'])
        self.assertIn('no improvement for 2 rounds', actions[-1][1])
        self.assertEqual(state['iteration'], 3)

    def test_fewer_failures_for_one_task_counts(self):
        def v(i):
            t1 = ['a', 'b'] if i == 0 else ['a']
            t2 = ['x'] if i == 0 else ['x', 'y']  # total stays at 3
            defects = [dict(criterion_id=c, task_id='t1', detail='') for c in t1]
            defects += [dict(criterion_id=c, task_id='t2', detail='') for c in t2]
            return dict(passed=False, failed_criteria=t1 + t2, defects=defects, checks_run=[], cost=1)
        action, reason, state = decide(new_state(), v(0))
        action, reason, state = decide(state, v(1))
        self.assertEqual(action, 'fix')
        self.assertIn('fewer failures for t1', reason)
        self.assertEqual(state['rounds_without_improvement'], 0)

    def test_passed_set_oscillation_still_bounded(self):
        def v(i):
            cid, other = ('a', 'b') if i % 2 == 0 else ('b', 'a')
            return dict(passed=False, failed_criteria=[cid], checks_run=[f'tests:{other}', f'static:{cid}'],
                        defects=[dict(criterion_id=cid, task_id='t1', detail='')], cost=1)
        actions, _ = run_loop(v, limits={'max_iterations': 100})
        self.assertEqual(actions[-1][0], 'escalate')
        self.assertLessEqual(len(actions), 5)

    def test_passed_ids_reads_strings_and_dicts(self):
        v = dict(failed_criteria=['c2'], defects=[],
                 checks_run=['tests:c1', 'static:c2', {'criterion_id': 'c3'}, 'diff:ns:c4', 7, ''])
        self.assertEqual(fix_loop.passed_ids(v), {'c1', 'c3', 'ns:c4'})


class FaultInjection(unittest.TestCase):
    """Every scenario must end in stop or escalate within GUARD decide calls."""

    def assert_terminates(self, actions):
        self.assertIn(actions[-1][0], ('stop', 'escalate'))
        self.assertLessEqual(len(actions), GUARD)

    def test_always_failing(self):
        actions, _ = run_loop(lambda i: verdict(['c1']))
        self.assert_terminates(actions)

    def test_oscillating_failure_counts(self):
        actions, _ = run_loop(lambda i: verdict(['c1', 'c2', 'c3'] if i % 2 == 0 else ['c1', 'c2']),
                              limits={'max_iterations': 100})
        self.assert_terminates(actions)
        self.assertIn('no improvement', actions[-1][1])

    def test_growing_defect_list(self):
        actions, _ = run_loop(lambda i: verdict([f'c{k}' for k in range(i + 1)]),
                              limits={'max_iterations': 100})
        self.assert_terminates(actions)

    def test_cost_zero_still_bounded(self):
        actions, state = run_loop(lambda i: verdict([f'c{k}' for k in range(50 - i)], cost=0),
                                  limits={'max_iterations': 6})
        self.assert_terminates(actions)
        self.assertEqual(state['requests_used'], 0)
        self.assertEqual(len(actions), 6)

    def test_missing_time_still_bounded(self):
        actions, state = run_loop(lambda i: verdict([f'c{k}' for k in range(50 - i)]),
                                  limits={'time_budget_s': 1}, times=lambda i: None)
        self.assert_terminates(actions)
        self.assertIsNone(state['elapsed_s'])
        self.assertIn('max iterations', actions[-1][1])

    def test_garbage_cost_and_time_still_bounded(self):
        actions, _ = run_loop(lambda i: verdict([f'c{k}' for k in range(50 - i)], cost='lots'),
                              times=lambda i: 'soon')
        self.assert_terminates(actions)

    def test_garbage_checks_run_still_bounded(self):
        def v(i):
            out = verdict(['c1'])
            out['checks_run'] = [None, {'x': 1}, 3.5, f'tests:new{i}']  # a new passed id every round
            return out
        actions, _ = run_loop(v, limits={'max_iterations': 7})
        self.assert_terminates(actions)
        self.assertIn('max iterations', actions[-1][1])


if __name__ == '__main__':
    unittest.main()
