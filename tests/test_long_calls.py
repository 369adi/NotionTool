"""long_calls: answer before Notion's MCP request timeout (-32001) and hand out a job_id."""
import json
import threading
import time
import unittest

from notiontool import long_calls
from notiontool.bridge import Bridge

LIMIT = 0.2


class FakeStore:
    def __init__(self, settings=None):
        self._settings = dict(settings or {})
        self.events = []

    def settings(self):
        return dict(self._settings)

    def event(self, *args, **kwargs):
        self.events.append((args, kwargs))


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.jobs = long_calls.Jobs()
        self.release = threading.Event()
        self.calls = 0

    def tearDown(self):
        self.release.set()

    def slow(self, value='done'):
        def fn():
            self.calls += 1
            self.release.wait(5)
            return {'value': value}
        return fn

    def test_fast_call_returns_directly(self):
        result = self.jobs.run('operator', 'nt_shell', {'command': 'x'}, lambda: {'exit_code': 0}, LIMIT)
        self.assertEqual(result, {'exit_code': 0})
        self.assertEqual(len(self.jobs), 0)  # nobody holds a job_id, so nothing is kept

    def test_slow_call_returns_still_running_with_job_id(self):
        started = time.monotonic()
        answer = self.jobs.run('operator', 'nt_shell', {'command': 'sleep'}, self.slow(), LIMIT)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(answer['still_running'])
        self.assertTrue(answer['job_id'].startswith('job-'))
        self.assertEqual(answer['tool'], 'nt_shell')
        self.assertGreaterEqual(answer['elapsed_s'], 0)
        self.assertEqual(answer['hint'], long_calls.HINT)

    def test_job_result_returns_final_result(self):
        answer = self.jobs.run('operator', 'nt_shell', {'command': 'sleep'}, self.slow('final'), LIMIT)
        again = self.jobs.result('operator', answer['job_id'], 0.05)
        self.assertTrue(again['still_running'])
        self.assertEqual(again['job_id'], answer['job_id'])
        self.release.set()
        self.assertEqual(self.jobs.result('operator', answer['job_id'], 5), {'value': 'final'})
        # Kept for later reads (30 min), the same result again.
        self.assertEqual(self.jobs.result('operator', answer['job_id'], 0), {'value': 'final'})

    def test_duplicate_call_reuses_running_job(self):
        first = self.jobs.run('task:a', 'nt_shell', {'command': 'sleep', 'timeout': 90}, self.slow(), LIMIT)
        second = self.jobs.run('task:a', 'nt_shell', {'timeout': 90, 'command': 'sleep'}, self.slow(), LIMIT)
        self.assertEqual(first['job_id'], second['job_id'])
        self.assertEqual(self.calls, 1)
        other = self.jobs.run('task:b', 'nt_shell', {'command': 'sleep', 'timeout': 90}, self.slow(), LIMIT)
        self.assertNotEqual(other['job_id'], first['job_id'])
        self.assertEqual(self.calls, 2)

    def test_other_caller_cannot_read_job(self):
        answer = self.jobs.run('task:a', 'nt_shell', {'command': 'sleep'}, self.slow(), LIMIT)
        with self.assertRaisesRegex(ValueError, 'another caller'):
            self.jobs.result('task:b', answer['job_id'], 0)
        with self.assertRaisesRegex(ValueError, 'another caller'):
            self.jobs.result('operator', answer['job_id'], 0)
        with self.assertRaisesRegex(ValueError, 'Unknown or expired'):
            self.jobs.result('task:a', 'job-missing', 0)

    def test_exception_in_job_is_returned_as_error(self):
        def broken():
            self.release.wait(5)
            raise ValueError('Command timed out; its process tree was stopped')
        answer = self.jobs.run('operator', 'nt_shell', {'command': 'x'}, broken, LIMIT)
        self.assertTrue(answer['still_running'])
        self.release.set()
        with self.assertRaisesRegex(ValueError, 'Command timed out'):
            self.jobs.result('operator', answer['job_id'], 5)
        # A fast failing call raises directly, like before.
        with self.assertRaisesRegex(KeyError, 'boom'):
            self.jobs.run('operator', 'nt_api', {}, lambda: (_ for _ in ()).throw(KeyError('boom')), LIMIT)

    def test_old_jobs_are_cleaned_up(self):
        now = [1000.0]
        jobs = long_calls.Jobs(clock=lambda: now[0])
        gate = threading.Event()
        def slow():
            gate.wait(5)
            return 'ok'
        answer = jobs.run('operator', 'nt_shell', {'command': 'old'}, slow, LIMIT)
        gate.set()
        self.assertEqual(jobs.result('operator', answer['job_id'], 5), 'ok')
        self.assertIn(answer['job_id'], jobs)
        now[0] += long_calls.KEEP_S + 1
        with self.assertRaisesRegex(ValueError, 'Unknown or expired'):
            jobs.result('operator', answer['job_id'], 0)
        self.assertNotIn(answer['job_id'], jobs)

    def test_at_most_max_jobs_finished_jobs_are_kept(self):
        jobs = long_calls.Jobs()
        gates = []
        ids = []
        for i in range(long_calls.MAX_JOBS + 5):
            gate = threading.Event()
            gates.append(gate)
            ids.append(jobs.run('operator', 'nt_shell', {'n': i}, gate.wait, 0)['job_id'])
        for gate in gates:
            gate.set()
        deadline = time.monotonic() + 5
        while jobs.running() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(jobs.running(), [])
        jobs.run('operator', 'nt_shell', {'n': 'last'}, lambda: 'x', LIMIT)
        self.assertLessEqual(len(jobs), long_calls.MAX_JOBS)
        self.assertNotIn(ids[0], jobs)
        self.assertIn(ids[-1], jobs)

    def test_running_jobs_are_never_dropped(self):
        jobs = long_calls.Jobs()
        for i in range(long_calls.MAX_JOBS + 3):
            jobs.run('operator', 'nt_shell', {'n': i}, self.slow(), 0)
        self.assertEqual(len(jobs.running()), long_calls.MAX_JOBS + 3)

    def test_sync_limit_setting_is_clamped(self):
        self.assertEqual(long_calls.sync_limit({}), long_calls.SYNC_LIMIT)
        self.assertEqual(long_calls.SYNC_LIMIT, 45.0)
        self.assertEqual(long_calls.sync_limit({'mcp_sync_limit_s': 3}), 10.0)
        self.assertEqual(long_calls.sync_limit({'mcp_sync_limit_s': 90}), 55.0)
        self.assertEqual(long_calls.sync_limit({'mcp_sync_limit_s': '30'}), 30.0)
        self.assertEqual(long_calls.sync_limit({'mcp_sync_limit_s': 'x'}), long_calls.SYNC_LIMIT)
        self.assertEqual(long_calls.wait_seconds(None), long_calls.DEFAULT_WAIT)  # DEFAULT_WAIT=50.0
        self.assertEqual(long_calls.wait_seconds(500), long_calls.MAX_WAIT)  # clamped to MAX_WAIT=50.0
        self.assertEqual(long_calls.wait_seconds(-1), 0.0)


