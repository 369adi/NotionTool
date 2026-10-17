import copy
import unittest

from notiontool import dag
from notiontool import file_leases as fl

T0 = 1000.0


class AcquireTests(unittest.TestCase):
    def test_live_states_are_exact(self):
        self.assertEqual(fl.LIVE_TASK_STATES, {'running', 'cancelling', 'uncertain', 'checkpoint', 'preparing'})

    def test_free_path_is_granted(self):
        r = fl.acquire({}, 'notiontool/a.py', 't1', T0, 'Cowork 1')
        self.assertTrue(r['granted'])
        self.assertEqual(r['status'], 'granted')
        self.assertFalse(r['renewed'])
        self.assertEqual(r['lease']['task_id'], 't1')
        self.assertEqual(r['lease']['expires'], T0 + 60)
        self.assertIn('notiontool/a.py', r['leases'])

    def test_same_task_renews_and_keeps_since(self):
        first = fl.acquire({}, 'a.py', 't1', T0, 'Cowork 1')['leases']
        r = fl.acquire(first, 'a.py', 't1', T0 + 30, 'Cowork 1')
        self.assertTrue(r['granted'])
        self.assertTrue(r['renewed'])
        self.assertEqual(r['lease']['since'], T0)
        self.assertEqual(r['lease']['expires'], T0 + 30 + 60)

    def test_other_task_gets_busy_with_holder(self):
        leases = fl.acquire({}, 'a.py', 't2', T0, 'Cowork 2')['leases']
        r = fl.acquire(leases, 'a.py', 't1', T0 + 12, 'Cowork 1')
        self.assertFalse(r['granted'])
        self.assertEqual(r['status'], 'busy')
        self.assertEqual(r['holder_task'], 't2')
        self.assertEqual(r['holder_label'], 'Cowork 2')
        self.assertEqual(r['since_s'], 12)
        self.assertGreaterEqual(r['wait_hint_s'], 1)
        self.assertEqual(r['leases'], leases)

    def test_expired_lease_never_blocks(self):
        leases = fl.acquire({}, 'a.py', 't2', T0, 'Cowork 2')['leases']
        r = fl.acquire(leases, 'a.py', 't1', T0 + 61, 'Cowork 1')
        self.assertTrue(r['granted'])
        self.assertEqual(r['lease']['task_id'], 't1')
        self.assertEqual(r['lease']['since'], T0 + 61)

    def test_stale_15_min_ttl_no_longer_blocks(self):
        # file_registry used TTL = 15 * 60; a lease now lasts at most 60 s.
        leases = fl.acquire({}, 'a.py', 't2', T0, 'Cowork 2', settings={'file_lease_s': 15 * 60})['leases']
        self.assertEqual(leases['a.py']['expires'], T0 + fl.MAX_LEASE_S)
        r = fl.acquire(leases, 'a.py', 't1', T0 + 2 * 60, 'Cowork 1')
        self.assertTrue(r['granted'])

    def test_lease_seconds_setting(self):
        self.assertEqual(fl.lease_seconds(None), 60)
        self.assertEqual(fl.lease_seconds({'file_lease_s': 10}), 10)
        self.assertEqual(fl.lease_seconds({'file_lease_s': 900}), 60)
        self.assertEqual(fl.lease_seconds({'file_lease_s': 'x'}), 60)
        self.assertEqual(fl.lease_seconds({'file_lease_s': -5}), 60)
        self.assertEqual(fl.lease_seconds({'file_lease_s': True}), 60)
        r = fl.acquire({}, 'a.py', 't1', T0, settings={'file_lease_s': 10})
        self.assertEqual(r['lease']['expires'], T0 + 10)

    def test_path_spellings_match_one_key(self):
        leases = fl.acquire({}, 'NotionTool\\Engine.PY', 't2', T0, 'Cowork 2')['leases']
        for spelling in ('notiontool/engine.py', './notiontool//engine.py', 'notiontool/x/../engine.py', 'NOTIONTOOL/ENGINE.PY'):
            r = fl.acquire(leases, spelling, 't1', T0 + 1, 'Cowork 1')
            self.assertEqual(r['status'], 'busy', spelling)
        self.assertEqual(fl.normalize_key('C:\\Users\\A\\b.py'), 'c:/users/a/b.py')

    def test_inputs_are_not_changed(self):
        leases = fl.acquire({}, 'a.py', 't2', T0, 'Cowork 2')['leases']
        before = copy.deepcopy(leases)
        fl.acquire(leases, 'b.py', 't1', T0)
        fl.acquire(leases, 'a.py', 't2', T0 + 5)
        fl.release(leases, 'a.py', 't2')
        fl.release_for_tasks(leases, {})
        self.assertEqual(leases, before)


