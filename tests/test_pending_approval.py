"""A turn paused on a tool approval click is named as such, not left as an anonymous unconfirmed completion.

Root cause of the blocked planning run on 2026-10-19: Notion ended the stream while an nt_write_file call
waited for approval (interaction_status 'pending'); the harness then reported 'completion unconfirmed'
for 17 minutes and failed the dependants.
"""
import unittest
from unittest import mock

from notiontool.engine import PendingRemote
from notiontool.notion import pending_tool_label
from test_stream_guard import STEP, T1, Base, FakeClient, user


def pending_tool(seq, tool='nt_write_file'):
    return {'op': 'put', 'entity': {'id': 'toolu_%d' % seq, 'kind': 'tool', 'sequence': seq, 'name': 'callFunction',
                                    'tool_use_id': 'toolu_%d' % seq, 'interaction_status': 'pending',
                                    'text': {'running': [['MCP tool - ' + tool]]}}}


def finished_tool(seq):
    return {'op': 'put', 'entity': {'id': 'x%d' % seq, 'kind': 'tool', 'sequence': seq, 'name': 'mcp_run_tool',
                                    'payload': {'type': 'mcp', 'tool_name': 'nt_shell'}}}


class PendingApprovalTests(Base):
    def recover(self, transcript):
        task = dict(id='task1', run_id='run1', thread_id=T1, step_id=STEP, before_seq=5)
        with self.assertRaises(PendingRemote):
            self.adapter(FakeClient([transcript])).recover(dict(self.worker), task, None)

    def test_pending_tool_is_recorded_once(self):
        transcript = {'patches': [user(6), finished_tool(7), pending_tool(8)], 'session': {'status': 'running'}}
        with mock.patch.object(self.store, 'event', wraps=self.store.event) as event:
            self.recover(transcript)
            self.recover(transcript)
        self.assertEqual(self.store.get('tasks', 'task1')['awaiting_approval'], 'nt_write_file')
        kinds = [c.args[0] for c in event.call_args_list]
        self.assertEqual(kinds.count('turn_awaiting_approval'), 1)

    def test_no_pending_tool_records_nothing(self):
        self.recover({'patches': [user(6), finished_tool(7)], 'session': {'status': 'running'}})
        self.assertNotIn('awaiting_approval', self.store.get('tasks', 'task1'))

    def test_pending_tool_before_own_turn_is_ignored(self):
        self.recover({'patches': [pending_tool(4), user(6)], 'session': {'status': 'running'}})
        self.assertNotIn('awaiting_approval', self.store.get('tasks', 'task1'))

    def test_label_prefers_payload_tool_name(self):
        self.assertEqual(pending_tool_label({'payload': {'tool_name': 'nt_shell'}}), 'nt_shell')
        self.assertEqual(pending_tool_label({'text': {'running': [['run nt_write_file']]}}), 'nt_write_file')
        self.assertEqual(pending_tool_label({'name': 'callFunction'}), 'callFunction')


if __name__ == '__main__':
    unittest.main()