class BridgeWrapTests(unittest.TestCase):
    """The tools/call branch of Bridge.rpc with a fake slow operator tool."""

    def setUp(self):
        self.saved = long_calls.SYNC_LIMIT
        long_calls.SYNC_LIMIT = LIMIT
        self.bridge = Bridge(FakeStore(), engine=None)
        self.release = threading.Event()
        self.threads = []
        self.operator = object()

        def operator_call(app, name, args):
            self.threads.append(threading.current_thread().name)
            if name == 'nt_shell' and args.get('command') == 'slow':
                self.release.wait(5)
                return {'exit_code': 0, 'stdout': 'real result'}
            if name == 'nt_shell' and args.get('command') == 'image':
                self.release.wait(5)
                return {'stdout': 'shot', '_image': {'data': 'QUJD', 'mimeType': 'image/png'}}
            if name == 'nt_shell' and args.get('command') == 'fail':
                self.release.wait(5)
                raise ValueError('Command timed out; its process tree was stopped')
            return {'exit_code': 0, 'stdout': 'fast ' + name}
        self.bridge.operator_call = operator_call

    def tearDown(self):
        self.release.set()
        long_calls.SYNC_LIMIT = self.saved

    def call(self, name, args, operator=None, ident=1):
        reply = self.bridge.rpc({'id': ident, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}, operator=operator)
        result = reply['result']
        text = result['content'][0]['text']
        return result, (text if result.get('isError') else json.loads(text))

    def test_fast_call_is_unchanged(self):
        result, value = self.call('nt_search_code', {'pattern': 'x'}, self.operator)
        self.assertFalse(result.get('isError'))
        self.assertEqual(value, {'exit_code': 0, 'stdout': 'fast nt_search_code'})

    def test_slow_call_returns_job_and_job_result_returns_real_result(self):
        _, first = self.call('nt_shell', {'command': 'slow'}, self.operator)
        self.assertTrue(first['still_running'])
        _, again = self.call('nt_shell', {'command': 'slow'}, self.operator, ident=2)
        self.assertEqual(again['job_id'], first['job_id'])  # not started a second time
        self.assertEqual(self.threads.count(self.threads[0]), 1)
        self.release.set()
        _, final = self.call('nt_job_result', {'job_id': first['job_id'], 'wait_s': 5}, self.operator, ident=3)
        self.assertEqual(final, {'exit_code': 0, 'stdout': 'real result'})

    def test_job_error_is_tool_error(self):
        _, first = self.call('nt_shell', {'command': 'fail'}, self.operator)
        self.assertTrue(first['still_running'])
        self.release.set()
        result, text = self.call('nt_job_result', {'job_id': first['job_id'], 'wait_s': 5}, self.operator, ident=2)
        self.assertTrue(result['isError'])
        self.assertIn('Command timed out', text)

    def test_stored_job_result_keeps_its_image(self):
        _, first = self.call('nt_shell', {'command': 'image'}, self.operator)
        self.assertTrue(first['still_running'])
        self.release.set()
        for ident in (2, 3):
            reply = self.bridge.rpc({'id': ident, 'method': 'tools/call', 'params': {'name': 'nt_job_result',
                                     'arguments': {'job_id': first['job_id'], 'wait_s': 5}}}, operator=self.operator)
            content = reply['result']['content']
            self.assertEqual(json.loads(content[0]['text']), {'stdout': 'shot'})
            self.assertEqual(content[1], {'type': 'image', 'data': 'QUJD', 'mimeType': 'image/png'})

    def test_coworker_token_separates_callers(self):
        _, first = self.call('nt_shell', {'command': 'slow'}, self.operator)
        result, text = self.call('mcp_call', {'tool_name': 'nt_job_result', 'assignment_token': 'tok-b',
                                              'arguments': {'job_id': first['job_id'], 'wait_s': 0}}, None, ident=2)
        self.assertTrue(result['isError'])
        self.assertIn('another caller', text)
        result, text = self.call('nt_job_result', {'job_id': first['job_id'], 'wait_s': 0}, None, ident=3)
        self.assertTrue(result['isError'])
        self.assertIn('another caller', text)

    def test_job_result_is_never_wrapped(self):
        _, first = self.call('nt_shell', {'command': 'slow'}, self.operator)
        before = len(self.bridge.long_jobs())
        _, again = self.call('nt_job_result', {'job_id': first['job_id'], 'wait_s': 0}, self.operator, ident=2)
        self.assertTrue(again['still_running'])
        self.assertEqual(again['job_id'], first['job_id'])
        self.assertEqual(len(self.bridge.long_jobs()), before)

    def test_writes_stay_synchronous(self):
        main = threading.current_thread().name
        self.call('nt_write_file', {'path': 'x.txt', 'content': 'y'}, self.operator)
        self.call('nt_edit_file', {'path': 'x.txt', 'old_text': 'y', 'new_text': 'z'}, self.operator, ident=2)
        self.assertEqual(self.threads, [main, main])
        self.call('nt_shell', {'command': 'quick'}, self.operator, ident=3)
        self.assertNotEqual(self.threads[-1], main)

    def test_tools_list_has_job_result_for_main_and_coworkers(self):
        for operator in (self.operator, None):
            reply = self.bridge.rpc({'id': 1, 'method': 'tools/list', 'params': {}}, operator=operator)
            names = [t['name'] for t in reply['result']['tools']]
            self.assertEqual(names.count('nt_job_result'), 1)
            spec = next(t for t in reply['result']['tools'] if t['name'] == 'nt_job_result')
            self.assertTrue(spec['annotations']['readOnlyHint'])
            mcp_call = next(t for t in reply['result']['tools'] if t['name'] == 'mcp_call')
            self.assertIn('nt_job_result', mcp_call['description'])

    def test_shell_description_mentions_job_result(self):
        from notiontool.devtools import TOOLS
        shell = next(t for t in TOOLS if t['name'] == 'nt_shell')
        self.assertIn('45 s', shell['description'])
        self.assertIn('nt_job_result', shell['description'])


if __name__ == '__main__':
    unittest.main()