class ReleaseTests(unittest.TestCase):
    def test_busy_then_free_after_release(self):
        leases = fl.acquire({}, 'a.py', 't2', T0, 'Cowork 2')['leases']
        self.assertEqual(fl.acquire(leases, 'a.py', 't1', T0 + 1)['status'], 'busy')
        r = fl.release(leases, 'a.py', 't2')
        self.assertTrue(r['released'])
        self.assertEqual(fl.acquire(r['leases'], 'a.py', 't1', T0 + 2)['status'], 'granted')

    def test_release_only_removes_own_lease(self):
        leases = fl.acquire({}, 'a.py', 't2', T0, 'Cowork 2')['leases']
        r = fl.release(leases, 'A.py', 't1')
        self.assertFalse(r['released'])
        self.assertEqual(r['reason'], 'not_owner')
        self.assertIn('a.py', r['leases'])
        self.assertEqual(fl.release({}, 'a.py', 't1')['reason'], 'no_lease')

    def test_finished_failed_and_stalled_tasks_release_their_files(self):
        leases = {}
        for path, task in (('a.py', 'done1'), ('b.py', 'fail1'), ('c.py', 'stall1'), ('d.py', 'run1'), ('e.py', 'prep1')):
            leases = fl.acquire(leases, path, task, T0, task)['leases']
        states = {'done1': 'done', 'fail1': 'failed', 'stall1': 'stalled', 'run1': 'running', 'prep1': 'preparing'}
        r = fl.release_for_tasks(leases, states)
        self.assertEqual(r['freed'], ['a.py', 'b.py', 'c.py'])
        self.assertEqual(sorted(r['leases']), ['d.py', 'e.py'])

    def test_every_live_state_keeps_its_lease(self):
        leases, states = {}, {}
        for i, state in enumerate(sorted(fl.LIVE_TASK_STATES)):
            leases = fl.acquire(leases, 'f%d.py' % i, 't%d' % i, T0)['leases']
            states['t%d' % i] = state
        self.assertEqual(fl.release_for_tasks(leases, states)['freed'], [])

    def test_unknown_task_is_released(self):
        leases = fl.acquire({}, 'a.py', 'ghost', T0)['leases']
        r = fl.release_for_tasks(leases, {'other': 'running'})
        self.assertEqual(r['freed'], ['a.py'])
        self.assertEqual(r['leases'], {})

    def test_release_for_tasks_with_now_drops_expired(self):
        leases = fl.acquire({}, 'a.py', 't1', T0)['leases']
        self.assertEqual(fl.release_for_tasks(leases, {'t1': 'running'}, now=T0 + 61)['freed'], ['a.py'])
        self.assertEqual(fl.prune(leases, T0 + 61)['freed'], ['a.py'])
        self.assertEqual(fl.prune(leases, T0 + 1)['freed'], [])


class MainHolderTests(unittest.TestCase):
    def test_main_holds_the_same_lease(self):
        r = fl.acquire_main({}, 'notiontool/engine.py', T0)
        self.assertTrue(r['granted'])
        self.assertEqual(r['lease']['task_id'], fl.MAIN_TASK)
        self.assertEqual(r['lease']['label'], 'Main')
        busy = fl.acquire(r['leases'], 'notiontool/engine.py', 't1', T0 + 3, 'Cowork 1')
        self.assertEqual(busy['status'], 'busy')
        self.assertEqual(busy['holder_label'], 'Main')
        self.assertEqual(busy['holder_task'], 'main')

    def test_main_lease_survives_task_release_until_expiry(self):
        leases = fl.acquire_main({}, 'a.py', T0)['leases']
        self.assertEqual(fl.release_for_tasks(leases, {})['freed'], [])
        self.assertEqual(fl.release_for_tasks(leases, {}, now=T0 + 61)['freed'], ['a.py'])

    def test_coworker_lease_blocks_main(self):
        leases = fl.acquire({}, 'a.py', 't2', T0, 'Cowork 2')['leases']
        r = fl.acquire_main(leases, 'a.py', T0 + 4)
        self.assertEqual(r['status'], 'busy')
        self.assertEqual(r['holder_label'], 'Cowork 2')


