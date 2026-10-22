"""Phase 4: planner modes DIRECT/SPLIT/DEEP and background request priority (mocked; no live requests)."""
import threading
import unittest

from notiontool.allocation import allocation, planner_mode
from notiontool.network_budget import (BREAKER_FAILURES, CircuitOpen, NetworkBudget, current_priority,
                                       set_thread_priority)

try:
    from tests.test_coordination import Clock, MemoryStore
    import tests.test_harness_multi_main as multi
except ImportError:  # discovered with -s tests
    from test_coordination import Clock, MemoryStore
    import test_harness_multi_main as multi


class PlannerModeTests(unittest.TestCase):
    def test_bounded_requests_are_direct(self):
        for goal in ('Reply with exactly one line: DEDUPE-OK',
                     'Phase 2 telemetry check 1: reply with one short sentence.',
                     'Was ist 2+2?', 'Calculate 17 * 23', 'What is the capital of France?', 'Hallo!',
                     'Übersetze "Haus" ins Englische'):
            self.assertEqual(planner_mode(goal), 'direct', goal)
            policy = allocation(goal, 5)
            self.assertEqual((policy['mode'], policy['planner_mode'], policy['budget'], policy['advisers']),
                             ('direct', 'direct', 1, 0), goal)

    def test_delegation_tools_files_and_lists_need_a_plan(self):
        for goal in ('Split this into exactly three independent tasks and answer each',
                     'Use the screen_capture tool once and describe the screen',
                     'Implement a login page',
                     'Lies die Datei README.md und fasse sie zusammen',
                     'What is in notiontool/engine.py? Read the file.',
                     'Answer: 1. what is 2+2\n2. what is 3+3',
                     'What is A? What is B?',
                     'Plan the migration of the store'):
            self.assertEqual(planner_mode(goal), 'split', goal)
            policy = allocation(goal, 5)
            self.assertEqual((policy['mode'], policy['planner_mode']), ('planned', 'split'), goal)

    def test_analysis_review_and_long_goals_are_deep(self):
        for goal in ('Review the allocation code for bugs', 'Compare SQLite and Postgres for this app',
                     'Diagnose why workers are slow', 'Analysiere die Architektur', ' '.join(['word'] * 160)):
            self.assertEqual(planner_mode(goal), 'deep', goal[:40])
            policy = allocation(goal, 5)
            self.assertEqual((policy['mode'], policy['planner_mode'], policy['budget']), ('planned', 'deep', 5))

    def test_explicit_count_stays_split_with_requested_parts(self):
        policy = allocation('Use exactly 15 independent calculations', 15)
        self.assertEqual((policy['mode'], policy['planner_mode'], policy['requested_parts']), ('planned', 'split', 15))


class DirectRunTests(unittest.TestCase):
    def setUp(self):
        self.case = multi.MultiMainTests('test_simple_questions_get_no_coworker')
        self.case.setUp()

    def tearDown(self):
        self.case.tearDown()

    def test_direct_run_is_one_main_turn_even_with_model_routing(self):
        case = self.case
        case.adapter.task_model_routing = True
        run = case.engine.submit('c', 'Reply with exactly one line: DIRECT-OK')
        self.assertTrue(run['model_routing'])
        self.assertEqual(run['allocation']['planner_mode'], 'direct')
        case.pump(lambda: case.store.get('runs', run['id'])['state'] == 'done')
        self.assertEqual([r for _, r in case.adapter.roles], ['integrator'])
        self.assertEqual(len([t for t in case.store.all('tasks') if t.get('run_id') == run['id']]), 1)


def budget():
    store, clock = MemoryStore(), Clock()
    return NetworkBudget(store, clock=clock.time, sleep=clock.sleep, rng=lambda: .5), store, clock


