"""Eight realistic slots; each chat stays on its own account (B45)."""
import unittest
import time
import test_engine as fixtures


class MeteredAdapter(fixtures.FakeAdapter):
    """FakeAdapter that counts concurrent executor invocations per account."""
    def __init__(self, count=5):
        super().__init__(count=count)
        self.by_account = {}; self.account_peaks = {}; self.execution = []
        self.executing = 0; self.execution_peak = 0; self.wait_execution = True

    def run(self, worker, task, prompt, cancel, update):
        execution = task['role'] == 'executor'
        if execution:
            with self.lock:
                account = worker['account_id']
                self.by_account[account] = self.by_account.get(account, 0) + 1
                self.account_peaks[account] = max(self.account_peaks.get(account, 0), self.by_account[account])
                self.executing += 1
                self.execution_peak = max(self.execution_peak, self.executing)
                self.execution.append((task['run_id'], worker['id'], account, prompt))
            if self.wait_execution:
                self.gate.wait(12)
        try:
            return super().run(worker, task, prompt, cancel, update)
        finally:
            if execution:
                with self.lock:
                    self.by_account[worker['account_id']] -= 1
                    self.executing -= 1


class EightSlotTests(unittest.TestCase):
    """B45: each chat dispatches only to coworkers on its own account.
    To exercise all 8 slots in parallel, one chat per slot is used.
    """
    worker = fixtures.EngineTests.worker
    pump = fixtures.EngineTests.pump
    tearDown = fixtures.EngineTests.tearDown

    def setUp(self):
        # Base setup: 1 account 'a', workers w0-w39, chat 'c', main worker.
        fixtures.EngineTests.setUp(self)
        # count=5: planner returns 5 executor tasks (one account's worker quota).
        self.adapter = MeteredAdapter(count=5)
        self.engine.adapter = self.adapter
        # Create 8 accounts, assign workers, create one main+chat per account.
        for slot in range(8):
            ident = 'a' if slot == 0 else 'a%d' % slot
            self.store.put('accounts', dict(id=ident, slot=slot + 1, state='ready', cooldown_until=0))
            for n in range(slot * 5, slot * 5 + 5):
                w = self.store.get('workers', 'w%d' % n)
                w['account_id'] = ident
                self.store.put('workers', w)
            if slot:
                self.worker('main%d' % slot, 'main')
                w = self.store.get('workers', 'main%d' % slot)
                w['account_id'] = ident
                self.store.put('workers', w)
                self.store.put('chats', dict(id='c%d' % slot, title='Slot%d' % slot,
                                             main_worker='main%d' % slot,
                                             folder_id='f', project_path=self.temp.name))
        settings = self.store.settings()
        settings['per_account'] = 5
        self.store.put('settings', settings)

    # -- helpers -------------------------------------------------------------

    def _chats(self, exclude=()):
        """Return all 8 chat IDs except those in exclude."""
        return [cid for cid in (['c'] + ['c%d' % n for n in range(1, 8)])
                if cid not in exclude]

    def _submit_all(self, msg, exclude=()):
        """Submit msg to every eligible chat and return all run objects."""
        return [self.engine.submit(cid, msg) for cid in self._chats(exclude=exclude)]

    # -- tests ---------------------------------------------------------------

    def test_eight_slots_five_workers_each_forty_inflight(self):
        """8 chats x 5 workers = 40 concurrent executors."""
        runs = self._submit_all('five independent contributions')
        self.pump(lambda: self.adapter.executing == 40, timeout=12)
        expected_accounts = {('a' if n == 0 else 'a%d' % n) for n in range(8)}
        self.assertEqual(set(self.adapter.by_account.keys()), expected_accounts)
        self.assertTrue(all(n == 5 for n in self.adapter.by_account.values()))
        self.assertEqual(len({w for _, w, _, _ in self.adapter.execution}), 40)
        self.assertFalse(
            any(self.store.get('workers', w)['purpose'] == 'main'
                for _, w, _, _ in self.adapter.execution))
        self.adapter.gate.set()
        self.pump(lambda: all(self.store.get('runs', r['id'])['state'] == 'done'
                              for r in runs), timeout=12)

    def test_per_slot_cap_three_queues_remaining_work(self):
        """per_account=3 across 8 chats: 8x3=24 inflight, 8x2=16 queued."""
        settings = self.store.settings()
        settings['per_account'] = 3
        self.store.put('settings', settings)
        runs = self._submit_all('capacity constrained')
        self.pump(lambda: self.adapter.executing == 24, timeout=12)
        self.assertTrue(all(n == 3 for n in self.adapter.by_account.values()))
        total_queued = sum(
            sum(1 for t in self.engine.tasks(r['id']) if t['state'] == 'queued')
            for r in runs)
        self.assertEqual(total_queued, 16)
        self.adapter.gate.set()
        self.pump(lambda: all(self.store.get('runs', r['id'])['state'] == 'done'
                              for r in runs), timeout=12)
        self.assertTrue(all(n <= 3 for n in self.adapter.account_peaks.values()))

    def test_two_main_chats_fairly_share_full_pool(self):
        """Two chats on different accounts: 5+5=10 concurrent executors."""
        first = self.engine.submit('c', 'ALPHA_ONLY')
        second = self.engine.submit('c1', 'BETA_ONLY')
        self.pump(lambda: all(self.store.get('runs', r['id'])['state'] == 'executing'
                              for r in (first, second)), timeout=9)
        self.pump(lambda: self.adapter.executing == 10, timeout=9)
        self.adapter.gate.set()
        self.pump(lambda: all(self.store.get('runs', r['id'])['state'] == 'done'
                              for r in (first, second)), timeout=9)
        for ident, _, _, prompt in self.adapter.execution:
            expected = 'ALPHA_ONLY' if ident == first['id'] else 'BETA_ONLY'
            unexpected = 'BETA_ONLY' if ident == first['id'] else 'ALPHA_ONLY'
            self.assertIn(expected, prompt)
            self.assertNotIn(unexpected, prompt)
        self.assertLessEqual(self.adapter.execution_peak, 10)
        self.assertEqual(
            {m['chat_id'] for m in self.store.all('messages') if m['role'] == 'assistant'},
            {'c', 'c1'})

    def test_one_disabled_slot_does_not_stall_other_seven(self):
        """Disable all workers on a7; the other 7 chats still run 35 executors."""
        for w in self.store.all('workers'):
            if w['account_id'] == 'a7':
                w['enabled'] = False
                self.store.put('workers', w)
        runs = self._submit_all('degraded pool', exclude=['c7'])
        self.pump(lambda: self.adapter.executing == 35, timeout=12)
        self.assertNotIn('a7', self.adapter.by_account)
        self.adapter.gate.set()
        self.pump(lambda: all(self.store.get('runs', r['id'])['state'] == 'done'
                              for r in runs), timeout=12)

    def test_reviewer_fallback_respects_account_cooldown(self):
        """When account 'a' is on cooldown, no reviewer can be found for chat 'c'."""
        run = self.engine.submit('c', 'review during cooldown')
        for w in self.store.all('workers'):
            if w['purpose'] == 'worker' and w['account_id'] == 'a':
                task = self.engine.add_task(run, 'executor', 'completed contribution')
                task.update(state='done', worker_id=w['id'])
                self.store.put('tasks', task)
        account = self.store.get('accounts', 'a')
        account['cooldown_until'] = time.time() + 60
        self.store.put('accounts', account)
        reviewer = self.engine.add_task(run, 'reviewer', 'check results')
        self.assertIsNone(self.engine._choose_worker(reviewer, run, self.store.settings()))

    def test_recovering_worker_is_not_reused_and_pool_continues(self):
        """w0 is recovering; account 'a' dispatches to its 4 remaining workers."""
        w = self.store.get('workers', 'w0')
        w['state'] = 'recovering'
        self.store.put('workers', w)
        run = self.engine.submit('c', 'one unavailable worker')
        self.pump(lambda: self.adapter.executing == 4, timeout=9)
        self.assertNotIn('w0', {w for _, w, _, _ in self.adapter.execution})
        self.adapter.gate.set()
        self.pump(lambda: self.store.get('runs', run['id'])['state'] == 'done', timeout=9)

    def test_global_limit_twelve_across_eight_slots(self):
        """max_workers=12 caps total inflight to 12 even with 8 chats in flight."""
        settings = self.store.settings()
        settings['max_workers'] = 12
        self.store.put('settings', settings)
        runs = self._submit_all('global backpressure')
        self.pump(lambda: self.adapter.executing == 12, timeout=12)
        total_queued = sum(
            sum(1 for t in self.engine.tasks(r['id']) if t['state'] == 'queued')
            for r in runs)
        self.assertGreaterEqual(total_queued, 28)  # 8*5 - 12 = 28 queued minimum
        self.adapter.gate.set()
        self.pump(lambda: all(self.store.get('runs', r['id'])['state'] == 'done'
                              for r in runs), timeout=12)
        self.assertLessEqual(self.adapter.execution_peak, 12)

    def test_signed_out_slot_is_excluded_even_with_cached_ready_workers(self):
        """Account a7 signed out: its chat is excluded; 7 others run 35 executors."""
        account = self.store.get('accounts', 'a7')
        account['state'] = 'login'
        self.store.put('accounts', account)
        runs = self._submit_all('one expired account', exclude=['c7'])
        self.pump(lambda: self.adapter.executing == 35, timeout=12)
        self.assertNotIn('a7', self.adapter.by_account)
        self.adapter.gate.set()
        self.pump(lambda: all(self.store.get('runs', r['id'])['state'] == 'done'
                              for r in runs), timeout=12)


if __name__ == '__main__':
    unittest.main()
