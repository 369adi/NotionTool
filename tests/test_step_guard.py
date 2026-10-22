import threading

import pytest

from notiontool import step_guard, token_budget
from notiontool.engine import RateLimited
from notiontool.turn_watch import TurnWatch

NOW = 1_800_000_000.0
A = 'acc1'
W = dict(id=A + ':t1', account_id=A, purpose='worker', context_usage=dict(estimated_tokens=100_000))
W2 = dict(id=A + ':t2', account_id=A, purpose='worker', context_usage=dict(estimated_tokens=100_000))
M = dict(id=A + ':main', account_id=A, purpose='operator', thread_id='main-thread',
         context_usage=dict(estimated_tokens=2_000_000))


class Store:
    def __init__(self, settings=None):
        self.lock = threading.RLock()
        self.docs, self.events = {}, []
        self._settings = settings or {}

    def get(self, kind, ident, default=None):
        return self.docs.get((kind, ident), default)

    def all(self, kind):
        return [v for (k, _), v in self.docs.items() if k == kind]

    def put(self, kind, doc):
        self.docs[(kind, doc['id'])] = doc

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))

    def settings(self):
        return self._settings


def stop(store, purpose, now):
    token_budget.clear_hard_stop_cache()
    return token_budget.hard_stop_state(store, purpose, now)


def test_steps_soft_then_hard_stops_every_chat():
    s = Store()
    step_guard.add_steps(s, W, 400, now=NOW)
    assert step_guard.state(s, NOW + 1)['level'] == 'ok'
    step_guard.add_steps(s, M, 150, now=NOW + 2)  # Main steps count too
    assert step_guard.state(s, NOW + 3)['level'] == 'throttle'
    assert token_budget.burst_state(s, NOW + 3)['level'] == 'throttle'
    assert not stop(s, 'main', NOW + 3)['stop']
    step_guard.add_steps(s, W, 250, now=NOW + 4)
    st = step_guard.state(s, NOW + 5)
    assert st['level'] == 'pause' and st['steps_15m'] == 800
    assert token_budget.burst_state(s, NOW + 5)['level'] == 'pause'
    assert stop(s, 'main', NOW + 5)['stop'] and stop(s, 'main', NOW + 5)['source'] == 'steps'
    assert stop(s, 'worker', NOW + 5)['stop']
    assert 'all chats pause' in token_budget.limit_stop_text(60, 'steps')
    # 15 min later the 15 min window is free again, the 60 min sum (800) is below 1000
    assert step_guard.state(s, NOW + 16 * 60)['level'] == 'ok'


def test_weighted_steps_hit_before_plain_steps():
    s = Store()
    step_guard.add_steps(s, M, 300, now=NOW)  # 300 x 200k cap = 60M
    assert step_guard.state(s, NOW + 1)['level'] == 'ok'
    step_guard.add_steps(s, M, 100, now=NOW + 1)  # 80M >= soft 70M
    assert step_guard.state(s, NOW + 2)['level'] == 'throttle'
    step_guard.add_steps(s, M, 100, now=NOW + 2)  # 100M = hard
    assert step_guard.state(s, NOW + 3)['level'] == 'pause'


def test_limits_from_settings_and_accounts_apart():
    s = Store({'step_hard_15m': 10, 'step_soft_15m': 5})
    step_guard.add_steps(s, W, 12, now=NOW)
    other = dict(W, id='acc2:t1', account_id='acc2')
    assert step_guard.state(s, NOW + 1, account='acc2')['level'] == 'ok'
    assert step_guard.state(s, NOW + 1)['level'] == 'pause'  # worst account
    assert step_guard.start_wait(s, other, NOW + 1) == (0, '')
    assert step_guard.start_wait(s, W, NOW + 1)[0] > 0


def test_provider_error_pauses_all_then_half_speed():
    s = Store()
    text = 'UserRateLimitResponse: Please try again later. at fairUseAIRateLimit (entitlementHelpers.js)'
    assert step_guard.is_provider_limit(text)
    assert step_guard.is_provider_limit('Notion AI was disabled for this workspace due to high usage')
    assert not step_guard.is_provider_limit('HTTP 500 internal error')
    assert step_guard.note_provider_limit(s, M, text, now=NOW, key='th:e1')
    assert not step_guard.note_provider_limit(s, M, text, now=NOW + 5, key='th:e1')  # same entry once
    assert [k for k, _ in s.events].count('provider_rate_limited') == 1
    st = step_guard.state(s, NOW + 10)
    assert st['level'] == 'pause' and 800 < st['wait'] <= 900
    assert stop(s, 'main', NOW + 10)['stop'] and stop(s, 'worker', NOW + 10)['stop']
    with pytest.raises(RateLimited):
        token_budget.refuse_if_burst(s, W, NOW + 10)
    after = NOW + 901
    assert step_guard.state(s, after)['level'] == 'ok'
    step_guard.add_steps(s, W, 260, now=after)  # half of soft 500 = 250
    assert step_guard.state(s, after + 1)['level'] == 'throttle'
    assert step_guard.state(s, after + 1)['limits']['max_parallel_turns'] == 2
    step_guard.add_steps(s, W, 120, now=after + 2)  # half of hard 750 = 375
    assert step_guard.state(s, after + 3)['level'] == 'pause'


