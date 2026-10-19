"""Unit tests for notiontool.desktop_app with fake spawn, DevTools HTTP and session; no process, no network."""
import unittest
from pathlib import Path

from notiontool import desktop_app as da
from notiontool import notion_shell
from notiontool.desktop_app import DesktopApp

EXE = r'C:\Program Files\WindowsApps\Notion\Notion.exe'
PROFILE = r'C:\Users\me\AppData\Local\NotionTool-App\profile'
WS = 'ws://127.0.0.1:9333/devtools/page/%s'
VERSION = {'Browser': 'Chrome/152', 'User-Agent': 'Notion/7.39.0'}


class FakeProc:
    pid = 4242


class FakeSession:
    def __init__(self):
        self.calls, self.closed, self.close_calls = [], False, 0

    def send(self, method, params=None, timeout=15):
        self.calls.append((method, params))
        if method == 'Page.addScriptToEvaluateOnNewDocument':
            return {'identifier': 'script-7'}
        return {}

    def close(self):
        self.close_calls += 1
        self.closed = True


class Fakes:
    def __init__(self, targets, version_after=1):
        self.targets, self.version_after = targets, version_after
        self.spawned, self.urls, self.connected, self.terminated, self.sleeps = [], [], [], [], []
        self.session = FakeSession()
        self.t = 0.0

    def spawn(self, argv):
        self.spawned.append(list(argv))
        return FakeProc()

    def http_get(self, url):
        self.urls.append(url)
        if url.endswith('/json/version'):
            hits = sum(1 for u in self.urls if u.endswith('/json/version'))
            return VERSION if hits > self.version_after else None
        if url.endswith('/json/list'):
            return self.targets
        raise AssertionError('unexpected url ' + url)

    def connect(self, ws_url):
        self.connected.append(ws_url)
        return self.session

    def terminate(self, pid):
        self.terminated.append(pid)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds

    def clock(self):
        return self.t

    def app(self, **kw):
        return DesktopApp(EXE, PROFILE, spawn=self.spawn, http_get=self.http_get, connect=self.connect,
                          terminate=self.terminate, sleep=self.sleep, clock=self.clock, **kw)


def target(ident, url, kind='page'):
    return {'id': ident, 'type': kind, 'url': url, 'webSocketDebuggerUrl': WS % ident}


MIXED = [
    target('blank', 'about:blank'),
    target('file', 'file:///C:/index.html'),
    target('tools', 'devtools://devtools/bundled/inspector.html'),
    target('worker', 'https://www.notion.so/sw.js', kind='service_worker'),
    target('notion', 'https://www.notion.so/ai'),
    target('second', 'https://www.notion.com/x'),
]


class StartTests(unittest.TestCase):
    def test_spawn_argv_wait_and_target(self):
        fakes = Fakes(MIXED)
        app = fakes.app()
        info = app.start()
        self.assertEqual(fakes.spawned, [[EXE, '--user-data-dir=' + PROFILE, '--remote-debugging-port=9333']])
        self.assertEqual(fakes.urls[:3], ['http://127.0.0.1:9333/json/version'] * 2 + ['http://127.0.0.1:9333/json/list'])
        self.assertEqual(len(fakes.sleeps), 1)  # first /json/version was None
        self.assertEqual(fakes.connected, [WS % 'notion'])
        self.assertEqual(info, dict(pid=4242, port=9333, target_id='notion', ws_url=WS % 'notion', ready=True))
        self.assertTrue(app.alive)

    def test_only_blank_is_not_ready(self):
        fakes = Fakes([target('blank', 'about:blank'), target('file', 'file:///x.html')], version_after=0)
        info = fakes.app().start()
        self.assertFalse(info['ready'])
        self.assertEqual(info['target_id'], '')
        self.assertEqual(fakes.connected, [])

    def test_timeout(self):
        fakes = Fakes(MIXED, version_after=10 ** 6)
        with self.assertRaises(TimeoutError):
            fakes.app().start(timeout=1)
        self.assertEqual(fakes.connected, [])

    def test_other_host_ws_refused(self):
        bad = dict(target('notion', 'https://notion.so/'), webSocketDebuggerUrl='ws://10.0.0.5:9333/devtools/page/notion')
        fakes = Fakes([bad], version_after=0)
        self.assertFalse(fakes.app().start()['ready'])
        self.assertEqual(fakes.connected, [])

    def test_custom_port(self):
        fakes = Fakes([dict(target('n', 'https://notion.com/'), webSocketDebuggerUrl='ws://127.0.0.1:9400/devtools/page/n')],
                      version_after=0)
        app = DesktopApp(EXE, PROFILE, 9400, spawn=fakes.spawn, http_get=fakes.http_get, connect=fakes.connect,
                         sleep=fakes.sleep, clock=fakes.clock)
        self.assertTrue(app.start()['ready'])
        self.assertIn('--remote-debugging-port=9400', fakes.spawned[0])
        self.assertTrue(all(u.startswith('http://127.0.0.1:9400/') for u in fakes.urls))


