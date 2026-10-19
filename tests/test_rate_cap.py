"""I11: hard cap of 180 Notion request starts per sliding minute."""
import collections
import threading
import unittest

from notiontool.network_budget import HARD_LIMIT_PER_MINUTE, WINDOW_SECONDS, NetworkBudget, clamp_limit


class FakeStore:
    def __init__(self, saved=None):
        self.saved = dict(saved or {})
        self.recorded = []
        self.puts = []

    def get(self, kind, key, default=None):
        if kind == 'settings' and key == 'network_budget' and self.saved:
            return dict(self.saved)
        return default

    def put(self, kind, doc):
        self.puts.append((kind, dict(doc)))

    def event(self, kind, run_id='', *, commit=True, **fields):
        self.recorded.append(dict(fields, kind=kind))


class Clock:
    def __init__(self, start=1000.0):
        self.now = start
        self.lock = threading.Lock()

    def time(self):
        with self.lock:
            return self.now

    def sleep(self, seconds):
        with self.lock:
            self.now += max(seconds, 0)


class Recorder(collections.deque):
    def __init__(self):
        super().__init__()
        self.all = []

    def append(self, value):
        self.all.append(value)
        super().append(value)


def max_in_window(starts):
    starts = sorted(starts)
    best, left = 0, 0
    for right, value in enumerate(starts):
        while value - starts[left] >= WINDOW_SECONDS:
            left += 1
        best = max(best, right - left + 1)
    return best


class RateCapTests(unittest.TestCase):
    def budget(self, store=None):
        self.clock = Clock()
        budget = NetworkBudget(store, clock=self.clock.time, sleep=self.clock.sleep)
        budget.starts = Recorder()
        return budget

    def test_hard_limit_is_180_per_minute(self):
        self.assertEqual((HARD_LIMIT_PER_MINUTE, WINDOW_SECONDS), (180, 60.0))

    def test_clamp_never_raises_above_the_cap(self):
        for value, expected in ((500, 180), (181, 180), (180, 180), (50, 50), (0, 1), (-3, 1), ('bad', 180), (None, 180)):
            self.assertEqual(clamp_limit(value), expected, value)

    def test_saved_setting_can_lower_but_not_raise_the_cap(self):
        self.assertEqual(NetworkBudget(FakeStore({'limit_per_minute': 999})).limit_per_minute, 180)
        self.assertEqual(NetworkBudget(FakeStore({'limit_per_minute': 30})).limit_per_minute, 30)
        self.assertEqual(NetworkBudget().limit_per_minute, 180)

    def test_181st_start_waits_for_the_window_and_emits_one_event(self):
        store = FakeStore()
        budget = self.budget(store)
        for _ in range(181):
            budget.acquire()
        starts = budget.starts.all
        self.assertEqual(starts[179] - starts[0], 44.75)  # pacing alone would allow the 181st at 45 s
        self.assertGreaterEqual(starts[180] - starts[0], WINDOW_SECONDS)
        waits = [e for e in store.recorded if e['kind'] == 'request_window_wait']
        self.assertEqual(len(waits), 1)
        self.assertEqual((waits[0]['limit'], waits[0]['seconds']), (180, 15.25))
        self.assertEqual(budget.window_waits, 1)

    def test_sustained_load_never_exceeds_the_cap_in_any_window(self):
        budget = self.budget()
        for _ in range(600):
            budget.acquire()
        self.assertEqual(len(budget.starts.all), 600)
        self.assertEqual(max_in_window(budget.starts.all), 180)
        self.assertLessEqual(budget.window_count(), 180)

    def test_concurrent_callers_share_one_window(self):
        budget = self.budget()
        errors = []

        def worker():
            try:
                for _ in range(50):
                    budget.acquire()
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(budget.starts.all), 400)
        self.assertLessEqual(max_in_window(budget.starts.all), 180)

    def test_lowered_cap_is_enforced_and_kept_after_a_429(self):
        store = FakeStore({'limit_per_minute': 40})
        budget = self.budget(store)
        for _ in range(100):
            budget.acquire()
        self.assertLessEqual(max_in_window(budget.starts.all), 40)
        budget.limited(5)
        self.assertEqual(store.puts[-1][1]['limit_per_minute'], 40)

    def test_cancel_while_held_by_the_window_does_not_submit(self):
        budget = self.budget()
        for _ in range(180):
            budget.acquire()
        cancel = threading.Event()
        calls = []
        original = self.clock.sleep

        def sleep(seconds):
            calls.append(seconds)
            cancel.set()
            original(seconds)

        budget.sleep = sleep
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            budget.acquire(cancel)
        self.assertEqual(len(budget.starts.all), 180)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
