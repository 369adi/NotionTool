"""Desktop state push: cached state, capped lists, non-blocking pusher, heartbeat (desktop_state_cache.py)."""
import json
import shutil
import statistics
import tempfile
import time
import types
import unittest
from pathlib import Path

from notiontool import desktop_shell
from notiontool import desktop_state_cache as cache_mod
from notiontool.store import Store

ROOT = Path(__file__).resolve().parents[1]
HEAVY = 'x' * 30000   # ~30 KB of prompt/response per task, like the real store (16 MB / ~520 tasks)


def fake_store(directory, tasks=500, events=2000, workers=45, runs=94):
    store = Store(directory)
    now = time.time()
    records = []
    for i in range(workers):
        records.append(('workers', dict(id='w%d' % i, label='Cowork %d' % i, purpose='main' if i == 0 else 'worker',
                                        state='busy' if i % 3 else 'idle', thread_id='t%d' % i, created=now - 600)))
    for i in range(runs):
        records.append(('runs', dict(id='r%d' % i, state='running' if i >= runs - 10 else 'done',
                                     goal='Goal %d' % i, created=now - (runs - i) * 60, main_worker='w0')))
    for i in range(tasks):
        state = 'running' if i >= tasks - 30 else ('queued' if i >= tasks - 50 else 'done')
        records.append(('tasks', dict(id='k%d' % i, run_id='r%d' % (i * runs // tasks), title='Task %d' % i,
                                      state=state, worker_id='w%d' % (1 + i % (workers - 1)), created=now - 900,
                                      started=now - 300, updated=now - 30, finished=now - 20 if state == 'done' else None,
                                      model='gpt-5', effort='medium', submitted_prompt=HEAVY, raw_response=HEAVY[:8000],
                                      result=HEAVY[:4000], attempt_log=[{'n': 1, 'text': 'try'}])))
    store.batch(records)
    for i in range(events):
        store.event('task_done', message='Event %d' % i, commit=(i % 200 == 199))
    store.db.commit()
    return store


def server_for(store):
    return types.SimpleNamespace(store=store, engine=types.SimpleNamespace(coordination=lambda: {}),
                                 stalled_tasks={})


APP = dict(desktop_shell.app_defaults(), app_autostart=False)


def no_age(value):
    """Drop wall-clock ages (age_s etc.) so two builds a second apart compare equal."""
    if isinstance(value, dict):
        return {k: no_age(v) for k, v in value.items() if not k.startswith('age')}
    if isinstance(value, list):
        return [no_age(v) for v in value]
    return value


def timed(fn, repeat=3):
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(samples), result


class FakeStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix='nt-push-')
        cls.store = fake_store(cls.dir)
        cls.server = server_for(cls.store)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def new_cache(self, **kw):
        kw.setdefault('settings_loader', lambda data_dir: dict(APP))
        kw.setdefault('settings_path', Path(self.dir) / 'missing.json')
        return cache_mod.DesktopStateCache(self.server, self.dir, **kw)

    def test_before_after_numbers(self):
        before_ms, old = timed(lambda: desktop_shell.desktop_state(self.server, dict(APP)))
        after_ms, (new, _) = timed(lambda: self.new_cache().get())
        clock = [0.0]
        cache = self.new_cache(clock=lambda: clock[0])
        cache.get()
        hit_ms, (_, rebuilt) = timed(cache.get)
        samples = []
        for i in range(5):              # steady state: the engine wrote something, so every second rebuilds
            task = self.store.get('tasks', 'k499')
            task['updated'] = time.time() + i
            self.store.put('tasks', task)
            clock[0] += cache_mod.MIN_BUILD_S
            start = time.perf_counter()
            self.assertTrue(cache.get()[1])
            samples.append((time.perf_counter() - start) * 1000.0)
        rebuild_ms = statistics.median(samples)
        old_kb = len(cache_mod._state_key(old)) / 1024.0
        new_kb = len(cache_mod._state_key(new)) / 1024.0
        print('\n[desktop push] fake store 500 tasks/2000 events: desktop_state before %.0f ms; after: first build '
              '%.0f ms, rebuild on change %.1f ms, unchanged %.2f ms; payload before %.1f KB, after %.1f KB'
              % (before_ms, after_ms, rebuild_ms, hit_ms, old_kb, new_kb))
        self.assertLess(rebuild_ms, 50.0)
        self.assertLess(hit_ms, 5.0)
        self.assertFalse(rebuilt)
        self.assertLess(new_kb, 150.0)
        self.assertLess(after_ms, before_ms)

    def test_same_content_as_full_build(self):
        old = desktop_shell.desktop_state(self.server, dict(APP))
        new, _ = self.new_cache().get()
        for task in old['tasks']:
            task['result'] = task['result'][:cache_mod.RESULT_CHARS]    # only difference: shorter result preview
        for key in ('workers', 'runs', 'tasks', 'chats', 'mains', 'attention', 'rate', 'switches'):
            self.assertEqual(no_age(old.get(key)), no_age(new.get(key)), key)
        # links only come from capped runs (active + last 20 finished); never invented
        self.assertTrue(new['links'])
        for link in new['links']:
            self.assertIn(link, old['links'])
        self.assertEqual(old['events'][-30:], new['events'])

    def test_lists_capped(self):
        state, _ = self.new_cache().get()
        self.assertLessEqual(len(state['events']), cache_mod.EVENT_LIMIT)
        self.assertEqual(cache_mod.EVENT_LIMIT, 30)
        runs = cache_mod.cap_runs(self.store.all('runs'))
        active = [r for r in runs if r['state'] == 'running']
        self.assertEqual(len(active), 10)
        self.assertEqual(len(runs) - len(active), cache_mod.HISTORY_RUNS)
        light = cache_mod.LightStore(self.store).all('tasks')
        self.assertTrue(light)
        self.assertEqual(len(light), 500)
        for key in ('submitted_prompt', 'raw_response', 'attempt_log'):
            self.assertNotIn(key, light[0])
        self.assertEqual(len(light[0]['result']), cache_mod.RESULT_CHARS)

    def test_rebuild_only_on_revision_change(self):
        calls = []
        clock = [100.0]

        def builder(server, app):
            calls.append(1)
            return dict(ts=1, events=[], n=len(server.store.all('runs')))
        cache = self.new_cache(builder=builder, clock=lambda: clock[0], wall=lambda: 1000.0)
        cache.get()
        cache.get()
        self.assertEqual(len(calls), 1)
        self.store.put('settings', dict(id='push-test', touched=time.time()))
        clock[0] += 0.2
        self.assertFalse(cache.get()[1])        # throttled: at most one build per MIN_BUILD_S
        clock[0] += cache_mod.MIN_BUILD_S
        self.assertTrue(cache.get()[1])
        self.assertEqual(len(calls), 2)
        snap = cache.stats.snapshot()
        self.assertIn('desktop_state_ms', snap['timings'])
        self.assertIn('load_app_settings_ms', snap['timings'])


