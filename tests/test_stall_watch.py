"""Tests for notiontool.stall_watch (pure, in-memory data only)."""

import copy
import unittest

from notiontool.stall_watch import (decide_stalled, event_text, last_progress, next_state,
                                    on_empty_reply, turn_id)

T0 = 1000.0


def turn(tid='turn1', thread='th1', task='task1', worker='w1', label='Coworker 5', **extra):
    item = dict(turn_id=tid, thread_id=thread, task_id=task, worker_id=worker, worker_label=label,
                sent_at=T0, last_tool_at=0, last_output_at=0, last_event_at=0)
    item.update(extra)
    return item


def kinds(actions):
    return [a['kind'] for a in actions]


class StallTimingTests(unittest.TestCase):
    def test_no_stall_before_120_s(self):
        self.assertEqual(decide_stalled([turn()], [], T0 + 119.9, {}), [])

    def test_stall_at_120_s(self):
        actions = decide_stalled([turn()], [], T0 + 120, {})
        self.assertEqual(kinds(actions), ['stop_turn', 'requeue'])

    def test_open_gateway_call_blocks_even_after_14_min(self):
        calls = [dict(thread_id='th1', tool='run_shell')]
        self.assertEqual(decide_stalled([turn()], calls, T0 + 14 * 60, {}), [])

    def test_open_call_matched_by_task_id(self):
        calls = [dict(task_id='task1', tool='run_shell')]
        self.assertEqual(decide_stalled([turn()], calls, T0 + 14 * 60, {}), [])

    def test_open_call_on_other_thread_does_not_block(self):
        calls = [dict(thread_id='other', task_id='other')]
        self.assertEqual(kinds(decide_stalled([turn()], calls, T0 + 300, {})), ['stop_turn', 'requeue'])

    def test_new_tool_call_resets_timer(self):
        self.assertEqual(decide_stalled([turn(last_tool_at=T0 + 100)], [], T0 + 200, {}), [])

    def test_new_output_resets_timer(self):
        self.assertEqual(decide_stalled([turn(last_output_at=T0 + 100)], [], T0 + 200, {}), [])

    def test_new_event_resets_timer(self):
        self.assertEqual(decide_stalled([turn(last_event_at=T0 + 100)], [], T0 + 200, {}), [])

    def test_last_progress_is_newest(self):
        self.assertEqual(last_progress(turn(last_tool_at=T0 + 5, last_output_at=T0 + 9, last_event_at=T0 + 7)), T0 + 9)


class StallSequenceTests(unittest.TestCase):
    def test_first_stall_stop_then_requeue(self):
        actions = decide_stalled([turn()], [], T0 + 130, {})
        stop, requeue = actions
        self.assertEqual((stop['kind'], stop['thread_id'], stop['task_id'], stop['reason']),
                         ('stop_turn', 'th1', 'task1', 'stalled'))
        self.assertEqual((requeue['kind'], requeue['task_id'], requeue['avoid_worker_id']),
                         ('requeue', 'task1', 'w1'))
        self.assertNotIn('avoid_worker_ids', requeue)

    def test_second_stall_avoids_both_workers(self):
        first = decide_stalled([turn()], [], T0 + 130, {})
        state = next_state(None, first)
        second = decide_stalled([turn(tid='turn2', thread='th2', worker='w2', sent_at=T0 + 200)], [], T0 + 400, {}, state)
        self.assertEqual(kinds(second), ['stop_turn', 'requeue'])
        self.assertEqual(second[1]['avoid_worker_id'], 'w2')
        self.assertEqual(second[1]['avoid_worker_ids'], ['w1', 'w2'])

    def test_third_stall_fails_with_stalled(self):
        state = None
        for index, worker in enumerate(['w1', 'w2', 'w3']):
            start = T0 + index * 1000
            actions = decide_stalled([turn(tid='t%d' % index, thread='th%d' % index, worker=worker, sent_at=start)],
                                     [], start + 200, {}, state)
            state = next_state(state, actions)
        self.assertEqual(kinds(actions), ['stop_turn', 'fail'])
        self.assertEqual(actions[1]['reason'], 'stalled')
        self.assertEqual(actions[1]['task_id'], 'task1')
        self.assertEqual(state['stalls']['task1']['count'], 3)

    def test_same_turn_id_no_double_action(self):
        actions = decide_stalled([turn()], [], T0 + 130, {})
        state = next_state(None, actions)
        self.assertEqual(decide_stalled([turn()], [], T0 + 5000, {}, state), [])

    def test_duplicate_turn_in_one_call_handled_once(self):
        actions = decide_stalled([turn(), turn()], [], T0 + 130, {})
        self.assertEqual(kinds(actions), ['stop_turn', 'requeue'])

    def test_turn_id_fallback(self):
        item = turn()
        del item['turn_id']
        self.assertEqual(turn_id(item), 'th1:task1')


