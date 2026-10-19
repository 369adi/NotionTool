import threading
import time
import unittest

from notiontool import progress_feed as pf
from notiontool.progress_feed import ProgressFeed, summarize


def put(seq, kind, **fields):
    entity = dict(id='e%d' % seq, sequence=seq, kind=kind, created_at=1000 + seq)
    entity.update(fields)
    return {'op': 'put', 'entity': entity}


# Recorded shape of getThreadTranscript (direction backward): session + put patches of entities.
OLD_TURN = [put(1, 'user_message', content='older task'),
            put(2, 'tool_call', tool_name='write_file', input={'path': 'C:/x/old.py'}),
            put(3, 'assistant_message', content='Step 9 of 9 old answer'),
            put(4, 'turn_completed')]
RUNNING = {'session': {'status': 'running', 'sequence': 12}, 'patches': OLD_TURN + [
    put(5, 'user_message', id='s1', content='do the task'),
    put(6, 'thinking', content='plan'),
    put(7, 'tool_call', tool_use_id='t1', tool_name='mcp_call',
        input={'tool_name': 'read_file', 'arguments': {'path': 'C:\\repo\\notiontool\\engine.py'}}),
    put(8, 'tool_result', tool_use_id='t1', content='...'),
    put(9, 'tool_call', tool_use_id='t2', tool_name='search_code', input='{"query": "remote_done_at"}'),
    put(10, 'tool_result', tool_use_id='t2', content='...'),
    put(11, 'tool_call', tool_use_id='t3', tool_name='mcp_call',
        input={'tool_name': 'run_shell', 'arguments': {'command': 'python -m unittest tests.test_progress_feed ' + 'x' * 80}}),
]}
TASK = dict(id='k1', state='running', worker_id='w1', sent_at=1000.0, step_id='s1', before_seq=4, thread_id='th1')


class Store:
    def __init__(self, tasks, workers):
        self.tasks, self.workers = tasks, workers

    def all(self, table):
        return [dict(t) for t in self.tasks] if table == 'tasks' else list(self.workers.values())

    def get(self, table, ident, default=None):
        return dict(self.workers.get(ident) or {}) or default


class Client:
    """Read-only fake: only transcript() exists, so any send or stream would fail the test."""
    def __init__(self, payloads):
        self.payloads, self.calls = payloads, []

    def transcript(self, thread, limit=100):
        self.calls.append((thread, limit))
        value = self.payloads[thread]
        if isinstance(value, Exception):
            raise value
        return value() if callable(value) else value


class Budget:
    def __init__(self):
        self.info = dict(paused=False, cooldown_seconds=0)

    def status(self):
        return dict(self.info)


class Adapter:
    def __init__(self, payloads):
        self.c = Client(payloads)
        self.network_budget = Budget()

    def client(self, account_id):
        return self.c


class Clock:
    def __init__(self, now=2000.0):
        self.now = now

    def __call__(self):
        return self.now


def worker(i):
    return dict(id='w%d' % i, account_id='a1', purpose='worker', thread_id='th%d' % i)


class SummaryTest(unittest.TestCase):
    def test_running_turn_fields(self):
        info = summarize(RUNNING, TASK)
        self.assertEqual(info['steps'], 4)  # thinking + 3 tool calls; results and the older turn do not count
        self.assertEqual(info['step'], 'Running command')
        self.assertTrue(info['target'].startswith('python -m unittest tests.test_progress_feed'))
        self.assertLessEqual(len(info['target']), 60)
        self.assertEqual(info['progress']['source'], 'phase')
        self.assertEqual(info['progress']['phase'], 'testing')
        self.assertFalse(info['completed'])
        self.assertEqual(info['last_activity'], 1011)

    def test_kinds_and_targets(self):
        base = RUNNING['patches'][:8]  # up to the read_file result
        info = summarize({'session': {'status': 'running'}, 'patches': base}, TASK)
        self.assertEqual((info['step'], info['target']), ('Reading files', 'engine.py'))
        self.assertEqual(info['progress']['phase'], 'exploring')
        info = summarize({'session': {}, 'patches': base + [put(9, 'tool_call', tool_name='search_code', input='{"query": "remote_done_at"}')]}, TASK)
        self.assertEqual((info['step'], info['target']), ('Searching code', 'remote_done_at'))
        info = summarize({'session': {}, 'patches': base + [put(12, 'tool_call', tool_name='write_file', input={'path': 'a/b/progress_feed.py'})]}, TASK)
        self.assertEqual((info['step'], info['target'], info['progress']['phase']), ('Writing file', 'progress_feed.py', 'editing'))
        info = summarize({'session': {}, 'patches': base[:6]}, TASK)
        self.assertEqual(info['step'], 'Thinking')

    def test_answer_snippet_and_completion(self):
        text = 'Done so far. ' + 'word ' * 60 + 'final words'
        payload = {'session': {'status': 'idle'}, 'patches': RUNNING['patches'] + [put(12, 'assistant_message', content=[{'type': 'text', 'text': text}]), put(13, 'turn_completed')]}
        info = summarize(payload, TASK)
        self.assertEqual(info['step'], 'Writing answer')
        self.assertLessEqual(len(info['snippet']), 140)
        self.assertTrue(info['snippet'].endswith('final words'))
        self.assertTrue(info['completed'])
        self.assertEqual(info['progress']['percent'], 100)

    def test_step_x_of_y_and_checklist(self):
        step = {'session': {'status': 'running'}, 'patches': RUNNING['patches'][:9] + [put(12, 'assistant_message', content='Step 3 of 4: tests')]}
        p = summarize(step, TASK)['progress']
        self.assertEqual((p['source'], p['percent'], p['label']), ('steps', 75, 'Step 3 of 4'))
        check = {'session': {'status': 'running'}, 'patches': RUNNING['patches'][:9] + [put(12, 'assistant_message', content='- [x] read\n- [x] write\n- [ ] test\n- [ ] answer')]}
        p = summarize(check, TASK)['progress']
        self.assertEqual((p['source'], p['percent']), ('checklist', 50))

    def test_only_own_turn_without_marker_uses_newest_user(self):
        info = summarize(RUNNING, dict(TASK, step_id='', before_seq=0))
        self.assertEqual(info['steps'], 4)
        self.assertEqual(summarize({'patches': []}, TASK)['step'], 'Starting')