class CheckWriteTests(unittest.TestCase):
    def test_unchanged_hash_is_ok(self):
        r = fl.check_write('a.py', 'ABC123', 'abc123')
        self.assertTrue(r['ok'])
        self.assertEqual(r['status'], 'ok')

    def test_changed_hash_is_a_conflict_with_clear_text(self):
        r = fl.check_write('notiontool\\a.py', 'aaa', 'bbb')
        self.assertFalse(r['ok'])
        self.assertEqual(r['status'], 'conflict')
        self.assertEqual(r['reason'], 'changed')
        self.assertEqual(r['message'], 'File notiontool/a.py changed since you read it. Read it again and apply your change on top of the new version; never overwrite it.')

    def test_new_file_ok_and_unread_existing_file_conflicts(self):
        self.assertTrue(fl.check_write('new.py', None, None)['ok'])
        r = fl.check_write('a.py', None, 'bbb')
        self.assertEqual(r['reason'], 'not_read')
        self.assertIn('never overwrite', r['message'])
        self.assertEqual(fl.check_write('a.py', 'aaa', None)['reason'], 'deleted')


class BusyMessageTests(unittest.TestCase):
    def test_exact_busy_text(self):
        r = fl.busy_message('notiontool/engine.py', 'Cowork 2', 12.4, 5)
        self.assertEqual(r['text'], 'File notiontool/engine.py is being written by Cowork 2 right now (about 12 s). Wait a few seconds and try again.')
        self.assertEqual(r['wait_hint_s'], 5)

    def test_busy_message_from_acquire_result(self):
        leases = fl.acquire({}, 'a.py', 'main', T0, 'Main')['leases']
        busy = fl.acquire(leases, 'a.py', 't1', T0 + 7)
        r = fl.busy_message_for(busy, 'a.py')
        self.assertEqual(r['text'], 'File a.py is being written by Main right now (about 7 s). Wait a few seconds and try again.')
        self.assertEqual(r['wait_hint_s'], 5)
        self.assertEqual(fl.busy_message('a.py', 'Main', 0)['wait_hint_s'], fl.DEFAULT_WAIT_HINT_S)


class PlanOrderTests(unittest.TestCase):
    def test_three_tasks_on_one_file_run_one_after_another(self):
        tasks = [dict(id='T1', files=['notiontool/a.py']), dict(id='T2', files=['NotionTool\\A.py']),
                 dict(id='T3', files=['./notiontool/a.py'], depends_on=[])]
        before = copy.deepcopy(tasks)
        r = fl.plan_order(tasks)
        self.assertEqual([(e['task'], e['depends_on']) for e in r['edges']], [('T2', 'T1'), ('T3', 'T2')])
        self.assertEqual(r['depends_on'], {'T1': [], 'T2': ['T1'], 'T3': ['T2']})
        self.assertEqual(tasks, before)
        graph = [dict(id=k, depends_on=v) for k, v in r['depends_on'].items()]
        self.assertIsNone(dag.find_cycle(graph))

    def test_existing_edge_is_skipped(self):
        tasks = [dict(id='T1', files=['a.py']), dict(id='T2', files=['a.py'], deps=['T1'])]
        r = fl.plan_order(tasks)
        self.assertEqual(r['edges'], [])
        self.assertEqual(r['skipped'][0]['reason'], 'exists')
        self.assertEqual(r['depends_on']['T2'], ['T1'])

    def test_edge_that_would_create_a_cycle_is_skipped(self):
        tasks = [dict(id='A', files=['x.py'], depends_on=['C']), dict(id='B', files=['y.py']),
                 dict(id='C', files=['x.py', 'y.py'], depends_on=['B'])]
        r = fl.plan_order(tasks)
        self.assertIn(dict(task='C', depends_on='A', file='x.py', reason='cycle'), r['skipped'])
        self.assertEqual([(e['task'], e['depends_on']) for e in r['edges']], [])
        graph = [dict(id=k, depends_on=v) for k, v in r['depends_on'].items()]
        self.assertIsNone(dag.find_cycle(graph))

    def test_no_cycles_on_many_overlapping_tasks(self):
        files = ['a.py', 'b.py', 'c.py']
        tasks = [dict(id='T%d' % i, files=[files[i % 3], files[(i + 1) % 3]],
                      depends_on=['T%d' % (i + 2)] if i % 4 == 0 and i + 2 < 9 else []) for i in range(9)]
        r = fl.plan_order(tasks)
        graph = [dict(id=k, depends_on=v) for k, v in r['depends_on'].items()]
        self.assertIsNone(dag.find_cycle(graph))
        self.assertTrue(r['edges'])

    def test_tasks_without_shared_files_get_no_edges(self):
        r = fl.plan_order([dict(id='T1', files=['a.py']), dict(id='T2', files=['b.py']), dict(id='T3')])
        self.assertEqual(r['edges'], [])
        self.assertEqual(r['depends_on'], {'T1': [], 'T2': [], 'T3': []})


if __name__ == '__main__':
    unittest.main()
