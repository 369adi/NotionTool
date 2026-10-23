import unittest

from notiontool.main_wake import RED_HEAD, build_red_message, red_blocked


def task(tid, red='abc123', state='waiting', at=0.0, run='r1'):
    return dict(id=tid, run_id=run, title='Merge lane ' + tid, patch_state=state, patch_main_red=red,
                patch_result=dict(status='main_red', at=at, failing=['tests.test_x.T.test_y']))


RUNS = {'r1': dict(id='r1', operator=True), 'r2': dict(id='r2', operator=False)}


class RedBlockedTest(unittest.TestCase):
    def test_waiting_long_enough_is_reported(self):
        out = red_blocked([task('t1', at=0.0)], RUNS, set(), now=400.0)
        self.assertEqual(list(out), ['abc123'])
        self.assertEqual(out['abc123'][0][1]['id'], 't1')

    def test_fresh_wait_is_not_reported_yet(self):
        self.assertEqual(red_blocked([task('t1', at=200.0)], RUNS, set(), now=400.0), {})

    def test_already_alerted_commit_is_skipped(self):
        self.assertEqual(red_blocked([task('t1')], RUNS, {'abc123'}, now=1000.0), {})

    def test_only_waiting_operator_tasks(self):
        tasks = [task('t1', state='merged'), task('t2', red=''), task('t3', run='r2'), task('t4', run='gone')]
        self.assertEqual(red_blocked(tasks, RUNS, set(), now=1000.0), {})

    def test_tasks_on_the_same_red_commit_are_grouped(self):
        out = red_blocked([task('t1'), task('t2')], RUNS, set(), now=1000.0)
        self.assertEqual([t['id'] for _, t in out['abc123']], ['t1', 't2'])

    def test_message_names_commit_tasks_and_failing_tests(self):
        items = [(RUNS['r1'], task('t1')), (RUNS['r1'], task('t2'))]
        text = build_red_message('abc123def456789', items)
        self.assertTrue(text.startswith(RED_HEAD))
        self.assertIn('abc123def4', text)
        self.assertIn('t1', text)
        self.assertIn('t2', text)
        self.assertEqual(text.count('tests.test_x.T.test_y'), 1)


if __name__ == '__main__':
    unittest.main()
