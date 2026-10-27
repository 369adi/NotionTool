"""Several Notion tabs: the overlay goes to the tab the user sees (desktop_app.py)."""
from notiontool.desktop_app import DesktopApp

PORT = 9333
HOST_PART = 'www.notion' + '.so'


def target(ident):
    url = 'https:' + '//' + HOST_PART + '/' + ident
    return {'id': ident, 'type': 'page', 'url': url,
            'webSocketDebuggerUrl': 'ws://127.0.0.1:%d/devtools/page/%s' % (PORT, ident)}


class FakeSession:
    def __init__(self, ws, state):
        self.ws, self.state, self.closed = ws, state, False

    def send(self, method, params=None, timeout=None):
        ident = self.ws.rsplit('/', 1)[-1]
        if method == 'Runtime.evaluate' and (params or {}).get('expression') == 'document.visibilityState':
            return {'result': {'type': 'string', 'value': self.state['vis'].get(ident, 'hidden')}}
        return {}

    def close(self):
        self.closed = True


def make(idents, vis):
    state = {'vis': dict(vis), 'opened': []}

    def http_get(url):
        if url.endswith('/json/version'):
            return {'Browser': 'x'}
        return [target(i) for i in idents]

    def connect(ws):
        session = FakeSession(ws, state)
        state['opened'].append(session)
        return session

    app = DesktopApp('notion.exe', 'profile', PORT, spawn=lambda argv: type('P', (), {'pid': 1})(),
                     http_get=http_get, connect=connect, sleep=lambda s: None)
    return app, state


def test_single_tab_attaches_without_probing():
    app, state = make(['a'], {})
    assert app.start()['target_id'] == 'a'
    assert len(state['opened']) == 1


def test_attaches_to_the_visible_tab_not_the_first():
    app, state = make(['a', 'b'], {'b': 'visible'})
    assert app.start()['target_id'] == 'b'
    assert app.session.ws.endswith('/b')
    assert all(s.closed for s in state['opened'] if s is not app.session)


def test_all_hidden_falls_back_to_first_tab():
    app, _ = make(['a', 'b'], {})
    assert app.start()['target_id'] == 'a'


def test_other_tab_visible_only_when_ours_is_hidden():
    app, state = make(['a', 'b'], {'a': 'visible'})
    app.start()
    assert app.other_tab_visible() is False
    state['vis'] = {'b': 'visible'}
    assert app.other_tab_visible() is True
    state['vis'] = {}
    assert app.other_tab_visible() is False  # window minimised: every tab hidden, nothing moves
