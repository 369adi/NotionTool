"""P8b: ui_mode 'notion' starts the Notion chat shell, 'classic' does not, and the overlay offers no
action the server's shell handlers refuse (the harness never creates chats).

Fakes only: no network, no real window, no Notion calls, no chat creation.
"""
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from notiontool import notion_shell as ns
from notiontool import server as srv

ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / 'notiontool' / 'web' / 'notion_shell' / 'overlay.js'
CSS_PATH = ROOT / 'notiontool' / 'web' / 'notion_shell' / 'overlay.css'
CREATE_ACTIONS = ('new_welcome_chat', 'create_chat', 'new_chat', 'create_main', 'main/create')


class FakeThread:
    started = []

    def __init__(self, target=None, daemon=None, name=None, **kw):
        self.target, self.name = target, name

    def start(self):
        FakeThread.started.append(self)


def fake_app(settings):
    app = mock.MagicMock(name='Application')
    app.store.settings.return_value = dict(settings)
    app.store.all.return_value = []
    app.token = 'tok'
    return app


def launch_threads(settings):
    """Run the real Application.launch on a fake app; nothing listens, nothing starts."""
    FakeThread.started = []
    app = fake_app(settings)
    with mock.patch.object(srv, 'ThreadingHTTPServer', mock.MagicMock()), \
            mock.patch.object(srv.threading, 'Thread', FakeThread), \
            mock.patch.object(srv, 'Updater', mock.MagicMock()), \
            mock.patch('notiontool.turn_watch.TurnWatch', mock.MagicMock()):
        url = srv.Application.launch(app, 0)
    return app, url, list(FakeThread.started)


class UiModeStartTests(unittest.TestCase):
    def shell_threads(self, app, threads):
        return [t for t in threads if t.name == 'notion-shell' or t.target is app.start_notion_shell]

    def test_notion_mode_starts_the_shell_thread(self):
        app, url, threads = launch_threads({'ui_mode': 'notion'})
        shell = self.shell_threads(app, threads)
        self.assertEqual(len(shell), 1)
        self.assertIs(shell[0].target, app.start_notion_shell)
        self.assertTrue(url.startswith('http://127.0.0.1:'))

    def test_classic_mode_does_not_start_the_shell(self):
        for settings in ({'ui_mode': 'classic'}, {}):
            app, url, threads = launch_threads(settings)
            self.assertEqual(self.shell_threads(app, threads), [], settings)
            self.assertTrue(any(t.name == 'local-http' for t in threads))
            app.start_notion_shell.assert_not_called()

    def test_start_notion_shell_starts_shell_without_create_handler(self):
        started = []

        class Recorder:
            def __init__(self, browser, account_id, handlers, **kw):
                started.append((browser, account_id, dict(handlers)))

            def start(self):
                started.append('start')

        browser = object()
        app = fake_app({'ui_mode': 'notion', 'harness_account_id': 'a1'})
        app.adapter = SimpleNamespace(edge_transport=lambda account_id: SimpleNamespace(browser=browser))
        app.shell_handlers = lambda: srv.Application.shell_handlers(app)
        with mock.patch.object(ns, 'NotionShell', Recorder), \
                mock.patch.object(ns, 'shell_secrets', lambda server: ('s', 'k')), \
                mock.patch('notiontool.harness_account.selected_account', lambda store: 'a1'):
            shell = srv.Application.start_notion_shell(app)
        self.assertIsInstance(shell, Recorder)
        self.assertEqual(started[-1], 'start')
        self.assertIs(started[0][0], browser)
        self.assertEqual(started[0][1], 'a1')
        self.assertNotIn('new_welcome_chat', started[0][2])

    def test_overlay_assets_are_loaded_from_the_package(self):
        self.assertEqual(Path(ns.ASSET_DIR).resolve(), JS_PATH.parent.resolve())
        self.assertTrue(JS_PATH.is_file())
        self.assertTrue(CSS_PATH.is_file())


class OverlayActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = JS_PATH.read_text(encoding='utf-8')
        cls.css = CSS_PATH.read_text(encoding='utf-8')
        cls.server_handlers = srv.Application.shell_handlers(mock.MagicMock(name='server'))

    def used_actions(self):
        return set(re.findall(r"call\('([A-Za-z_]+)'", self.js))

    def test_overlay_has_no_chat_creation_action(self):
        for word in CREATE_ACTIONS:
            self.assertNotIn(word, self.js)
            self.assertNotIn(word, self.css)
        self.assertNotIn('startWelcomeChat', self.js)
        self.assertIn('function newChatRefused()', self.js)
        self.assertIn('New chats are not created here.', self.js)

    def test_overlay_action_list_matches_python(self):
        match = re.search(r'var ACTIONS = \[(.*?)\];', self.js, re.S)
        self.assertIsNotNone(match)
        self.assertEqual(tuple(re.findall(r"'([a-z_]+)'", match.group(1))), tuple(ns.ACTIONS))
        self.assertNotIn('new_welcome_chat', ns.ACTIONS)

    def test_every_overlay_action_is_accepted_by_the_server_shell_handlers(self):
        used = self.used_actions()
        self.assertTrue(used)
        self.assertTrue(used <= set(ns.ACTIONS), used - set(ns.ACTIONS))
        self.assertTrue(used <= set(self.server_handlers), used - set(self.server_handlers))
        fake = SimpleNamespace(handlers={a: (lambda args: 'ok') for a in self.server_handlers},
                               _reply=lambda ident, payload: (ident, payload))
        for action in sorted(used):
            ident, payload = ns.NotionShell.dispatch(fake, '{"id": "x", "action": "%s", "args": {}}' % action)
            self.assertEqual(payload, {'ok': True, 'result': 'ok'}, action)

    def test_shell_refuses_chat_creation_on_its_own(self):
        self.assertNotIn('new_welcome_chat', ns.default_handlers(mock.MagicMock(name='server')))
        fake = SimpleNamespace(handlers=dict(ns.default_handlers(mock.MagicMock(name='server'))),
                               _reply=lambda ident, payload: (ident, payload))
        for action in ('new_welcome_chat', 'create_chat', 'new_chat'):
            ident, payload = ns.NotionShell.dispatch(fake, '{"id": "c", "action": "%s"}' % action)
            self.assertEqual(payload, {'ok': False, 'error': 'Action not allowed'})


if __name__ == '__main__':
    unittest.main()