class SettingsTests(unittest.TestCase):
    def test_stall_watch_false_disables(self):
        self.assertEqual(decide_stalled([turn()], [], T0 + 5000, {'stall_watch': False}), [])
        self.assertTrue(decide_stalled([turn()], [], T0 + 5000, {'stall_watch': None}))

    def test_custom_stall_after_s(self):
        self.assertEqual(decide_stalled([turn()], [], T0 + 50, {'stall_after_s': 60}), [])
        self.assertTrue(decide_stalled([turn()], [], T0 + 60, {'stall_after_s': 60}))

    def test_custom_max_stalls_one_fails_at_once(self):
        actions = decide_stalled([turn()], [], T0 + 130, {'max_stalls': 1})
        self.assertEqual(kinds(actions), ['stop_turn', 'fail'])


class EmptyReplyAndTextTests(unittest.TestCase):
    def test_on_empty_reply_stop_before_retry(self):
        actions = on_empty_reply(dict(id='task1', worker_id='w1'), 'th1')
        self.assertEqual(kinds(actions), ['stop_turn', 'retry'])
        self.assertEqual(actions[0]['thread_id'], 'th1')
        self.assertEqual(actions[0]['task_id'], 'task1')

    def test_live_case_stream_end_empty_reply_14_min(self):
        live = turn(sent_at=T0, last_tool_at=T0 + 30, last_event_at=T0 + 60)
        actions = decide_stalled([live], [], T0 + 60 + 14 * 60, {})
        self.assertEqual(actions[0]['kind'], 'stop_turn')
        self.assertEqual(actions[0]['thread_id'], 'th1')
        self.assertEqual(kinds(on_empty_reply(dict(id='task1', worker_id='w1'), 'th1'))[0], 'stop_turn')

    def test_event_text_lines(self):
        actions = decide_stalled([turn()], [], T0 + 120, {})
        self.assertEqual(event_text(actions[0]), 'Coworker 5 stalled for 2 min; stopped and re-queued')
        self.assertEqual(event_text(actions[1]), 'Task task1 re-queued on another coworker')
        fail = decide_stalled([turn()], [], T0 + 120, {'max_stalls': 1})[1]
        self.assertEqual(event_text(fail), 'Task task1 failed: stalled 1 times')
        stop, retry = on_empty_reply(dict(id='task1'), 'th1')
        self.assertEqual(event_text(stop), 'Stopped the old turn of task task1 after an empty reply')
        self.assertEqual(event_text(retry), 'Task task1 retried after an empty reply')
        for line in map(event_text, actions):
            self.assertNotIn('\n', line)

    def test_inputs_stay_unchanged(self):
        turns = [turn()]
        calls = [dict(thread_id='x')]
        settings = {'stall_after_s': 120}
        state = {'handled_turn_ids': ['old'], 'stalls': {'task1': {'count': 1, 'worker_ids': ['w0']}}}
        before = copy.deepcopy((turns, calls, settings, state))
        actions = decide_stalled(turns, calls, T0 + 500, settings, state)
        actions_before = copy.deepcopy(actions)
        new_state = next_state(state, actions)
        self.assertEqual((turns, calls, settings, state), before)
        self.assertEqual(actions, actions_before)
        self.assertEqual(new_state['stalls']['task1']['worker_ids'], ['w0', 'w1'])
        self.assertEqual(new_state['handled_turn_ids'], ['old', 'turn1'])


if __name__ == '__main__':
    unittest.main()