class FakeShell:
    def __init__(self):
        self.pushed = []
        self.evaluated = []

    def push(self, state):
        self.pushed.append(state)
        return True

    def _evaluate(self, expression):
        self.evaluated.append(expression)
        return True


class FakeCache:
    def __init__(self):
        self.state = dict(ts=1, n=1, events=[])
        self.stats = cache_mod.PushStats()

    def get(self):
        return self.state, True


class PusherTests(unittest.TestCase):
    def setUp(self):
        self.clock = [0.0]
        self.jobs = []
        self.events = []
        store = types.SimpleNamespace(event=lambda kind, **f: self.events.append((kind, f)))
        self.shell, self.cache = FakeShell(), FakeCache()
        self.pusher = cache_mod.StatePusher(self.shell, self.cache, store=store, clock=lambda: self.clock[0],
                                            wall=lambda: 1234.5, spawn=self.jobs.append)

    def run_jobs(self):
        while self.jobs:
            self.jobs.pop(0)()

    def test_send_only_on_change_and_heartbeat(self):
        self.assertTrue(self.pusher.tick())
        self.run_jobs()
        self.assertEqual(len(self.shell.pushed), 1)
        self.clock[0] = 1.0
        self.assertIsNone(self.pusher.tick())          # unchanged, heartbeat not due
        self.cache.state = dict(ts=2, n=1, events=[])   # only ts changed: still unchanged
        self.assertIsNone(self.pusher.tick())
        self.clock[0] = 3.5
        self.assertTrue(self.pusher.tick())
        self.run_jobs()
        self.assertEqual(len(self.shell.pushed), 1)
        beat = self.shell.evaluated[-1]
        self.assertIn('"type": "heartbeat"', beat)
        self.assertIn('"ts": 1234.5', beat)
        self.assertIn('__ntDeskLastMessage', beat)
        counters = self.pusher.stats.snapshot()['counters']
        self.assertEqual((counters['pushes'], counters['heartbeats']), (1, 1))

    def test_slow_page_never_blocks_and_stale_pushes_drop(self):
        self.assertTrue(self.pusher.tick())          # push in flight (job not run yet)
        self.cache.state = dict(ts=1, n=2, events=[])
        start = time.perf_counter()
        self.assertFalse(self.pusher.tick())         # dropped, not queued, returns at once
        self.assertLess(time.perf_counter() - start, 0.5)
        self.clock[0] = cache_mod.PUSH_TIMEOUT_S + 0.5
        self.pusher.tick()
        snap = self.pusher.stats.snapshot()['counters']
        self.assertEqual(snap['timeouts'], 1)
        self.assertGreaterEqual(snap['dropped'], 2)
        self.assertEqual(self.events[0][0], 'push_slow')
        self.assertEqual(len(self.jobs), 2)          # one push + one background state build, never a push queue
        self.run_jobs()                               # the slow push finally ends: newest state goes next
        self.assertTrue(self.pusher.tick())
        self.run_jobs()
        self.assertEqual(self.shell.pushed[-1]['n'], 2)

    def test_failed_push_is_counted_and_retried(self):
        def broken(state):
            raise RuntimeError('page busy')
        self.shell.push = broken
        self.pusher.tick()
        self.run_jobs()
        snap = self.pusher.stats.snapshot()
        self.assertEqual(snap['counters']['failures'], 1)
        self.assertIn('page busy', snap['last_error'])
        self.shell.push = FakeShell.push.__get__(self.shell)
        self.assertTrue(self.pusher.tick())
        self.run_jobs()
        self.assertEqual(len(self.shell.pushed), 1)


class OverlayTests(unittest.TestCase):
    def test_extras_heartbeat_and_banner(self):
        js = (ROOT / 'notiontool' / 'web' / 'desktop' / 'extras.js').read_text(encoding='utf-8')
        self.assertIn('NotionTool lost connection to the harness - retrying', js)
        self.assertIn('__ntDeskLastMessage', js)
        self.assertIn("next.type === 'heartbeat'", js)
        self.assertIn('DISCONNECTED_MS = 20000', js)


if __name__ == '__main__':
    unittest.main()
