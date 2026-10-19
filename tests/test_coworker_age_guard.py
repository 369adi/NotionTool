"""I32: a Coworker chat 23 h or older never gets a new task."""
import time
import unittest

from notiontool import coworker_age_guard as guard
from tests import test_engine as fixtures

HOUR = 3600


class AgeGuardUnitTests(unittest.TestCase):
    def test_limit_is_23_hours(self):
        now = 1_760_000_000.0
        self.assertEqual(guard.HARD_MAX_AGE_HOURS, 23)
        self.assertFalse(guard.too_old(dict(created_at=now - 22.9 * HOUR), now))
        self.assertTrue(guard.too_old(dict(created_at=now - 23 * HOUR), now))
        self.assertTrue(guard.too_old(dict(created_at=(now - 30 * HOUR) * 1000), now))  # ms stamps

    def test_unknown_created_at_is_not_too_old(self):
        self.assertFalse(guard.too_old(dict(id='x'), time.time()))
        self.assertFalse(guard.too_old(dict(id='x', created_at=''), time.time()))
        self.assertFalse(guard.too_old(None, time.time()))

    def test_mains_are_never_filtered(self):
        class Store:
            def event(self, *a, **k):
                pass
        now = time.time()
        main = dict(id='m', purpose='operator', created_at=now - 50 * HOUR)
        old = dict(id='o', purpose='worker', created_at=now - 50 * HOUR)
        self.assertEqual(guard.young(Store(), [main, old], now), [main])


class AgeGuardEngineTests(unittest.TestCase):
    pump = fixtures.EngineTests.pump
    tearDown = fixtures.EngineTests.tearDown

    def worker(self, ident, purpose='worker'):
        fixtures.EngineTests.worker(self, ident, purpose)

    def setUp(self):
        fixtures.EngineTests.setUp(self)
        # Keep two coworkers (w0, w1) so every branch has a clear choice.
        for n in range(2, 40):
            w = self.store.get('workers', 'w%d' % n)
            w['enabled'] = False
            self.store.put('workers', w)
        self.main = 'main'
        self.run = dict(id='r-age', chat_id='c', main_worker=self.main, capacity=5, state='executing')
        self.store.put('runs', self.run)
        self.events = []
        original = self.store.event

        def record(kind, *args, **fields):
            self.events.append((kind, fields))
            return original(kind, *args, **fields)
        self.store.event = record
        self.age('w0', 1)
        self.age('w1', 1)

    def age(self, ident, hours):
        w = self.store.get('workers', ident)
        w['created_at'] = time.time() - hours * HOUR
        self.store.put('workers', w)

    def choose(self, task):
        worker = self.engine._choose_worker(task, self.run, self.store.settings())
        return worker and worker['id']

    def executor(self, **fields):
        return dict(id='ex', run_id=self.run['id'], role='executor', title='Part', **fields)

    def too_old_events(self):
        return [f for k, f in self.events if k == 'coworker_too_old']

    def test_young_chat_is_used(self):
        self.assertIn(self.choose(self.executor()), ('w0', 'w1'))
        self.assertEqual(self.too_old_events(), [])

    def test_executor_skips_old_chat(self):
        self.age('w0', 23)
        for _ in range(3):
            self.assertEqual(self.choose(self.executor()), 'w1')
        self.assertEqual(self.choose(self.executor(prefer_worker='w0')), 'w1')
        events = self.too_old_events()
        self.assertEqual(len(events), 1)  # throttled: once per worker per hour
        self.assertEqual(events[0]['worker_id'], 'w0')
        self.assertGreaterEqual(events[0]['age_hours'], 23)

    def test_hard_limit_ignores_setting(self):
        settings = self.store.settings()
        settings['coworker_max_age_hours'] = 72
        self.store.put('settings', settings)
        self.age('w0', 30)
        self.assertEqual(self.choose(self.executor()), 'w1')

    def test_fixer_skips_old_chat(self):
        self.age('w1', 24)
        self.assertEqual(self.choose(dict(id='fx', run_id=self.run['id'], role='executor', title='Fix', fix=True)), 'w0')

    def test_reviewer_skips_old_chat(self):
        # w0 executed; the busy Main hands the review to a coworker, but w1 is too old.
        self.store.put('tasks', dict(id='e0', run_id=self.run['id'], role='executor', state='done', worker_id='w0', title='p'))
        task = dict(id='rev', run_id=self.run['id'], role='reviewer', title='Reviewer', main_busy_at=time.time())
        self.assertEqual(self.choose(task), 'w1')
        self.age('w1', 23.5)
        self.assertNotEqual(self.choose(dict(task)), 'w1')
        self.assertIn(self.choose(dict(task)), ('w0', None))

    def test_reviewer_fallback_skips_old_chat(self):
        for n, ident in enumerate(['w0', 'w1', 'w0']):
            self.store.put('tasks', dict(id='e%d' % n, run_id=self.run['id'], role='executor',
                                         state='done', worker_id=ident, title='p'))
        self.age('w1', 25)  # w1 has the smallest share but is too old
        task = dict(id='rev', run_id=self.run['id'], role='reviewer', title='Reviewer', main_busy_at=time.time())
        self.assertEqual(self.choose(task), 'w0')
        self.age('w0', 25)
        self.assertIsNone(self.choose(dict(task)))

    def test_operator_review_skips_old_chat(self):
        task = dict(id='orv', run_id=self.run['id'], role='reviewer', title='Review', review_of='x', exclude_workers=['w1'])
        self.assertEqual(self.choose(task), 'w0')
        self.age('w0', 23)
        self.assertIsNone(self.choose(dict(task)))

    def test_main_busy_fallback_skips_old_chat(self):
        task = dict(id='pl', run_id=self.run['id'], role='planner', title='Planner', main_busy_at=time.time())
        self.age('w0', 23)
        self.assertEqual(self.choose(task), 'w1')
        self.age('w1', 23)
        self.assertIsNone(self.choose(dict(task)))

    def test_all_old_task_waits_not_failed(self):
        self.age('w0', 23)
        self.age('w1', 40)
        task = self.executor(state='queued', attempts=0)
        self.store.put('tasks', task)
        self.assertIsNone(self.choose(task))
        stored = self.store.get('tasks', 'ex')
        self.assertEqual(stored['state'], 'queued')
        self.assertEqual(stored['attempts'], 0)
        self.assertNotIn('worker_id', stored)

    def test_running_task_on_old_chat_is_not_aborted(self):
        w = self.store.get('workers', 'w0')
        w.update(state='busy', lease='L1')
        self.store.put('workers', w)
        running = self.executor(state='running', worker_id='w0', lease='L1')
        running['id'] = 'run1'
        self.store.put('tasks', running)
        self.age('w0', 30)
        self.assertEqual(self.choose(self.executor()), 'w1')
        self.assertEqual(self.store.get('tasks', 'run1')['state'], 'running')
        self.assertEqual(self.store.get('workers', 'w0')['lease'], 'L1')

    def test_event_throttle_per_hour(self):
        now = time.time()
        w = dict(id='wx', purpose='worker', created_at=now - 24 * HOUR)
        self.assertTrue(guard.note(self.store, w, now))
        self.assertFalse(guard.note(self.store, w, now + 10))
        self.assertFalse(guard.note(self.store, w, now + HOUR - 1))
        self.assertTrue(guard.note(self.store, w, now + HOUR))
        self.assertTrue(guard.note(self.store, dict(w, id='wy'), now + 10))


if __name__ == '__main__':
    unittest.main()
