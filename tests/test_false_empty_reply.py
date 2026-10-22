"""Fix 2: 'empty reply' only from one transcript read that proves the own turn finished without text.

Also: the Main's own turns count against the 6-hour token budget, and nothing continues when it is used up.
"""
import threading
import time

from notiontool import token_budget
from notiontool.notion import turn_state
from notiontool.turn_watch import budget_used_up, charge_main_usage

STEP = 'step-own-164'


def put(entity):
    return {'op': 'put', 'entity': entity}


def transcript(entities, status='idle', seq=None, pending=False):
    session = {} if status is None else {'status': status}
    if seq is not None:
        session['sequence'] = seq
    return {'patches': [put(e) for e in entities], 'session': session, 'session_pending': pending}


TASK = {'id': 't3ec11e437', 'before_seq': 100, 'step_id': STEP}
OWN = {'kind': 'user_message', 'id': STEP, 'sequence': 164, 'content': 'do the fix'}
TOOLS = [{'kind': 'tool_use', 'tool_use_id': 'c%d' % i, 'sequence': 164.000072 + i * 0.000001} for i in range(13)]


def test_replay_t3ec11e437_is_a_verified_reply():
    reply = {'kind': 'assistant_message', 'sequence': 273, 'content': 'Fixed and tested'}
    out = turn_state(transcript([OWN] + TOOLS + [reply], status='idle', seq=273), TASK)
    assert out['state'] == 'done_reply'
    assert out['text'] == 'Fixed and tested'


def test_finished_turn_without_text_is_done_empty():
    done = {'kind': 'turn_completed', 'sequence': 200}
    out = turn_state(transcript([OWN] + TOOLS + [done], status='idle', seq=200), TASK)
    assert out['state'] == 'done_empty'


def test_running_session_is_never_empty():
    out = turn_state(transcript([OWN] + TOOLS, status='running', seq=165), TASK)
    assert out['state'] == 'running'
    out = turn_state(transcript([OWN] + TOOLS, status='idle', seq=165, pending=True), TASK)
    assert out['state'] == 'running'


def test_missing_status_is_unknown_not_empty():
    out = turn_state(transcript([OWN] + TOOLS, status=None), TASK)
    assert out['state'] == 'unknown'


def test_stale_session_sequence_is_running():
    out = turn_state(transcript([OWN] + TOOLS, status='idle', seq=150), TASK)
    assert out['state'] == 'running'


def test_completed_status_alone_never_proves_empty():
    out = turn_state(transcript([OWN] + TOOLS, status='completed', seq=300), TASK)
    assert out['state'] == 'unknown'


def test_pending_tool_uses_neutral_label():
    tool = {'kind': 'tool_use', 'sequence': 170, 'interaction_status': 'pending'}
    out = turn_state(transcript([OWN, tool, {'kind': 'turn_completed', 'sequence': 171}], status='idle', seq=171), TASK)
    assert out['state'] == 'tool_pending'
    assert 'has not finished yet' in out['reason']


def test_own_message_missing_is_not_found():
    out = turn_state(transcript(TOOLS, status='idle', seq=300), TASK)
    assert out['state'] == 'not_found'


class FakeStore:
    def __init__(self, limit=None):
        self.lock = threading.RLock()
        self.docs, self.events = {}, []
        self._settings = {} if limit is None else {'token_budget_6h': limit}

    def get(self, kind, ident, default=None):
        return self.docs.get((kind, ident), default)

    def put(self, kind, doc):
        self.docs[(kind, doc['id'])] = doc

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))

    def settings(self):
        return self._settings


def test_main_first_observation_is_only_a_baseline():
    store = FakeStore()
    assert charge_main_usage(store, {'id': 'm', 'purpose': 'operator'}, None, {'estimated_tokens': 900000, 'turns': 40}) == 0
    assert token_budget.public(store)['used'] == 0


def test_main_turn_charges_context_plus_growth():
    store = FakeStore()
    worker = {'id': 'm', 'purpose': 'operator', 'label': 'Main'}
    used = charge_main_usage(store, worker, {'estimated_tokens': 1000, 'turns': 2}, {'estimated_tokens': 1500, 'turns': 3})
    assert used == 500 + 1000
    report = token_budget.public(store)
    assert report['used'] == 1500 and report['main_tokens'] == 1500 and report['coworker_tokens'] == 0


def test_budget_used_up_blocks_continue():
    store = FakeStore(limit=1000)
    assert not budget_used_up(store)
    token_budget.charge(store, {'id': 'w', 'purpose': 'worker'}, 1000, 0, turn=True)
    assert budget_used_up(store)
    assert token_budget.public(store)['blocked'] is True