class FeedTest(unittest.TestCase):
    def make(self, n=1, payload=RUNNING):
        tasks = [dict(TASK, id='k%d' % i, worker_id='w%d' % i, thread_id='th%d' % i) for i in range(n)]
        store = Store(tasks, {'w%d' % i: worker(i) for i in range(n)})
        adapter = Adapter({'th%d' % i: payload for i in range(n)})
        clock = Clock()
        return ProgressFeed(store, adapter, clock=clock), store, adapter, clock

    def run_for(self, feed, clock, seconds):
        for _ in range(int(seconds)):
            feed.tick()
            clock.now += 1

    def test_polls_busy_task_every_15s_and_caches(self):
        feed, store, adapter, clock = self.make()
        feed.tick()
        self.assertEqual(adapter.c.calls, [('th0', pf.WINDOW)])
        payload = feed.payload('k0')
        self.assertEqual((payload['steps'], payload['step']), (4, 'Running command'))
        self.assertEqual(set(feed.public()), {'k0'})
        self.run_for(feed, clock, 14)
        self.assertEqual(len(adapter.c.calls), 1)  # cached between polls
        clock.now = 2015
        feed.tick()
        self.assertEqual(len(adapter.c.calls), 2)

    def test_idle_and_main_workers_are_not_read_and_ended_task_stops(self):
        feed, store, adapter, clock = self.make(3)
        store.tasks[1]['state'] = 'done'
        store.workers['w2']['purpose'] = 'main'
        self.run_for(feed, clock, 60)
        self.assertEqual({c[0] for c in adapter.c.calls}, {'th0'})
        store.tasks[0]['state'] = 'done'
        before = len(adapter.c.calls)
        self.run_for(feed, clock, 60)
        self.assertEqual(len(adapter.c.calls), before)
        self.assertIsNone(feed.payload('k0'))
        self.assertEqual(feed.public(), {})

    def test_ten_busy_coworkers_stay_under_40_per_minute_and_are_staggered(self):
        feed, store, adapter, clock = self.make(10)
        times = []
        orig = adapter.c.transcript
        adapter.c.transcript = lambda thread, limit=100: (times.append(clock.now), orig(thread, limit))[1]
        self.run_for(feed, clock, 600)
        self.assertEqual(len(set(times)), len(times))  # one start per tick: staggered
        worst = max(sum(1 for t in times if s <= t < s + 60) for s in times)
        self.assertLess(worst, 40)
        self.assertLessEqual(worst, pf.MAX_PER_MINUTE)
        for i in range(10):
            self.assertGreater(sum(1 for c in adapter.c.calls if c[0] == 'th%d' % i), 30)

    def test_stalled_after_five_minutes_without_new_step(self):
        state = {'patches': list(RUNNING['patches']), 'session': {'status': 'running'}}
        feed, store, adapter, clock = self.make(1, payload=lambda: dict(state, patches=list(state['patches'])))
        feed.tick()
        self.assertFalse(feed.payload('k0')['stalled'])
        self.run_for(feed, clock, 301)
        payload = feed.payload('k0')
        self.assertTrue(payload['stalled'])
        self.assertGreaterEqual(payload['stalled_s'], 300)
        state['patches'].append(put(12, 'tool_call', tool_name='write_file', input={'path': 'x.py'}))
        self.run_for(feed, clock, 16)
        self.assertFalse(feed.payload('k0')['stalled'])
        self.assertEqual(feed.payload('k0')['steps'], 5)

    def test_errors_back_off_and_paused_budget_sends_nothing(self):
        feed, store, adapter, clock = self.make()
        adapter.c.payloads['th0'] = ValueError('Notion getThreadTranscript: 500')
        feed.tick()
        self.assertEqual(feed.payload('k0')['error'], 'ValueError')
        self.run_for(feed, clock, 59)
        self.assertEqual(len(adapter.c.calls), 1)
        adapter.network_budget.info['paused'] = True
        self.run_for(feed, clock, 120)
        self.assertEqual(len(adapter.c.calls), 1)

    def test_reads_run_at_background_priority(self):
        from notiontool.network_budget import current_priority
        seen = []
        feed, store, adapter, clock = self.make(1, payload=lambda: (seen.append(current_priority()), RUNNING)[1])
        feed.tick()
        self.assertEqual(seen, ['background'])

    def test_start_stop(self):
        feed, store, adapter, clock = self.make()
        old = pf.TICK_S
        pf.TICK_S = 0.01
        try:
            feed.start()
            deadline = time.time() + 2
            while not adapter.c.calls and time.time() < deadline:
                time.sleep(0.01)
            feed.stop()
        finally:
            pf.TICK_S = old
        self.assertTrue(adapter.c.calls)
        self.assertIsNone(feed.thread)


if __name__ == '__main__':
    unittest.main()
