import threading
import time

import pytest

from notiontool import token_budget, ai_disable_detector
from notiontool.engine import RateLimited


class FakeStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.docs, self.events = {}, []
        self.db = None

    def get(self, kind, ident, default=None):
        return self.docs.get((kind, ident), default)

    def all(self, kind):
        return [v for (k, _), v in self.docs.items() if k == kind]

    def put(self, kind, doc):
        self.docs[(kind, doc['id'])] = doc

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))

    def settings(self):
        return {}


def test_window_is_six_hours_and_aligned():
    now = time.time()
    start = token_budget.window_start(now)
    assert start <= now < start + token_budget.WINDOW_SECONDS
    assert time.localtime(start).tm_hour in (0, 6, 12, 18)


def test_charge_persists_and_blocks_at_limit():
    store = FakeStore()
    worker = dict(id='w1', label='Main', purpose='main', context_usage=dict(estimated_tokens=1000))
    token_budget.charge_send(store, worker, 'x' * 300)
    token_budget.charge_reply(store, dict(id='c1', purpose='worker'), 'y' * 30)
    info = token_budget.public(store)
    assert info['used'] == 1110 and info['turns'] == 1
    assert info['main_tokens'] == 1100 and info['coworker_tokens'] == 10
    store.docs = {k: v for k, v in store.docs.items() if k[0] == token_budget.KIND}  # chats deleted
    assert token_budget.public(store)['used'] == 1110
    token_budget.charge(store, worker, 45_000_000)
    with pytest.raises(RateLimited):
        token_budget.refuse_if_exhausted(store)


def test_detector_reports_transition_once():
    store = FakeStore()
    store.put('accounts', dict(id='a1', email='x@y.de', state='ready'))
    ai_disable_detector.scan(store)
    store.put('accounts', dict(id='a1', email='x@y.de', state='ai_disabled'))
    ai_disable_detector.scan(store)
    ai_disable_detector.scan(store)
    assert [k for k, _ in store.events].count('ai_disabled_detected') == 1
    rep = ai_disable_detector.reports(store)[0]
    assert rep['causes'] and rep['account'] == 'x@y.de'
