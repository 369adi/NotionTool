import unittest

from notiontool.turn_watch import STALL_SECONDS, interrupted_turn


def transcript(status, entities):
    return {'session': {'status': status},
            'patches': [{'op': 'put', 'entity': e} for e in entities]}


USER = {'id': 'u1', 'kind': 'user_message', 'content': 'mach weiter', 'sequence': 1, 'created_at': 1000}
TOOL = {'id': 't1', 'kind': 'tool_call', 'tool_use_id': 'x', 'sequence': 2, 'created_at': 1010}


class StallTest(unittest.TestCase):
    def test_running_turn_with_recent_activity_is_left_alone(self):
        self.assertIsNone(interrupted_turn(transcript('running', [USER, TOOL]), now=1010 + 60))

    def test_running_turn_without_activity_for_stall_time_is_resumed(self):
        found = interrupted_turn(transcript('running', [USER, TOOL]), now=1010 + STALL_SECONDS + 1)
        self.assertIsNotNone(found)
        self.assertTrue(found['stalled'])
        self.assertFalse(found['pending'])
        self.assertEqual(found['key'], 't1')

    def test_without_clock_a_running_turn_is_never_resumed(self):
        self.assertIsNone(interrupted_turn(transcript('running', [USER, TOOL])))

    def test_idle_turn_keeps_old_behaviour(self):
        found = interrupted_turn(transcript('idle', [USER, TOOL]), now=1020)
        self.assertIsNotNone(found)
        self.assertFalse(found['stalled'])
        self.assertTrue(found['pending'])


if __name__ == '__main__':
    unittest.main()
