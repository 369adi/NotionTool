"""wake() reports a tab as frozen only after two message-channel misses while the plain evaluation answers.

A busy (plain evaluation times out) or unreachable (DevTools error) tab is never frozen; it is logged as
edge_tab_probe instead. Fakes only: no real Edge, no real Notion, no network and no app port.
"""
import inspect
import threading
import time
import unittest
from unittest import mock

from notiontool import edge_transport as edge_module
from notiontool.edge_transport import EdgeTransport


class FakeStore:
    def __init__(self):
        self.events = []

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))

    def settings(self):
        return {'task_timeout': 900}


class ProbeBrowser:
    """Answers the plain evaluation and the message-channel check from separate scripts.

    plain: 'ok', 'timeout' (TimeoutError), 'devtools_timeout' (browser.send's own RuntimeError),
    'slow' (sleeps the given limit, then times out) or 'error' (connection closed).
    channel: 'ok' or 'miss'.
    """

    def __init__(self, plain=(), channel=(), on_channel=None):
        self.plain, self.channel = list(plain), list(channel)
        self.on_channel = on_channel
        self.calls = []  # (kind, timeout, monotonic time)
        self.woken = 0
        self.sent = []

    def evaluate(self, tab, expression, timeout=30):
        if expression == EdgeTransport.PLAIN_SCRIPT:
            self.calls.append(('plain', timeout, time.monotonic()))
            answer = self.plain.pop(0) if self.plain else 'ok'
            if answer == 'timeout':
                raise TimeoutError('timed out')
            if answer == 'devtools_timeout':
                raise RuntimeError('DevTools call timed out')
            if answer == 'slow':
                time.sleep(timeout)
                raise TimeoutError('timed out')
            if answer == 'error':
                raise ConnectionResetError('websocket connection closed')
            return 1
        if 'MessageChannel' in expression:
            self.calls.append(('channel', timeout, time.monotonic()))
            answer = self.channel.pop(0) if self.channel else 'ok'
            if self.on_channel is not None:
                self.on_channel()
            if answer == 'miss':
                raise TimeoutError('timed out')
            return 1
        raise AssertionError('unexpected evaluation: ' + expression)

    def kinds(self):
        return [kind for kind, _, _ in self.calls]

    def wake(self, tab, timeout=5):
        self.woken += 1

    def send(self, tab, method, params=None, timeout=30):
        self.sent.append(method)
        return {}


def make(**answers):
    store, browser = FakeStore(), ProbeBrowser(**answers)
    edge = EdgeTransport(store, None, 'acct', object(), browser=browser)
    edge.stall_wake_after = 0
    edge.probe_retry_s = 0.01
    return edge, store, browser


def events(store, kind):
    return [fields for name, fields in store.events if name == kind]


