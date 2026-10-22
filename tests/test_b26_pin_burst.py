import json
import threading

import pytest

from notiontool import token_budget
from notiontool.notion_shell import pin_config_body, PIN_DEFAULT_MODEL


def body(*values):
    tr = [{'type': 'config', 'value': {'model': 'albuquerque-quinn'}}]
    for v in values:
        tr.append({'type': 'updated-config', 'value': v})
    tr.append({'type': 'user', 'value': [['hi']]})
    return json.dumps({'transcript': tr})


def last_cfg(text):
    steps = [s for s in json.loads(text)['transcript'] if s['type'] == 'updated-config']
    return steps[-1]['value']


def test_auto_gets_default_opus_high():
    out = pin_config_body(body({}), {})
    v = last_cfg(out)
    assert v['model'] == PIN_DEFAULT_MODEL and v['reasoningEffort'] == 'high' and v['modelFromUser'] is True


def test_explicit_choice_passes_and_is_learned():
    pin = {}
    assert pin_config_body(body({'model': 'achira-donut', 'reasoningEffort': 'max'}), pin) is None
    assert pin == {'model': 'achira-donut', 'effort': 'max'}
    v = last_cfg(pin_config_body(body({'modelFromUser': True}), pin))
    # The model pick is kept; an old effort pick is never carried over (no accidental 'low').
    assert v['model'] == 'achira-donut' and v['reasoningEffort'] == 'high'


def test_old_low_effort_never_sticks():
    pin = {}
    assert pin_config_body(body({'model': 'achira-donut', 'reasoningEffort': 'low'}), pin) is None
    v = last_cfg(pin_config_body(body({}), pin))
    assert v['reasoningEffort'] == 'high'


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
