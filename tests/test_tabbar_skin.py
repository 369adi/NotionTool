import unittest

from notiontool import tabbar_skin

WS = 'ws://127.0.0.1:9333/devtools/page/AAA'
TAB = dict(type='page', title='Tab Bar', id='AAA', url='file:///C:/x/Notion/resources/app.asar/tabs.html',
           webSocketDebuggerUrl=WS)
PAGE_URL = 'https' + '://' + 'workspace.example' + '/page'
PAGE = dict(type='page', title='NotionTool - Main', id='BBB', url=PAGE_URL,
            webSocketDebuggerUrl='ws://127.0.0.1:9333/devtools/page/BBB')


class FakeSession:
    def __init__(self):
        self.sent, self.closed = [], False

    def send(self, method, params=None, timeout=None):
        self.sent.append((method, params))
        return {}

    def close(self):
        self.closed = True


class FakeApp:
    def __init__(self, lists):
        self.lists, self.port, self.sessions, self.urls = list(lists), 9333, [], []

    def _http_get(self, url):
        self.urls.append(url)
        return self.lists.pop(0) if self.lists else []

    def _connect(self, ws):
        session = FakeSession()
        session.ws = ws
        self.sessions.append(session)
        return session


class TabbarSkinTest(unittest.TestCase):
    def test_only_our_tab_bar_target(self):
        self.assertTrue(tabbar_skin.is_tab_bar(TAB, 9333))
        self.assertFalse(tabbar_skin.is_tab_bar(PAGE, 9333))
        self.assertFalse(tabbar_skin.is_tab_bar(dict(TAB, url=PAGE_URL), 9333))
        self.assertFalse(tabbar_skin.is_tab_bar(dict(TAB, webSocketDebuggerUrl='ws://10.0.0.2:9333/x'), 9333))
        self.assertFalse(tabbar_skin.is_tab_bar(TAB, 9444))
        self.assertFalse(tabbar_skin.is_tab_bar(dict(TAB, type='worker'), 9333))

    def test_apply_injects_now_and_for_new_documents(self):
        app = FakeApp([[PAGE, TAB]])
        holder = {}
        result = tabbar_skin.apply(app._http_get, app._connect, 9333, holder)
        self.assertEqual(result, dict(ok=True, target_id='AAA'))
        self.assertEqual(app.urls, ['http://127.0.0.1:9333/json/list'])
        session = app.sessions[0]
        self.assertEqual(session.ws, WS)
        methods = [m for m, _ in session.sent]
        self.assertEqual(methods, ['Page.enable', 'Page.addScriptToEvaluateOnNewDocument', 'Runtime.evaluate'])
        source = session.sent[2][1]['expression']
        self.assertIn('nt-tabbar-skin', source)
        self.assertIn('#04060c', source)
        self.assertIn('opacity:0', source)
        self.assertIs(holder['session'], session)
        self.assertFalse(session.closed)  # kept open: CDP drops new-document scripts with the session

    def test_reapply_closes_the_old_session(self):
        app = FakeApp([[TAB], [TAB]])
        holder = {}
        tabbar_skin.apply(app._http_get, app._connect, 9333, holder)
        tabbar_skin.apply(app._http_get, app._connect, 9333, holder)
        self.assertTrue(app.sessions[0].closed)
        self.assertIs(holder['session'], app.sessions[1])

    def test_no_target_and_errors_never_raise(self):
        app = FakeApp([[PAGE]])
        self.assertEqual(tabbar_skin.apply(app._http_get, app._connect, 9333), dict(ok=False, reason='no tab bar target'))

        def boom(ws):
            raise OSError('socket gone')
        result = tabbar_skin.apply(lambda url: [TAB], boom, 9333)
        self.assertFalse(result['ok'])
        self.assertIn('socket gone', result['reason'])
        self.assertFalse(tabbar_skin.apply(lambda url: None, boom, 9333)['ok'])

    def test_start_retries_until_the_tab_bar_exists(self):
        app = FakeApp([[], [PAGE], [PAGE, TAB]])
        sleeps = []
        holder = tabbar_skin.start(app, retries=5, interval=0.5, sleep=sleeps.append, thread=False)
        self.assertEqual(holder['result'], dict(ok=True, target_id='AAA', attempt=3))
        self.assertEqual(sleeps, [0.5, 0.5])
        self.assertEqual(len(app.sessions), 1)

    def test_start_gives_up_quietly(self):
        app = FakeApp([])
        holder = tabbar_skin.start(app, retries=3, interval=0, sleep=lambda s: None, thread=False)
        self.assertEqual(holder['result']['attempt'], 3)
        self.assertFalse(holder['result']['ok'])


if __name__ == '__main__':
    unittest.main()
