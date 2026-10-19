"""Phase 4 Fix K: a frozen Edge transport tab is woken instead of waiting for task_timeout."""
import inspect
import time
import unittest

from notiontool import browser as browser_module
from notiontool.edge_transport import EdgeTransport


class FakeStore:
    def __init__(self):
        self.events = []

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))

    def settings(self):
        return {'task_timeout': 900}


class FakeBudget:
    def __init__(self):
        self.released = []

    def acquire(self, cancel=None, source=''):
        return 'ticket'

    def release(self, ticket, outcome, **fields):
        self.released.append(outcome)


class FrozenBrowser:
    """Answers DevTools polls like a frozen tab: no page task runs until the tab is woken."""

    def __init__(self, frozen=True, fail_wake=False):
        self.frozen, self.fail_wake = frozen, fail_wake
        self.woken = 0
        self.delivered = False

    def wake(self, tab, timeout=5):
        if self.fail_wake:
            raise RuntimeError('DevTools call timed out')
        self.woken += 1
        self.frozen = False

    def evaluate(self, tab, expression, timeout=30):
        if 'setTimeout(() => r(1), 0)' in expression:
            if self.frozen:
                raise TimeoutError('no answer')
            return 1
        if 'splice' in expression:
            if self.frozen or self.delivered:
                return {'status': 200, 'done': self.delivered, 'error': False, 'text': '', 'remaining': 0}
            self.delivered = True
            return {'status': 200, 'done': True, 'error': False, 'text': '{"type":"done"}\n', 'remaining': 0}
        return None


def make(browser):
    store, budget = FakeStore(), FakeBudget()
    edge = EdgeTransport(store, None, 'acct', budget, browser=browser)
    edge.stall_wake_after = 0.2
    edge._start = lambda path, body, headers, timeout, cancel: ('tab', 'key', 'ticket')
    edge._save_cookies = lambda target: None
    return edge, store, budget


class EdgeWakeTest(unittest.TestCase):
    def test_stalled_stream_wakes_frozen_tab_and_finishes(self):
        browser = FrozenBrowser()
        edge, store, budget = make(browser)
        began = time.monotonic()
        text = ''.join(edge._chunks('runInferenceTranscript', {}, {}, 30))
        self.assertEqual(text, '{"type":"done"}\n')
        self.assertLess(time.monotonic() - began, 5)
        self.assertGreaterEqual(browser.woken, 1)
        self.assertEqual(budget.released, ['ok'])
        kinds = [(kind, fields.get('frozen'), fields.get('reason')) for kind, fields in store.events]
        self.assertIn(('edge_tab_woken', True, 'stalled runInferenceTranscript'), kinds)

    def test_stream_reads_the_end_after_wake(self):
        edge, _, _ = make(FrozenBrowser())
        self.assertEqual(list(edge.stream('runInferenceTranscript', {})), ['{"type":"done"}'])

    def test_periodic_wake_is_throttled_and_silent(self):
        browser = FrozenBrowser(frozen=False)
        edge, store, _ = make(browser)
        self.assertTrue(edge.wake('tab'))
        self.assertFalse(edge.wake('tab'))
        self.assertEqual(browser.woken, 1)
        self.assertEqual(store.events, [])

    def test_forced_wake_of_live_tab_logs_nothing(self):
        browser = FrozenBrowser(frozen=False)
        edge, store, _ = make(browser)
        self.assertTrue(edge.wake('tab', force=True, reason='stalled x'))
        self.assertEqual(browser.woken, 1)
        self.assertEqual(store.events, [])

    def test_frozen_event_is_rate_limited(self):
        browser = FrozenBrowser()
        edge, store, _ = make(browser)
        edge.stall_wake_after = 0
        self.assertTrue(edge.wake('tab', force=True, reason='stalled a'))
        browser.frozen = True
        self.assertTrue(edge.wake('tab', force=True, reason='stalled b'))
        self.assertEqual([kind for kind, _ in store.events], ['edge_tab_woken'])

    def test_wake_failure_is_swallowed(self):
        edge, store, _ = make(FrozenBrowser(fail_wake=True))
        self.assertFalse(edge.wake('tab', force=True))
        self.assertEqual(store.events, [])

    def test_owned_browser_starts_without_background_throttling(self):
        flags = browser_module.NO_THROTTLE_FLAGS
        for flag in ('--disable-background-timer-throttling', '--disable-renderer-backgrounding',
                     '--disable-backgrounding-occluded-windows'):
            self.assertIn(flag, flags)
        self.assertIn('*NO_THROTTLE_FLAGS', inspect.getsource(browser_module.Browser.ensure))

    def test_browser_wake_sends_lifecycle_active(self):
        calls = []
        owned = browser_module.Browser(None)
        owned.send = lambda tab, method, params=None, timeout=30: calls.append((method, params)) or {}
        owned.wake({'id': 'x'})
        self.assertEqual(calls, [('Page.setWebLifecycleState', {'state': 'active'})])


if __name__ == '__main__':
    unittest.main()
