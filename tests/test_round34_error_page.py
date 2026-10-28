"""Round 34 (user 2026-10-28 19:05: 'ich sehe keine chats mehr ... jetzt dauerhaft'): the window was all black.

Notion's own crash page was hidden by the v6 mask, and error_page_heal could not see it (innerText of a
hidden app is empty), so it reloaded the same crashing chat 4 times and gave up. Fakes only: no real app.
"""
import os
import unittest

from notiontool import error_page_heal as eph

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, 'notiontool', 'web', 'desktop', 'v6.js'), encoding='utf-8', errors='surrogateescape') as fh:
    V6 = fh.read().replace('\r\n', '\n')


class FakeStore:
    def __init__(self):
        self.events = []

    def event(self, kind, **doc):
        self.events.append((kind, doc))

    def kinds(self):
        return [k for k, _ in self.events]


class Ticks:
    """stop_event stand-in: returns True (stop) after n waits."""

    def __init__(self, n):
        self.n = n

    def wait(self, _s):
        self.n -= 1
        return self.n < 0


class FakeSession:
    """state_fn(reloads, presses) -> what the probe sees."""

    def __init__(self, state_fn, press_ok=True):
        self.state_fn = state_fn
        self.press_ok = press_ok
        self.reloads = 0
        self.presses = 0
        self.methods = []

    def send(self, method, params=None, timeout=None):
        self.methods.append(method)
        if method == 'Page.reload':
            self.reloads += 1
            return {}
        if method == 'Runtime.evaluate':
            expr = params['expression']
            if expr == eph.PROBE:
                return {'result': {'value': self.state_fn(self.reloads, self.presses)}}
            if expr == eph.PRESS_BACK:
                self.presses += 1
                return {'result': {'value': 'pressed' if self.press_ok else ''}}
            return {'result': {'value': 'workers=0 caches=0'}}
        raise AssertionError('unexpected method ' + method)


def run(session, ticks, checks):
    store = FakeStore()
    eph.heal(lambda: session, Ticks(ticks), store, checks=checks, wait_s=0, watch_wait_s=0)
    return store


class ProbeTest(unittest.TestCase):
    def test_probe_reads_hidden_text_and_the_crash_icon(self):
        self.assertIn('textContent', eph.PROBE)
        self.assertNotIn('innerText', eph.PROBE)
        self.assertIn('svg.eyes', eph.PROBE)

    def test_finished_layout_is_checked_before_the_crash_text(self):
        # A healthy chat that talks about 'Something went wrong' must never count as the crash page.
        self.assertLess(eph.PROBE.index('.layout-chat'), eph.PROBE.index('Something went wrong'))

    def test_press_back_uses_notions_own_button_and_never_navigates(self):
        self.assertIn('Back to my content', eph.PRESS_BACK)
        self.assertIn('.click()', eph.PRESS_BACK)
        for bad in ('location', 'history.', 'reload', 'navigate'):
            self.assertNotIn(bad, eph.PRESS_BACK)


class HealTest(unittest.TestCase):
    def test_crash_during_start_reloads_once_then_presses_back(self):
        s = FakeSession(lambda r, p: '' if p else 'error')
        store = run(s, ticks=30, checks=48)
        self.assertEqual(s.reloads, 1)
        self.assertEqual(s.presses, 1)
        self.assertEqual(store.kinds(), ['desktop_error_page_reloaded', 'desktop_error_page_back'])

    def test_crash_later_in_the_session_presses_back_without_reload(self):
        s = FakeSession(lambda r, p: '' if p else 'error')
        store = run(s, ticks=10, checks=0)
        self.assertEqual(s.reloads, 0)
        self.assertEqual(s.presses, 1)
        self.assertEqual(store.kinds(), ['desktop_error_page_back'])

    def test_healthy_window_is_never_touched(self):
        s = FakeSession(lambda r, p: '')
        store = run(s, ticks=80, checks=48)
        self.assertEqual((s.reloads, s.presses, store.events), (0, 0, []))

    def test_loading_after_the_start_is_left_alone(self):
        s = FakeSession(lambda r, p: 'loading')
        store = run(s, ticks=40, checks=0)
        self.assertEqual((s.reloads, s.presses, store.events), (0, 0, []))

    def test_stuck_loading_during_start_reloads(self):
        s = FakeSession(lambda r, p: 'loading' if r == 0 else '')
        store = run(s, ticks=30, checks=48)
        self.assertEqual(s.reloads, 1)
        self.assertEqual(s.presses, 0)
        self.assertEqual(store.kinds(), ['desktop_error_page_reloaded'])

    def test_presses_are_limited_and_stuck_is_logged_once(self):
        s = FakeSession(lambda r, p: 'error')
        store = run(s, ticks=60, checks=0)
        self.assertEqual(s.reloads, 0)
        self.assertEqual(s.presses, eph.MAX_PRESSES)
        self.assertEqual(store.kinds().count('desktop_error_page_stuck'), 1)

    def test_a_healthy_page_resets_the_press_limit(self):
        # crash, back, healthy; the user opens the chat again, crash again -> one more press
        seq = iter(['error', 'error', '', '', 'error', 'error'] + [''] * 20)
        s = FakeSession(lambda r, p: next(seq))
        store = run(s, ticks=20, checks=0)
        self.assertEqual(s.presses, 2)
        self.assertEqual(store.kinds(), ['desktop_error_page_back', 'desktop_error_page_back'])

    def test_keeps_watching_after_the_start(self):
        # crash shows up long after the 2 minute start window
        seq = iter([''] * 60 + ['error', 'error'] + [''] * 10)
        s = FakeSession(lambda r, p: next(seq))
        run(s, ticks=72, checks=48)
        self.assertEqual((s.reloads, s.presses), (0, 1))


class V6MaskTest(unittest.TestCase):
    def test_crash_page_is_never_masked(self):
        self.assertIn('function notionCrashed()', V6)
        self.assertIn("if (app.querySelector('svg.eyes')) return true;", V6)
        self.assertIn('var crashed = !chat && !!tid && notionCrashed();', V6)
        self.assertIn('var on = !crashed && !!(chat || tid);', V6)

    def test_boot_cover_goes_on_the_crash_page(self):
        self.assertIn("(chat || crashed || document.querySelector('.layout-content,.notion-frame'))", V6)

    def test_mask_css_rule_is_unchanged(self):
        css = open(os.path.join(ROOT, 'notiontool', 'web', 'desktop', 'v6.css'), encoding='utf-8').read()
        self.assertIn('html.nt-v6-on.nt-v6-mask #notion-app { visibility: hidden !important; }', css)


if __name__ == '__main__':
    unittest.main()