class InjectCloseTests(unittest.TestCase):
    def test_inject_order(self):
        fakes = Fakes(MIXED, version_after=0)
        app = fakes.app()
        app.start()
        source = 'window.__ntDesk = window.__ntDesk || {};'
        self.assertEqual(app.inject(source), 'script-7')
        self.assertEqual(app.script_id, 'script-7')
        self.assertEqual(fakes.session.calls, [
            ('Page.enable', None), ('Runtime.enable', None), ('Runtime.addBinding', {'name': 'ntHarness'}),
            ('Page.addScriptToEvaluateOnNewDocument', {'source': source}),
            ('Runtime.evaluate', {'expression': source})])

    def test_inject_refuses_secrets(self):
        fakes = Fakes(MIXED, version_after=0)
        app = fakes.app(secrets=('s3cr3t-value',))
        app.start()
        for source in ('var a = "s3cr3t-value";', 'fetch("http://127.0.0.1:8765")'):
            with self.assertRaises(ValueError):
                app.inject(source)
        self.assertEqual(fakes.session.calls, [])

    def test_inject_needs_session(self):
        with self.assertRaises(RuntimeError):
            Fakes(MIXED).app().inject('x')

    def test_close_idempotent_and_own_pid_only(self):
        fakes = Fakes(MIXED, version_after=0)
        app = fakes.app()
        app.start()
        app.close()
        app.close()
        self.assertEqual(fakes.session.close_calls, 1)
        self.assertEqual(fakes.terminated, [4242])
        self.assertFalse(app.alive)

    def test_close_without_start_terminates_nothing(self):
        fakes = Fakes(MIXED)
        app = fakes.app()
        app.close()
        self.assertEqual(fakes.terminated, [])

    def test_alive_follows_session(self):
        fakes = Fakes(MIXED, version_after=0)
        app = fakes.app()
        self.assertFalse(app.alive)
        app.start()
        fakes.session.closed = True
        self.assertFalse(app.alive)


class HelperTests(unittest.TestCase):
    def test_is_our_process(self):
        ours = '"%s" --user-data-dir=%s --remote-debugging-port=9333' % (EXE, PROFILE)
        self.assertTrue(DesktopApp.is_our_process(ours, PROFILE))
        self.assertTrue(DesktopApp.is_our_process(ours.upper(), PROFILE.lower()))
        self.assertTrue(DesktopApp.is_our_process([EXE, '--user-data-dir=' + PROFILE], PROFILE))
        self.assertFalse(DesktopApp.is_our_process('"%s"' % EXE, PROFILE))  # the user's normal client
        self.assertFalse(DesktopApp.is_our_process(ours, ''))
        self.assertFalse(DesktopApp.is_our_process(None, PROFILE))

    def test_reuses_shell_session_class(self):
        self.assertIs(da.CdpSession, notion_shell.CdpSession)
        text = Path(da.__file__).read_text(encoding='utf-8')
        self.assertNotIn('class CdpSession', text)
        self.assertNotIn('shell=True', text)

    def test_files_lf_utf8(self):
        for path in (Path(da.__file__), Path(__file__)):
            raw = path.read_bytes()
            self.assertNotIn(b'\r', raw)
            raw.decode('utf-8')


if __name__ == '__main__':
    unittest.main()
