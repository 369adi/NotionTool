"""Every recorded tool start gets exactly one tool_finished, also on errors and timeouts.

Without it, a hung or failed tool call is indistinguishable from one still running, which hid the
cause of turns that ended without a confirmed completion.
"""
import subprocess
import unittest
from unittest import mock

from notiontool import bridge as bridge_module
from test_bridge_reviewer_tests import TOKEN, ReviewerUnittestShellTests


class ToolFinishedTests(ReviewerUnittestShellTests.__base__):
    setUp = ReviewerUnittestShellTests.setUp
    tearDown = ReviewerUnittestShellTests.tearDown
    make = ReviewerUnittestShellTests.make
    shell = ReviewerUnittestShellTests.shell

    def kinds(self):
        return [e[0] for e in self.store.events]

    def finished(self):
        return [e[2] for e in self.store.events if e[0] == 'tool_finished']

    def test_success_records_one_finish_with_exit_code(self):
        self.shell('python -m unittest tests.test_a')
        self.assertEqual(self.kinds(), ['tool_started', 'tool_finished'])
        done = self.finished()[0]
        self.assertTrue(done['ok'])
        self.assertEqual(done['exit_code'], 0)
        self.assertEqual(done['task_id'], 't1')
        self.assertEqual(done['tool'], 'run_shell')
        self.assertGreaterEqual(done['seconds'], 0)

    def test_timeout_records_failed_finish(self):
        process = self.popen.return_value
        process.communicate.side_effect = [subprocess.TimeoutExpired('x', 1), (b'', None)]
        with mock.patch.object(bridge_module.subprocess, 'run'):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                self.shell('python -m unittest tests.test_a', timeout=1)
        self.assertEqual(self.kinds(), ['tool_started', 'tool_finished'])
        done = self.finished()[0]
        self.assertFalse(done['ok'])
        self.assertIn('timed out', done['error'])

    def test_unexpected_error_records_failed_finish(self):
        self.popen.return_value.communicate.side_effect = OSError('pipe broke')
        with self.assertRaises(OSError):
            self.shell('python -m unittest tests.test_a')
        self.assertEqual(self.kinds(), ['tool_started', 'tool_finished'])
        self.assertIn('pipe broke', self.finished()[0]['error'])

    def test_refusal_before_start_records_nothing(self):
        with self.assertRaisesRegex(ValueError, 'read-only PC access'):
            self.shell('whoami')
        self.assertEqual(self.store.events, [])

    def test_calls_do_not_leak_start_into_next_call(self):
        self.shell('python -m unittest tests.test_a')
        with self.assertRaisesRegex(ValueError, 'read-only PC access'):
            self.shell('whoami')
        self.assertEqual(self.kinds(), ['tool_started', 'tool_finished'])


if __name__ == '__main__':
    unittest.main()