class BackgroundPriorityTests(unittest.TestCase):
    def test_background_failures_keep_task_parallelism_but_open_their_breaker(self):
        nb, store, clock = budget()
        self.assertEqual(nb.parallel_limit(8), 8)
        for _ in range(BREAKER_FAILURES):
            nb.release(nb.acquire(priority='background', source='getThreadTranscript'), 'failed')
            clock.sleep(1)
        self.assertEqual(nb.parallel_limit(8), 8)
        self.assertNotIn('request_coordination_adjusted', store.kinds())
        self.assertIn('circuit_open', store.kinds())
        with self.assertRaises(CircuitOpen):
            nb.acquire(priority='background', source='getThreadTranscript')
        # Task traffic to other endpoints is unaffected by the open scan circuit.
        nb.release(nb.acquire(source='runInferenceTranscript'), 'ok')
        self.assertEqual(nb.parallel_limit(8), 8)

    def test_task_failures_still_lower_parallelism(self):
        nb, store, clock = budget()
        nb.parallel_limit(8)
        nb.release(nb.acquire(source='runInferenceTranscript'), 'failed')
        self.assertEqual(nb.parallel_limit(8), 4)

    def test_background_rate_limit_still_lowers_parallelism(self):
        nb, store, clock = budget()
        nb.parallel_limit(8)
        nb.release(nb.acquire(priority='background', source='getThreadTranscript'), 'rate_limited', 429)
        self.assertEqual(nb.parallel_limit(8), 4)

    def test_set_thread_priority_applies_only_to_its_thread(self):
        seen = []

        def scan():
            set_thread_priority('background')
            seen.append(current_priority())
            set_thread_priority('bogus')
            seen.append(current_priority())

        thread = threading.Thread(target=scan)
        thread.start()
        thread.join()
        self.assertEqual(seen, ['background', 'rest'])
        self.assertEqual(current_priority(), 'rest')


class BusyChatTests(unittest.TestCase):
    """Live backtest finding: a busy Main chat was reported as a Notion rate limit, used up all attempts
    and cooled the whole account down."""
    def setUp(self):
        self.case = multi.MultiMainTests('test_simple_questions_get_no_coworker')
        self.case.setUp()

    def tearDown(self):
        self.case.tearDown()

    def complete(self, exc, **fields):
        from concurrent.futures import Future
        case = self.case
        run = case.engine.submit('c', 'Reply with exactly one line: BUSY-OK')
        task = case.engine.tasks(run['id'])[0]
        task.update(state='running', lease='saved', worker_id='main', **fields)
        case.store.put('tasks', task)
        future = Future()
        future.set_exception(exc)
        case.engine._complete(future, dict(task_id=task['id'], lease='saved', worker_id='main'))
        return case.store.get('tasks', task['id'])

    def test_busy_chat_uses_no_attempt_and_no_account_cooldown(self):
        from notiontool.engine import RateLimited
        limit = self.case.store.settings()['max_attempts']
        task = self.complete(RateLimited(10, 'This chat is busy with another turn', free=True, scope='thread'),
                             attempts=limit)
        self.assertEqual((task['state'], task['attempts'], task['busy_waits']), ('retry', limit - 1, 1))
        self.assertIn('busy', task['error'])
        self.assertEqual(self.case.store.get('accounts', 'a')['cooldown_until'], 0)

    def test_real_rate_limit_still_cools_the_account_down(self):
        from notiontool.engine import RateLimited
        task = self.complete(RateLimited(10), attempts=1)
        self.assertEqual(task['state'], 'retry')
        self.assertGreater(self.case.store.get('accounts', 'a')['cooldown_until'], 0)

    def test_busy_waits_are_bounded(self):
        from notiontool.engine import BUSY_WAIT_LIMIT, RateLimited
        task = self.complete(RateLimited(10, 'busy', free=True, scope='thread'), attempts=1,
                             busy_waits=BUSY_WAIT_LIMIT)
        self.assertEqual(task['state'], 'blocked')
        self.assertIn('stayed busy', task['error'])

    def test_busy_main_hands_main_tasks_to_a_free_coworker(self):
        import time
        from notiontool.engine import MAIN_BUSY_FALLBACK_S, RateLimited
        case = self.case
        task = self.complete(RateLimited(10, 'busy', free=True, scope='thread'), attempts=1)
        self.assertTrue(task.get('main_busy_at'))
        run = case.store.get('runs', task['run_id'])
        worker = case.engine._choose_worker(task, run, case.store.settings())
        self.assertIsNotNone(worker)
        self.assertEqual((worker['purpose'], worker['account_id']), ('worker', 'a'))
        kinds = [e['kind'] for e in case.store.events(run['id'])] if hasattr(case.store, 'events') else []
        if kinds:
            self.assertIn('main_busy_fallback', kinds)
        task['main_busy_at'] = time.time() - MAIN_BUSY_FALLBACK_S - 1
        self.assertEqual(case.engine._choose_worker(task, run, case.store.settings())['id'], run['main_worker'])

    def test_adapter_reports_a_busy_session_as_thread_scoped(self):
        from notiontool.engine import RateLimited
        from notiontool.notion import NotionAdapter
        case = self.case
        account = case.store.get('accounts', 'a')
        account['model'] = 'model'
        case.store.put('accounts', account)
        for transcript in ({'session': {'status': 'running'}}, {'session': {}, 'session_pending': True}):
            adapter = object.__new__(NotionAdapter)
            adapter.store = case.store
            adapter.client = lambda account_id, t=transcript: type('Client', (), {'transcript': lambda self, tid, **kw: t})()
            with self.assertRaises(RateLimited) as caught:
                adapter.run(case.store.get('workers', 'main'), {'id': 't'}, 'prompt', None, None)
            self.assertEqual((caught.exception.scope, caught.exception.free), ('thread', True))
            self.assertIn('busy', caught.exception.message)


