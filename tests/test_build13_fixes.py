import tempfile
import unittest

from notiontool import no_console
from notiontool.store import Store
from notiontool import token_budget
from notiontool.turn_watch import charge_main_usage, interrupted_turn


class Build13(unittest.TestCase):
    def test_none_sequence_does_not_break_watch(self):
        transcript = {'session': {'status': 'idle'}, 'patches': [
            {'op': 'put', 'entity': {'id': 'a', 'kind': 'user_message', 'sequence': None}},
            {'op': 'put', 'entity': {'id': 'b', 'kind': 'assistant_message', 'sequence': 3}}]}
        self.assertIsNone(interrupted_turn(transcript, now=0))
        self.assertIsNone(interrupted_turn({'session': {'status': 'idle'}, 'patches': None}, now=0))

    def test_main_usage_is_charged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            worker = dict(id='w1', label='Main', purpose='main')
            got = charge_main_usage(store, worker, dict(estimated_tokens=1000, turns=2),
                                    dict(estimated_tokens=1300, turns=3))
            self.assertEqual(got, 1300)
            report = token_budget.public(store)
            self.assertEqual(report['used'], 1300)
            self.assertEqual(report['main_tokens'], 1300)
            self.assertEqual(report['turns'], 1)
            store.close()

    def test_no_console_flags(self):
        self.assertEqual(no_console.flags_for(0), no_console.CREATE_NO_WINDOW)
        self.assertEqual(no_console.flags_for(no_console.DETACHED_PROCESS), no_console.DETACHED_PROCESS)
        self.assertEqual(no_console.flags_for(no_console.CREATE_NEW_CONSOLE), no_console.CREATE_NEW_CONSOLE)


if __name__ == '__main__':
    unittest.main()
