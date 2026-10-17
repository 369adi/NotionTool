"""Phase 4 Fix L: a busy Main hands the review to the coworker with the smallest own share."""
import time
import unittest

from tests import test_engine as fixtures


class ReviewerBusyMainTests(unittest.TestCase):
    worker = fixtures.EngineTests.worker
    pump = fixtures.EngineTests.pump
    tearDown = fixtures.EngineTests.tearDown

    def setUp(self):
        fixtures.EngineTests.setUp(self)
        self.main = self.store.get('chats', 'c')['main_worker']
        self.run = dict(id='r-review', chat_id='c', main_worker=self.main, capacity=5, state='reviewing')
        self.coworkers = sorted(w['id'] for w in self.store.all('workers')
                                if w.get('purpose') == 'worker' and w.get('enabled')
                                and w['account_id'] == self.store.get('workers', self.main)['account_id'])
        self.assertGreaterEqual(len(self.coworkers), 2)
        # Every coworker executed once; the first one executed twice.
        for n, ident in enumerate(self.coworkers + self.coworkers[:1]):
            self.store.put('tasks', dict(id='e%d' % n, run_id=self.run['id'], role='executor',
                                         state='done', worker_id=ident, title='part'))
        self.events = []
        original = self.store.event

        def record(kind, *args, **fields):
            self.events.append(kind)
            return original(kind, *args, **fields)
        self.store.event = record

    def reviewer(self, **fields):
        return dict(id='rev', run_id=self.run['id'], role='reviewer', title='Reviewer', **fields)

    def choose(self, task):
        worker = self.engine._choose_worker(task, self.run, self.store.settings())
        return worker and worker['id']

    def test_free_main_still_reviews(self):
        self.assertEqual(self.choose(self.reviewer()), self.main)
        self.assertNotIn('reviewer_fallback', self.events)

    def test_busy_main_hands_review_to_smallest_share(self):
        task = self.reviewer(main_busy_at=time.time())
        chosen = self.choose(task)
        self.assertIn(chosen, self.coworkers[1:])
        self.assertNotEqual(chosen, self.main)
        self.assertTrue(task['review_fallback'])
        self.choose(task)
        self.assertEqual(self.events.count('reviewer_fallback'), 1)

    def test_old_busy_mark_returns_review_to_main(self):
        self.assertEqual(self.choose(self.reviewer(main_busy_at=time.time() - 3600)), self.main)

    def test_busy_main_respects_account_cooldown(self):
        account = self.store.get('accounts', self.store.get('workers', self.main)['account_id'])
        account['cooldown_until'] = time.time() + 60
        self.store.put('accounts', account)
        self.assertIsNone(self.choose(self.reviewer(main_busy_at=time.time())))

    def mark_compress(self):
        main = self.store.get('workers', self.main)
        main['compress_requested'] = True
        self.store.put('workers', main)

    def test_compressing_main_hands_review_to_coworker(self):
        self.mark_compress()
        chosen = self.choose(self.reviewer())
        self.assertIn(chosen, self.coworkers[1:])
        self.assertIn('reviewer_fallback', self.events)

    def test_compressing_main_hands_planner_to_coworker(self):
        self.mark_compress()
        chosen = self.choose(dict(id='pl', run_id=self.run['id'], role='planner', title='Planner'))
        self.assertIn(chosen, self.coworkers)

    def test_free_main_plans_again_after_compress(self):
        chosen = self.choose(dict(id='pl', run_id=self.run['id'], role='planner', title='Planner'))
        self.assertEqual(chosen, self.main)


if __name__ == '__main__':
    unittest.main()
