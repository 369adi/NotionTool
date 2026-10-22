"""H5: a pending tool call gets an honest label; 'approval' only with explicit evidence (branch t9c43ad7)."""
from notiontool.notion import (APPROVAL_WAIT_TEXT, TOOL_NOT_STARTED_TEXT, TOOL_RUNNING_TEXT, pending_tool_state,
                               record_state, tool_waiting)


def test_pending_without_evidence_is_not_started():
    entity = {'kind': 'tool_use', 'interaction_status': 'pending'}
    assert tool_waiting(entity)
    assert pending_tool_state(entity) == TOOL_NOT_STARTED_TEXT


def test_started_tool_is_running():
    assert pending_tool_state({'interaction_status': 'pending', 'payload': {'started_at': 5}}) == TOOL_RUNNING_TEXT


def test_approval_only_with_evidence():
    assert pending_tool_state({'interaction_status': 'awaiting_approval'}) == APPROVAL_WAIT_TEXT
    assert pending_tool_state({'payload': {'requires_approval': True}}) == APPROVAL_WAIT_TEXT
    assert tool_waiting({'requires_confirmation': True})
    assert not tool_waiting({'kind': 'tool_use', 'interaction_status': 'done'})


def test_record_state_reads_data_and_never_guesses():
    assert record_state(None) == 'unknown'
    assert record_state({'data': {'latest_user_or_trigger_time': 5, 'last_turn_outcome': {'completed_time': 3}}}) == 'running'
    assert record_state({'latest_user_or_trigger_time': 5, 'last_turn_outcome': {'completed_time': 9}}) == 'finished'
    assert record_state({'latest_user_or_trigger_time': 'x', 'last_turn_outcome': {}}) == 'unknown'
