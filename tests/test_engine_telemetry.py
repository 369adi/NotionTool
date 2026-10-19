"""Engine telemetry, assigned_files and the operator awaiting_main summary (batch 14)."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from notiontool import engine as engine_module
from notiontool.engine import Engine, TELEMETRY_SECONDS
from notiontool.store import Store


class IdleAdapter:
    def run(self, worker, task, prompt, cancel, update):
        raise AssertionError('No task is sent in these tests')

    def recover(self, worker, task, cancel):
        raise AssertionError('No reconciliation in these tests')


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class EngineTelemetryTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.store.put('chats', dict(id='c', title='Main', main_worker='m', folder_id='f', project_path=self.temp.name))
        self.store.put('runs', dict(id='r1', chat_id='c', state='executing', operator=True, project_path=self.temp.name,
                                    error='', capacity=2, worker_budget=2, fixes=0, main_worker='m',
                                    created=time.time(), updated=time.time()))
        self.engine = Engine(self.store, IdleAdapter())

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def running_task(self, ident='t1', lease='L1'):
        self.store.put('tasks', dict(id=ident, run_id='r1', role='executor', title='Work', prompt='p', state='running',
                                     result='', error='', deps=[], files=[], attempts=1, worker_id='', lease=lease))

    def stream(self, clock, *updates, ident='t1', lease='L1'):
        writes = []
        put = self.store.put

        def counting(kind, record, *args, **kwargs):
            if kind == 'tasks':
                writes.append(dict(record))
            return put(kind, record, *args, **kwargs)
        with mock.patch.object(engine_module.time, 'time', clock), mock.patch.object(self.store, 'put', counting):
            for at, text in updates:
                clock.now = at
                self.engine._partial(ident, lease, text)
        return writes

    def test_first_text_is_stored_with_last_output_at(self):
        self.running_task()
        writes = self.stream(Clock(1000.0), (1000.0, 'Hello'))
        self.assertEqual(len(writes), 1)
        task = self.store.get('tasks', 't1')
        self.assertEqual(task['partial'], 'Hello')
        self.assertEqual(task['last_output_at'], 1000.0)
        # The update payload is only answer text, so tool steps are not recorded.
        self.assertNotIn('last_tool_at', task)

    def test_at_most_one_write_per_five_seconds(self):
        self.assertEqual(TELEMETRY_SECONDS, 5)
        self.running_task()
        updates = [(1000.0 + i * 0.6, 'Hello' + ' more' * i) for i in range(8)]  # 1000.0 .. 1004.2
        writes = self.stream(Clock(1000.0), *updates)
        self.assertEqual(len(writes), 1)
        writes = self.stream(Clock(1005.0), (1005.0, 'Hello and the newest text'))
        self.assertEqual(len(writes), 1)
        task = self.store.get('tasks', 't1')
        self.assertEqual(task['partial'], 'Hello and the newest text')
        self.assertEqual(task['last_output_at'], 1005.0)

    def test_last_output_at_is_the_time_of_the_newest_text_not_of_the_write(self):
        self.running_task()
        self.stream(Clock(1000.0), (1000.0, 'A'), (1002.0, 'AB'), (1004.0, 'AB'), (1006.0, 'AB'))
        task = self.store.get('tasks', 't1')
        self.assertEqual(task['partial'], 'AB')
        self.assertEqual(task['progress_at'], 1006.0)
        self.assertEqual(task['last_output_at'], 1002.0)

    def test_empty_text_sets_no_last_output_at(self):
        self.running_task()
        self.stream(Clock(1000.0), (1000.0, '   '))
        self.assertNotIn('last_output_at', self.store.get('tasks', 't1'))

    def test_wrong_lease_or_finished_task_is_not_written(self):
        self.running_task()
        self.assertEqual(self.stream(Clock(1000.0), (1000.0, 'x'), lease='old'), [])
        task = self.store.get('tasks', 't1')
        task['state'] = 'done'
        self.store.put('tasks', task)
        self.assertEqual(self.stream(Clock(1010.0), (1010.0, 'late')), [])
        self.assertNotIn('last_output_at', self.store.get('tasks', 't1'))

    def test_add_task_sets_assigned_files_only_for_a_files_list(self):
        run = self.store.get('runs', 'r1')
        task = self.engine.add_task(run, 'executor', 'p', 'Docs', files=['docs/A.md', 'b.py'])
        self.assertEqual(task['assigned_files'], ['docs/A.md', 'b.py'])
        self.assertEqual(self.store.get('tasks', task['id'])['assigned_files'], ['docs/A.md', 'b.py'])
        self.assertIsNot(task['assigned_files'], task['files'])
        plain = self.engine.add_task(run, 'executor', 'p', 'Router')
        self.assertNotIn('assigned_files', plain)
        self.assertNotIn('assigned_files', self.store.get('tasks', plain['id']))

    def finished(self, ident, result):
        self.store.put('tasks', dict(id=ident, run_id='r1', role='executor', title='Task ' + ident, prompt='p',
                                     state='done', result=result, error='', deps=[], files=[], attempts=1,
                                     worker_id='', lease=''))

    def advance(self):
        run = self.store.get('runs', 'r1')
        with self.engine.lock:
            self.engine._advance(run)
        return run

    def test_awaiting_main_summary_names_the_waiting_tasks(self):
        self.finished('t1', 'ok\nRESULT: DONE')
        self.finished('t2', 'no result line')
        run = self.advance()
        self.assertEqual(run['state'], 'awaiting_main')
        self.assertNotIn('operator reads the reports', run['summary'])
        self.assertIn('2 delegated task(s) finished; 1 accepted with RESULT: DONE', run['summary'])
        self.assertTrue(run['summary'].endswith('; waiting for the Main: t2'), run['summary'])

    def test_passing_run_keeps_todays_summary(self):
        self.finished('t1', 'ok\nRESULT: DONE')
        run = self.advance()
        self.assertEqual(run['state'], 'done')
        self.assertEqual(run['summary'], '1 delegated task(s) finished; 1 accepted with RESULT: DONE; the operator reads the reports')


if __name__ == '__main__':
    unittest.main()
