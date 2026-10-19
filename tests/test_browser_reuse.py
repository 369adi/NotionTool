import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from notiontool import browser as browser_module
from notiontool.browser import Browser


def version(port, browser_id='live'):
    return {'Browser': 'Edg/1', 'webSocketDebuggerUrl': f'ws://127.0.0.1:{port}/devtools/browser/{browser_id}'}


class BrowserReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SimpleNamespace(directory=self.tmp.name, settings=lambda: {})
        self.browser = Browser(self.store, profile_name='harness')
        self.profile = Path(self.tmp.name) / 'browser-profiles' / 'harness'
        self.profile.mkdir(parents=True)
        self.launched = []
        popen = mock.patch.object(browser_module.subprocess, 'Popen', side_effect=self.fake_popen)
        self.popen = popen.start()
        self.addCleanup(popen.stop)
        kill = mock.patch.object(browser_module.os, 'kill', create=True)
        self.kill = kill.start()
        self.addCleanup(kill.stop)

    def fake_popen(self, args, **kwargs):
        (self.profile / 'DevToolsActivePort').write_text('9555\n/devtools/browser/fresh\n')
        process = mock.Mock()
        process.poll.return_value = None
        self.launched.append(process)
        return process

    def write_stale_file(self):
        (self.profile / 'DevToolsActivePort').write_text('5000\n/devtools/browser/old\n')

    def process(self, profile, ports, explicit=0):
        cmd = f'"msedge.exe" --remote-debugging-port={explicit} "--user-data-dir={profile}" --no-first-run'
        return {'pid': 4242, 'cmd': cmd, 'ports': ports}

    def assert_nothing_killed(self):
        self.kill.assert_not_called()
        for process in self.launched:
            process.terminate.assert_not_called()
            process.kill.assert_not_called()

    def test_stale_file_with_healthy_running_instance_reuses_port_and_launches_nothing(self):
        self.write_stale_file()
        probes = {5000: None, 6000: version(6000)}
        with mock.patch.object(Browser, 'list_processes', return_value=[self.process(self.profile, [6000])]), \
             mock.patch.object(Browser, 'probe', side_effect=lambda port: probes.get(port)):
            result = self.browser.ensure()
        self.assertEqual(result, version(6000))
        self.assertEqual(self.browser.port, 6000)
        self.popen.assert_not_called()
        self.assertTrue((self.profile / 'DevToolsActivePort').exists())
        self.assert_nothing_killed()

    def test_valid_file_of_running_instance_is_reused_without_process_lookup(self):
        (self.profile / 'DevToolsActivePort').write_text('7100\n/devtools/browser/live\n')
        with mock.patch.object(Browser, 'list_processes') as listing, \
             mock.patch.object(Browser, 'probe', side_effect=lambda port: version(port) if port == 7100 else None):
            self.browser.ensure()
        self.assertEqual(self.browser.port, 7100)
        listing.assert_not_called()
        self.popen.assert_not_called()

    def test_stale_file_with_no_instance_launches(self):
        self.write_stale_file()
        with mock.patch.object(Browser, 'list_processes', return_value=[]), \
             mock.patch.object(Browser, 'probe', return_value=None), \
             mock.patch.object(Browser, 'http', return_value=version(9555, 'fresh')):
            result = self.browser.ensure()
        self.popen.assert_called_once()
        self.assertEqual(self.browser.port, 9555)
        self.assertEqual(result, version(9555, 'fresh'))
        self.assert_nothing_killed()

    def test_unhealthy_instance_does_not_kill_healthy_one(self):
        self.write_stale_file()
        processes = [self.process(self.profile, [7000]), self.process(self.profile, [7001])]
        probes = {7001: version(7001)}
        with mock.patch.object(Browser, 'list_processes', return_value=processes), \
             mock.patch.object(Browser, 'probe', side_effect=lambda port: probes.get(port)), \
             mock.patch.object(browser_module.subprocess, 'run') as run:
            self.browser.ensure()
        self.assertEqual(self.browser.port, 7001)
        self.popen.assert_not_called()
        run.assert_not_called()
        self.assert_nothing_killed()

    def test_reused_port_going_bad_switches_to_healthy_instance_without_kill(self):
        self.browser.port = 7000
        probes = {7001: version(7001)}
        with mock.patch.object(Browser, 'list_processes', return_value=[self.process(self.profile, [7001])]), \
             mock.patch.object(Browser, 'probe', side_effect=lambda port: probes.get(port)):
            self.browser.ensure()
        self.assertEqual(self.browser.port, 7001)
        self.popen.assert_not_called()
        self.assert_nothing_killed()

    def test_healthy_owned_process_is_not_terminated(self):
        owned = mock.Mock()
        owned.poll.return_value = None
        self.browser.process, self.browser.port = owned, 9555
        with mock.patch.object(Browser, 'http', return_value=version(9555)):
            self.browser.ensure()
        owned.terminate.assert_not_called()
        owned.kill.assert_not_called()
        self.popen.assert_not_called()

    def test_close_does_not_stop_a_reused_browser(self):
        self.browser.port = 6000
        self.browser.close()
        self.assertEqual(self.browser.port, 0)
        self.assert_nothing_killed()

    def test_different_profile_is_not_reused(self):
        self.write_stale_file()
        other = Path(self.tmp.name) / 'browser-profiles' / 'other'
        with mock.patch.object(Browser, 'list_processes', return_value=[self.process(other, [8000], explicit=8000)]), \
             mock.patch.object(Browser, 'probe', side_effect=lambda port: version(port) if port == 8000 else None), \
             mock.patch.object(Browser, 'http', return_value=version(9555, 'fresh')):
            self.browser.ensure()
        self.popen.assert_called_once()
        self.assertEqual(self.browser.port, 9555)
        self.assert_nothing_killed()

    def test_running_ports_parses_explicit_and_listening_ports_for_profile_only(self):
        processes = [
            {'pid': 1, 'cmd': f'msedge.exe --remote-debugging-port=0 --user-data-dir={self.profile}', 'ports': 6100},
            {'pid': 2, 'cmd': f'msedge.exe --remote-debugging-port=6200 --user-data-dir="{self.profile}"', 'ports': [6200, 6201]},
            {'pid': 3, 'cmd': f'msedge.exe --remote-debugging-port=6300 --user-data-dir={self.profile}-x', 'ports': [6300]},
        ]
        with mock.patch.object(Browser, 'list_processes', return_value=processes):
            self.assertEqual(self.browser.running_ports(self.profile), [6100, 6200, 6201])

    def test_probe_rejects_invalid_ports_without_network(self):
        with mock.patch.object(browser_module.urllib.request, 'urlopen') as urlopen:
            self.assertIsNone(self.browser.probe(0))
            self.assertIsNone(self.browser.probe(70000))
        urlopen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
