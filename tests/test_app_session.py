import threading
import pytest
from notiontool import app_session


class Store:
    def __init__(self):
        self.docs = {'a1': {'id': 'a1', 'identity': {'user_id': 'u1'}}}
        self.events = []
    def get(self, kind, ident, default=None):
        d = self.docs.get(ident)
        return dict(d) if d else default
    def put(self, kind, doc):
        self.docs[doc['id']] = doc
    def event(self, kind, **f):
        self.events.append(kind)


class Sessions:
    def __init__(self):
        self.data = {'a1': {'cookies': [{'name': 'token_v2', 'value': 'old'}]}}
    def load(self, k):
        return self.data.get(k)
    def save(self, k, v):
        self.data[k] = v


class Edge:
    ready_process = object()
    def identity(self):
        return {}


class Client:
    def __init__(self, user):
        self.transport = Edge()
        self.user = user
    def identity(self):
        return {'user_id': self.user}


class Adapter:
    def __init__(self, user='u1'):
        self.store, self.sessions, self.lock = Store(), Sessions(), threading.RLock()
        self.transports = {'a1': object()}
        self.edge = Edge()
        self.edge_transports = {'a1': self.edge}
        self.user = user
        self.cleared = []
    def clear_account_pause(self, a):
        self.cleared.append(a)
    def client(self, a):
        return Client(self.user)


COOKIES = [{'name': 'token_v2', 'value': 'new', 'domain': '.notion.so'}]


def test_adopt_installs_cookies_for_edge_and_resumes():
    ad = Adapter()
    result = app_session.adopt_app_session(ad, 'a1', cookies=COOKIES)
    assert result == {'account_id': 'a1', 'transport': 'Edge', 'verified': True}
    assert ad.sessions.data['a1']['cookies'] == COOKIES
    assert ad.edge.ready_process is None and 'a1' not in ad.transports
    assert ad.cleared == ['a1'] and 'account_session_adopted' in ad.store.events


def test_adopt_rejects_other_user_and_restores_old_session():
    ad = Adapter(user='someone-else')
    with pytest.raises(ValueError):
        app_session.adopt_app_session(ad, 'a1', cookies=COOKIES)
    assert ad.sessions.data['a1']['cookies'][0]['value'] == 'old'


def test_app_cookies_filters_domains_and_needs_token():
    class S:
        def __init__(self, url):
            pass
        def send(self, m, p, t):
            return {'cookies': COOKIES + [{'name': 'x', 'value': 'y', 'domain': '.evil.com'}]}
        def close(self):
            pass
    targets = [{'type': 'page', 'url': 'https://www.' + 'notion' + '.so/x', 'webSocketDebuggerUrl': 'ws://x'}]
    got = app_session.app_cookies(http_get=lambda u: targets, connect=S)
    assert [c['domain'] for c in got] == ['.notion.so']
