import threading
import unittest

from notiontool import window_login as wl

HOST = 'https://www.' + 'notion' + '.so'
WS = 'ws://127.0.0.1:9333/devtools/page/%s'
COOKIES = [
    dict(name='token_v2', value='secret', domain='.www.' + 'notion.so', path='/', secure=True, httpOnly=True,
         sameSite='Lax', expires=1900000000, session=False),
    dict(name='notion_user_id', value='u1', domain='www.' + 'notion.so', path='/', expires=-1, session=True),
    dict(name='other', value='x', domain='example.org', path='/'),
]


class FakeSession:
    def __init__(self, flag=0):
        self.calls = []
        self.flag = flag
        self.closed = False

    def send(self, method, params=None, timeout=15):
        self.calls.append((method, params or {}))
        if method == 'Runtime.evaluate' and (params or {}).get('returnByValue'):
            value, self.flag = self.flag, 0
            return {'result': {'value': value}}
        return {}

    def close(self):
        self.closed = True


def page(ident, url):
    return dict(type='page', id=ident, url=url, webSocketDebuggerUrl=WS % ident)


class WindowLoginTest(unittest.TestCase):
    def test_cookie_params_keep_only_notion(self):
        params = wl.to_cookie_params(COOKIES)
        self.assertEqual([p['name'] for p in params], ['token_v2', 'notion_user_id'])
        self.assertEqual(params[0]['expires'], 1900000000)
        self.assertNotIn('expires', params[1])
        self.assertTrue(wl.has_token(params))

    def test_login_target(self):
        self.assertEqual(wl.login_target([page('a', HOST + '/login')])['id'], 'a')
        # Add account: login tab next to a workspace tab is hooked as well
        self.assertEqual(wl.login_target([page('a', HOST + '/login'), page('b', HOST + '/Budget-0123456789abcdef')])['id'], 'a')
        self.assertIsNone(wl.login_target([page('b', HOST + '/Budget-0123456789abcdef')]))
        self.assertIsNone(wl.login_target(None))

    def test_add_account_dialog(self):
        # The 'Add an account' dialog lives in the signed-in workspace page: hook that page, exact Google tile only
        self.assertEqual(wl.add_account_target([page('b', HOST + '/Budget-0123456789abcdef')])['id'], 'b')
        self.assertIsNone(wl.add_account_target([page('a', HOST + '/login')]))
        self.assertIn('add an account', wl.ADD_HOOK)
        self.assertIn("!== 'google'", wl.ADD_HOOK)
        self.assertIn(wl.FLAG, wl.ADD_HOOK)

    def test_transfer_sets_cookies_and_navigates(self):
        s = FakeSession()
        self.assertEqual(wl.transfer(s, COOKIES), 2)
        methods = [m for m, _ in s.calls]
        self.assertEqual(methods, ['Network.enable', 'Network.setCookies', 'Page.navigate'])
        with self.assertRaises(ValueError):
            wl.transfer(FakeSession(), COOKIES[1:])

    def test_assist_hooks_login_page_and_signs_in_on_click(self):
        sessions, events, done = [], [], threading.Event()

        def connect(url):
            sessions.append(FakeSession(flag=123))
            return sessions[-1]

        def event(kind, message):
            events.append(kind)
            if kind in ('window_login_done', 'window_login_failed'):
                done.set()

        assist = wl.LoginAssist(None, http_get=lambda url: [page('a', HOST + '/login')], connect=connect,
                                capture=lambda: COOKIES, event=event)
        self.assertEqual(assist.tick(), 'requested')
        self.assertTrue(done.wait(2))
        self.assertEqual(events, ['window_login_started', 'window_login_done'])
        methods = [m for m, _ in sessions[0].calls]
        self.assertIn('Page.addScriptToEvaluateOnNewDocument', methods)
        self.assertIn('Network.setCookies', methods)
        self.assertEqual(assist.tick(), 'waiting')
        self.assertEqual(len(sessions), 1)

    def test_assist_idle_without_notion_page(self):
        assist = wl.LoginAssist(None, http_get=lambda url: [page('b', 'https://example.com/x')],
                                connect=lambda u: self.fail('must not connect'), capture=list, event=lambda *a: None)
        self.assertEqual(assist.tick(), 'idle')

    def test_assist_hooks_add_account_dialog_when_signed_in(self):
        sent = []

        class Session:
            closed = False

            def send(self, method, params=None):
                sent.append((method, (params or {}).get('source') or (params or {}).get('expression') or ''))
                return {'result': {'value': 0}}

            def close(self):
                pass

        assist = wl.LoginAssist(None, http_get=lambda url: [page('b', HOST + '/Budget-0123456789abcdef')],
                                connect=lambda u: Session(), capture=list, event=lambda *a: None)
        self.assertEqual(assist.tick(), 'waiting')
        self.assertIn(('Runtime.evaluate', wl.ADD_HOOK), sent)

    def test_hook_matches_button_texts(self):
        self.assertIn('continue in browser', wl.HOOK)
        self.assertIn(wl.FLAG, wl.HOOK)


if __name__ == '__main__':
    unittest.main()