def test_start_spacing_parallel_and_starts_per_5_min():
    s = Store({'start_spacing_s': 45, 'max_parallel_turns': 2, 'starts_5m': 3})
    assert step_guard.start_wait(s, W, NOW) == (0, '')
    step_guard.note_start(s, W, NOW)
    wait, why = step_guard.start_wait(s, W2, NOW + 10)
    assert 30 <= wait <= 36 and 'spacing' in why
    with pytest.raises(RateLimited) as e:
        token_budget.refuse_if_burst(s, W2, NOW + 10)
    assert e.value.free and e.value.scope == 'thread'
    assert step_guard.start_wait(s, W2, NOW + 50) == (0, '')
    token_budget.refuse_if_burst(s, M, NOW + 10)  # the Main is never delayed at start
    # two coworker turns still stream (retry: older remote_done_at than the new sent_at)
    s.put('tasks', dict(id='x1', worker_id=A + ':t3', state='running', sent_at=NOW, remote_done_at=NOW - 100))
    s.put('tasks', dict(id='x2', worker_id=A + ':t4', state='running', sent_at=NOW))
    s.put('tasks', dict(id='x3', worker_id=A + ':t5', state='running', sent_at=NOW, remote_done_at=NOW + 5))
    s.put('tasks', dict(id='x4', worker_id=W2['id'], state='running', sent_at=NOW))  # its own chat
    assert step_guard.running_turns(s, A, W2['id']) == 2
    wait, why = step_guard.start_wait(s, W2, NOW + 50)
    assert wait == 30 and 'max 2' in why
    s.docs = {k: v for k, v in s.docs.items() if k[0] != 'tasks'}
    step_guard.note_start(s, W, NOW + 100)
    step_guard.note_start(s, W, NOW + 150)
    wait, why = step_guard.start_wait(s, W2, NOW + 200)
    assert wait == 30 and 'starts in 5 min' in why


def test_count_new_counts_stream_steps_once():
    s = Store()
    counted = step_guard.count_new(s, W, [1, 2, 3], 0, NOW)
    counted = step_guard.count_new(s, W, [1, 2, 3], counted, NOW)
    counted = step_guard.count_new(s, W, [1, 2, 3, 4, 5], counted, NOW)
    assert counted == 5 and step_guard.state(s, NOW + 1)['steps_15m'] == 5


def entity(i, kind, **extra):
    return dict(op='put', entity=dict(id='e%d' % i, kind=kind, sequence=i, **extra))


def test_turn_watch_counts_main_steps_and_sees_provider_error():
    s = Store()
    clock = [NOW]
    watch = TurnWatch(s, None, None, clock=lambda: clock[0])
    first = dict(patches=[entity(1, 'user_message'), entity(2, 'tool_call', tool_use_id='a'),
                          entity(3, 'tool_result', tool_use_id='a'), entity(4, 'assistant_message')])
    assert watch.count_steps(M, first) == 0  # baseline only
    second = dict(patches=first['patches'] + [entity(5, 'user_message'), entity(6, 'tool_call', tool_use_id='b'),
                                              entity(7, 'tool_result', tool_use_id='b'), entity(8, 'tool_call'),
                                              entity(9, 'assistant_message')])
    assert watch.count_steps(M, second) == 3
    assert step_guard.state(s, NOW + 1)['steps_15m'] == 3
    # a coworker transcript never counts steps here (notion.py counts them from the stream) ...
    assert watch.count_steps(dict(W, thread_id='t1'), second) == 0
    assert watch.count_steps(dict(W, thread_id='t1'), second) == 0
    # ... but its rate-limit error pauses every chat
    err = entity(10, 'error', message='UserRateLimitResponse: Please try again later.', created_at=NOW * 1000)
    watch.count_steps(dict(W, thread_id='t1'), dict(patches=second['patches'] + [err]))
    assert step_guard.state(s, NOW + 2)['level'] == 'pause'
    # an old error entry (older than the cooldown) is ignored
    s2 = Store()
    watch2 = TurnWatch(s2, None, None, clock=lambda: NOW + 5000)
    watch2.count_steps(M, dict(patches=[err]))
    assert step_guard.state(s2, NOW + 5001)['level'] == 'ok'


def test_state_fail_open():
    class Broken(Store):
        def get(self, kind, ident, default=None):
            raise RuntimeError('db gone')

        def put(self, kind, doc):
            raise RuntimeError('db gone')
    assert step_guard.state(Broken(), NOW)['level'] == 'ok'
    assert step_guard.add_steps(Broken(), W, 5, NOW) == 0