class CoworkerHandoffTests(unittest.TestCase):
    """Fix E: a coworker's context checkpoint carries only the current run, never the Main chat's other requests."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from notiontool.store import Store
        base = Path(__file__).parent / '.test-data'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.store = Store(self.temp.name)
        self.store.put('runs', dict(id='r', chat_id='c', goal='Answer three small questions in English'))
        self.store.put('runs', dict(id='old', chat_id='c', goal='Build the stone island'))
        self.store.message('c', 'user', 'Build the stone island with crystal spikes', run_id='old')
        self.store.message('c', 'user', 'Reply with exactly one line: DIRECT-OK')
        self.store.message('c', 'user', 'Keep the answers short', run_id='r')
        self.store.put('memory', dict(id='c', text='Chat memory about the stone island'))
        self.store.put('thread_context', dict(id='a:cw', entities={
            '1': dict(kind='assistant_message', sequence=1, text='Earlier answer for another run')}))
        self.task = dict(id='t', run_id='r', title='Combine the answers', state='running')
        self.store.put('tasks', self.task)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def handoff(self, purpose):
        from notiontool.chat_context import ChatContext
        worker = dict(id='a:cw', account_id='a', thread_id='cw', purpose=purpose,
                      handoff='Old checkpoint: DIRECT-OK still pending')
        return ChatContext(self.store).handoff(worker, self.task)

    def test_coworker_checkpoint_keeps_only_the_current_run(self):
        text = self.handoff('worker')
        self.assertIn('Answer three small questions in English', text)
        self.assertIn('Keep the answers short', text)
        self.assertIn('Combine the answers', text)
        for foreign in ('stone island', 'DIRECT-OK', 'Earlier answer for another run'):
            self.assertNotIn(foreign, text)

    def test_main_checkpoint_still_keeps_the_chat_history(self):
        text = self.handoff('main')
        self.assertIn('stone island', text)
        self.assertIn('DIRECT-OK', text)
        self.assertIn('Earlier answer for another run', text)


if __name__ == '__main__':
    unittest.main()
