import threading
import unittest

from notiontool import window_login as wl
from notiontool.server import Application


class Store:
    def __init__(self, account, settings=None):
        self.accounts = {account['id']: dict(account)}
        self._settings = {'public_mcp_url': 'https://mcp.example/mcp'} if settings is None else settings
        self.events = []

    def get(self, table, ident, default=None):
        return self.accounts.get(ident, default) if table == 'accounts' else default

    def put(self, table, value):
        self.accounts[value['id']] = dict(value)
        return value

    def settings(self):
        return self._settings

    def event(self, kind, *args, **kwargs):
        self.events.append(kind)


class Adapter:
    def __init__(self, store):
        self.store, self.calls = store, []

    def connect_bridge(self, account_id, url, token, read_auto=True, write_auto=True):
        self.calls.append(('connect', url, read_auto, write_auto))
        account = self.store.accounts[account_id]
        account['bridge_connected'] = True

    def ensure_team(self, account_id, create_main=False):
        self.calls.append(('team', create_main))
        return {'main_chats': 1, 'welcome_chats': 11}


class Fake:
    first_login_setup = Application.first_login_setup

    def __init__(self, account, settings=None):
        self.store = Store(account, settings)
        self.adapter = Adapter(self.store)
        self.bridge_token = 'tok'
        self.tunnels = 0

    def start_tunnel(self):
        self.tunnels += 1


class FirstLoginSetupTest(unittest.TestCase):
    def test_connects_mcp_automatically_and_creates_main_and_coworkers_once(self):
        app = Fake({'id': 'a', 'state': 'ready'})
        result = app.first_login_setup('a')
        self.assertEqual(result['mcp'], 'connected')
        self.assertEqual(app.adapter.calls, [('connect', 'https://mcp.example/mcp', True, True), ('team', True)])
        self.assertTrue(app.store.accounts['a']['first_setup_done'])
        self.assertEqual(app.first_login_setup('a')['skipped'], 'already set up')
        self.assertEqual(len(app.adapter.calls), 2)

    def test_existing_mcp_is_left_alone(self):
        app = Fake({'id': 'a', 'state': 'ready', 'bridge_connected': True})
        app.first_login_setup('a')
        self.assertEqual(app.adapter.calls, [('team', True)])

    def test_not_ready_account_is_skipped(self):
        app = Fake({'id': 'a', 'state': 'ai_disabled'})
        self.assertIn('skipped', app.first_login_setup('a'))
        self.assertEqual(app.adapter.calls, [])

    def test_missing_url_keeps_setup_open_for_next_sign_in(self):
        app = Fake({'id': 'a', 'state': 'ready'}, settings={})
        result = app.first_login_setup('a')
        self.assertIn('mcp_error', result)
        self.assertEqual(app.tunnels, 1)
        self.assertEqual(app.adapter.calls, [('team', True)])
        self.assertFalse(app.store.accounts['a'].get('first_setup_done'))


class WindowLoginCallbackTest(unittest.TestCase):
    def test_window_sign_in_hands_cookies_to_the_harness(self):
        from tests.test_window_login import COOKIES, FakeSession, HOST, page
        got, done = [], threading.Event()

        def on_signed_in(cookies):
            got.append(cookies)
            done.set()

        assist = wl.LoginAssist(None, http_get=lambda url: [page('a', HOST + '/login')],
                                connect=lambda url: FakeSession(flag=1), capture=lambda: COOKIES,
                                event=lambda *a: None, on_signed_in=on_signed_in)
        self.assertEqual(assist.tick(), 'requested')
        self.assertTrue(done.wait(2))
        self.assertEqual(got, [COOKIES])


if __name__ == '__main__':
    unittest.main()
