"""Low-RAM guard for the NotionTool window (user 2026-10-28 06:19)."""
import threading
import unittest
from unittest import mock

from notiontool import desktop_guard as g


class FakeSession:
    def __init__(self):
        self.sent = []

    def send(self, method, params=None, timeout=15):
        self.sent.append((method, params))
        return {}


class Ctl:
    def __init__(self):
        self.shell = type('S', (), {})()
        self.shell.session = FakeSession()


class GuardTests(unittest.TestCase):
    def make(self, t):
        ctl = Ctl()
        pids = []
        guard = g.DesktopGuard(ctl, lambda: pids.append(1) or [], threading.Event(), clock=lambda: t[0], icon=None)
        return ctl, pids, guard

    def test_pressure_only_when_ram_low_and_rate_limited(self):
        t = [0.0]
        ctl, _, guard = self.make(t)
        with mock.patch.object(g, 'free_mb', return_value=800), mock.patch.object(g, 'raise_priority', return_value=0):
            guard.step(); t[0] = 30; guard.step()
            self.assertEqual(ctl.shell.session.sent, [('Memory.simulatePressureNotification', {'level': 'critical'})])
            t[0] = 200; guard.step()
            self.assertEqual(len(ctl.shell.session.sent), 2)

    def test_no_pressure_with_enough_ram(self):
        t = [0.0]
        ctl, _, guard = self.make(t)
        with mock.patch.object(g, 'free_mb', return_value=6000), mock.patch.object(g, 'raise_priority', return_value=0):
            guard.step()
        self.assertEqual(ctl.shell.session.sent, [])

    def test_priority_refresh_is_rare(self):
        t = [0.0]
        _, pids, guard = self.make(t)
        with mock.patch.object(g, 'free_mb', return_value=6000):
            guard.step(); t[0] = 60; guard.step(); t[0] = 400; guard.step()
        self.assertEqual(len(pids), 2)

    def test_wired_into_controller_and_argv(self):
        from pathlib import Path
        root = Path(g.__file__).resolve().parent
        self.assertIn('DesktopGuard(self, lambda: our_processes(self.profile_dir), self.stop_event)',
                      (root / 'desktop_shell.py').read_text(encoding='utf-8'))
        self.assertIn('--disable-features=SpareRendererForSitePerProcess', (root / 'desktop_app.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
