import json
import threading

import pytest

from notiontool import token_budget
from notiontool.notion_shell import pin_config_body, PIN_DEFAULT_MODEL


def body(*values, base=None, thread=''):
    tr = [{'type': 'config', 'value': dict({'model': 'albuquerque-quinn'} if base is None else base)}]
    for v in values:
        tr.append({'type': 'updated-config', 'value': v})
    tr.append({'type': 'user', 'value': [['hi']]})
    data = {'transcript': tr}
    if thread:
        data['threadId'] = thread
    return json.dumps(data)


def last_cfg(text):
    steps = [s for s in json.loads(text)['transcript'] if s['type'] in ('config', 'updated-config')]
    return steps[-1]['value']


# Owner rule 2026-10-25: a model/effort the user picked stays; an empty updated-config step means
# "no change" and is never treated as Auto (live bug: the Main's xhigh was overwritten with high).

def test_empty_steps_keep_the_chats_own_config():
    assert pin_config_body(body({}), {}) is None
    assert pin_config_body(body({}, {}, {}), {}) is None


def test_base_effort_from_the_user_is_never_overwritten():
    base = {'model': 'albuquerque-quinn', 'reasoningEffort': 'xhigh', 'modelFromUser': True}
    assert pin_config_body(body({}, {}, {}, base=base), {}) is None


def test_effort_pick_in_an_earlier_step_stays():
    assert pin_config_body(body({'reasoningEffort': 'max'}, {}, {'useWebSearch': True}), {}) is None
    assert pin_config_body(body({'reasoningEffort': 'medium'}), {}) is None
    assert pin_config_body(body({'model': 'achira-donut', 'reasoningEffort': 'low'}, {}), {}) is None


def test_auto_model_is_filled_and_keeps_the_users_effort():
    v = last_cfg(pin_config_body(body({'reasoningEffort': 'max'}, {}, base={'model': 'auto'}), {}))
    assert v['model'] == PIN_DEFAULT_MODEL and v['reasoningEffort'] == 'max' and v['modelFromUser'] is True


def test_missing_model_gets_opus_without_inventing_an_effort():
    v = last_cfg(pin_config_body(body({}, base={}), {}))
    assert v['model'] == PIN_DEFAULT_MODEL and 'reasoningEffort' not in v
    v = last_cfg(pin_config_body(body(base={'type': 'workflow'}), {}))
    assert v['model'] == PIN_DEFAULT_MODEL and 'reasoningEffort' not in v


def test_explicit_choice_passes_and_is_learned_per_chat():
    pin = {}
    assert pin_config_body(body({'model': 'achira-donut', 'reasoningEffort': 'max'}, thread='ab-12'), pin) is None
    assert pin['threads']['ab12'] == {'model': 'achira-donut', 'effort': 'max'}
    # The same chat later shows Auto: its own model and effort come back.
    v = last_cfg(pin_config_body(body({'model': 'auto', 'reasoningEffort': 'auto'}, thread='ab12'), pin))
    assert v['model'] == 'achira-donut' and v['reasoningEffort'] == 'max'


def test_auto_string_replaced_and_bad_input_ignored():
    v = last_cfg(pin_config_body(body({'model': 'auto', 'reasoningEffort': 'auto'}), {}))
    assert v['model'] == PIN_DEFAULT_MODEL and v['reasoningEffort'] == 'high'
    assert pin_config_body('not json', {}) is None
    assert pin_config_body(None, {}) is None
    assert pin_config_body(json.dumps({'transcript': [{'type': 'user'}]}), {}) is None


class Store:
    def __init__(self, settings=None):
        self.lock = threading.RLock()
        self.data = {}
        self.events = []
        self._settings = settings or {}

    def settings(self):
        return self._settings

    def get(self, kind, ident):
        return self.data.get((kind, ident))

    def put(self, kind, record):
        self.data[(kind, record['id'])] = record

    def event(self, kind, **kw):
        self.events.append(kind)


W = {'id': 'w1', 'purpose': 'worker'}
M = {'id': 'm1', 'purpose': 'main'}


def test_burst_ok_throttle_pause():
    from notiontool.engine import RateLimited
    s = Store()
    now = 1_800_000_000.0
    token_budget.charge(s, W, 1_000_000, 0, turn=True, now=now)
    assert token_budget.burst_state(s, now)['level'] == 'ok'
    token_budget.refuse_if_burst(s, W, now)
    token_budget.charge(s, W, 4_500_000, 0, turn=True, now=now + 1)
    st = token_budget.burst_state(s, now + 2)
    assert st['level'] == 'throttle' and st['wait'] > 0
    with pytest.raises(RateLimited) as e:
        token_budget.refuse_if_burst(s, W, now + 2)
    assert e.value.free and e.value.scope == 'thread'
    token_budget.refuse_if_burst(s, W, now + 30)  # spacing passed
    token_budget.refuse_if_burst(s, M, now + 2)  # Main never slowed
    token_budget.charge(s, W, 3_000_000, 0, turn=True, now=now + 3)
    assert token_budget.burst_state(s, now + 40)['level'] == 'pause'
    with pytest.raises(RateLimited):
        token_budget.refuse_if_burst(s, W, now + 40)
    assert token_budget.burst_state(s, now + 16 * 60)['level'] == 'ok'


def test_burst_limits_from_settings():
    s = Store({'burst_soft_15m': 100})
    token_budget.charge(s, W, 200, 0, turn=True, now=1_800_000_000.0)
    assert token_budget.burst_state(s, 1_800_000_001.0)['level'] == 'throttle'