class WakeFrozenTest(unittest.TestCase):
    def test_responsive_tab_is_active_and_never_frozen(self):
        edge, store, browser = make()
        self.assertEqual(edge.tab_frozen('tab'), 'active')
        self.assertTrue(edge.wake('tab', force=True, reason='stalled runInferenceTranscript'))
        self.assertEqual(store.events, [])
        self.assertEqual(browser.kinds(), ['plain', 'channel', 'plain', 'channel'])
        self.assertEqual(browser.woken, 1)

    def test_busy_twice_is_not_frozen_and_logs_probe(self):
        edge, store, browser = make(plain=['timeout', 'devtools_timeout'])
        self.assertTrue(edge.wake('tab', force=True, reason='stalled runInferenceTranscript'))
        self.assertEqual(events(store, 'edge_tab_woken'), [])
        probes = events(store, 'edge_tab_probe')
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0]['verdict'], 'busy')
        self.assertEqual(probes[0]['account_id'], 'acct')
        self.assertEqual(probes[0]['reason'], 'stalled runInferenceTranscript')
        self.assertIsInstance(probes[0]['elapsed_ms'], int)
        self.assertEqual(browser.kinds(), ['plain', 'plain'])
        self.assertEqual(browser.woken, 1)

    def test_errors_twice_are_unreachable_not_frozen(self):
        edge, store, _ = make(plain=['error', 'error'])
        self.assertEqual(edge.tab_frozen('tab'), 'unreachable')
        edge, store, _ = make(plain=['error', 'error'])
        self.assertTrue(edge.wake('tab', force=True, reason='x' * 200))
        self.assertEqual(events(store, 'edge_tab_woken'), [])
        probes = events(store, 'edge_tab_probe')
        self.assertEqual([p['verdict'] for p in probes], ['unreachable'])
        self.assertEqual(len(probes[0]['reason']), 80)

    def test_plain_fine_and_two_channel_misses_are_frozen(self):
        edge, store, browser = make(channel=['miss', 'miss'])
        self.assertTrue(edge.wake('tab', force=True, reason='stalled runInferenceTranscript'))
        woken = events(store, 'edge_tab_woken')
        self.assertEqual(len(woken), 1)
        self.assertIs(woken[0]['frozen'], True)
        self.assertEqual(woken[0]['reason'], 'stalled runInferenceTranscript')
        self.assertEqual(events(store, 'edge_tab_probe'), [])
        self.assertEqual(browser.kinds(), ['plain', 'channel', 'plain', 'channel'])
        self.assertEqual(browser.woken, 1)  # a frozen tab is still woken

    def test_two_failed_checks_in_a_row_are_frozen(self):
        edge, _, _ = make(channel=['miss', 'miss'])
        self.assertEqual(edge.tab_frozen('tab'), 'frozen')

    def test_one_miss_then_answer_is_not_frozen(self):
        edge, store, _ = make(channel=['miss', 'ok'])
        self.assertEqual(edge.tab_frozen('tab'), 'active')
        edge, store, _ = make(channel=['miss', 'ok'])
        self.assertTrue(edge.wake('tab', force=True))
        self.assertEqual(store.events, [])

    def test_miss_and_busy_is_not_frozen(self):
        edge, _, _ = make(plain=['ok', 'timeout'], channel=['miss'])
        self.assertEqual(edge.tab_frozen('tab'), 'busy')
        edge, _, _ = make(plain=['timeout', 'ok'], channel=['miss'])
        self.assertEqual(edge.tab_frozen('tab'), 'busy')

    def test_cancel_stops_the_second_check(self):
        cancel = threading.Event()
        edge, store, browser = make(channel=['miss', 'miss'], on_channel=cancel.set)
        self.assertTrue(edge.wake('tab', force=True, reason='stalled', cancel=cancel))
        self.assertEqual(browser.kinds(), ['plain', 'channel'])
        self.assertEqual(events(store, 'edge_tab_woken'), [])

    def test_cancel_during_retry_pause_returns_at_once(self):
        cancel = threading.Event()
        edge, _, browser = make(channel=['miss', 'miss'])
        edge.probe_retry_s = 5.0
        threading.Timer(0.05, cancel.set).start()
        started = time.monotonic()
        self.assertEqual(edge.tab_frozen('tab', cancel), 'miss')
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(browser.kinds(), ['plain', 'channel'])

    def test_both_checks_share_one_time_budget(self):
        edge, _, browser = make(plain=['slow', 'slow'])
        edge.probe_timeout, edge.probe_retry_s, edge.probe_budget_s = 0.3, 0.1, 0.65
        started = time.monotonic()
        self.assertEqual(edge.tab_frozen('tab'), 'busy')
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual(len(browser.calls), 2)
        self.assertLess(browser.calls[1][1], 0.3)  # second check only gets what is left
        fresh = EdgeTransport(FakeStore(), None, 'acct', object(), browser=ProbeBrowser())
        self.assertLessEqual(fresh.probe_budget_s, 6.0)
        self.assertGreater(fresh.probe_retry_s, 0)
        self.assertLessEqual(fresh.probe_retry_s, 1.0)

    def test_frozen_event_is_rate_limited(self):
        edge, store, _ = make(channel=['miss', 'miss', 'miss', 'miss'])
        self.assertTrue(edge.wake('tab', force=True, reason='a'))
        self.assertTrue(edge.wake('tab', force=True, reason='b'))
        self.assertEqual(len(events(store, 'edge_tab_woken')), 1)

    def test_probe_event_has_its_own_timer(self):
        edge, store, _ = make(plain=['timeout', 'timeout', 'timeout', 'timeout'], channel=['miss', 'miss'])
        self.assertTrue(edge.wake('tab', force=True, reason='busy 1'))
        self.assertTrue(edge.wake('tab', force=True, reason='busy 2'))
        self.assertEqual(len(events(store, 'edge_tab_probe')), 1)
        self.assertTrue(edge.wake('tab', force=True, reason='frozen'))
        self.assertEqual(len(events(store, 'edge_tab_woken')), 1)

    def test_periodic_wake_never_probes(self):
        edge, store, browser = make(channel=['miss', 'miss'])
        self.assertTrue(edge.wake('tab'))
        self.assertEqual(browser.calls, [])
        self.assertEqual(store.events, [])

    def test_probe_is_a_page_task_and_never_navigates(self):
        self.assertIn('MessageChannel', EdgeTransport.PROBE_SCRIPT)
        self.assertIn('postMessage', EdgeTransport.PROBE_SCRIPT)
        edge, _, browser = make()
        edge.wake('tab', force=True)
        self.assertNotIn('Page.navigate', browser.sent)

    def test_stalled_stream_passes_cancel_to_wake(self):
        source = inspect.getsource(EdgeTransport._chunks)
        self.assertEqual(source.count("self.wake(target, force=True, reason='stalled ' + path, cancel=cancel)"), 2)

    def test_robots_txt_tab_transport_is_kept(self):
        with mock.patch.object(edge_module, 'Browser') as browser_cls:
            EdgeTransport(FakeStore(), None, 'acct', object())
        kwargs = browser_cls.call_args.kwargs
        self.assertEqual(kwargs['start_url'], EdgeTransport._base + '/robots.txt')
        self.assertTrue(kwargs['edge_only'])


if __name__ == '__main__':
    unittest.main()
