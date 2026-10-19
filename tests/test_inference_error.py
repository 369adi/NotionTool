"""A Notion failure line instead of an answer is never accepted as done (live bug td7736426921)."""
import json
import tempfile
import time
import unittest
from concurrent.futures import Future
from pathlib import Path

from notiontool import plain_protocol
from notiontool.engine import Engine
from notiontool.store import Store

ERROR_TEXT = 'Agent inference failed: vendorServerError (TypeError)'


class PlainAdapter:
    response_mode = 'plain'

    def run(self, worker, task, prompt, cancel, update):
        raise AssertionError('The tests drive _complete directly; nothing is sent')

    def recover(self, worker, task, cancel):
        raise AssertionError('No reconciliation check in these tests')


class DetectionTests(unittest.TestCase):
    def test_whole_answer_and_last_line_match(self):
        self.assertEqual(plain_protocol.inference_error(ERROR_TEXT), ERROR_TEXT)
        self.assertEqual(plain_protocol.inference_error('Reading files...\n\n' + ERROR_TEXT + '\n'), ERROR_TEXT)
        self.assertTrue(plain_protocol.inference_error('Something went wrong (code 500)'))

    def test_phrase_in_the_middle_is_not_an_error(self):
        text = 'DONE\nNotion said "Agent inference failed" once, the edit is complete.\nAll checks passed.'
        self.assertEqual(plain_protocol.inference_error(text), '')

    def test_normalize_raises_a_provider_reply_error(self):
        with self.assertRaises(plain_protocol.InferenceErrorReply) as caught:
            plain_protocol.normalize(dict(role='executor', files=[]), ERROR_TEXT)
        self.assertIsInstance(caught.exception, plain_protocol.ProviderReplyError)
        self.assertEqual(caught.exception.line, ERROR_TEXT)


class EngineTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        self.store.put('chats', dict(id='c', title='Main', main_worker='m', folder_id='f', project_path=self.temp.name))
        self.store.put('runs', dict(id='r1', chat_id='c', state='executing', project_path=self.temp.name, error='',
                                    capacity=2, worker_budget=2, fixes=0, main_worker='m',
                                    created=time.time(), updated=time.time()))
        self.engine = Engine(self.store, PlainAdapter())

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def start_attempt(self, lease, attempts=1, **extra):
        started = time.time() - 5
        self.store.put('workers', dict(id='w1', account_id='a', thread_id='w1', purpose='worker', enabled=True,
                                       state='busy', lease=lease, label='w1', last_used=0))
        task = self.store.get('tasks', 't1') or dict(id='t1', run_id='r1', role='executor', title='Read docs', prompt='p',
                                                     result='', deps=[], files=[], error='')
        task.update(state='running', lease=lease, worker_id='w1', attempts=attempts, started=started,
                    attempt_log=[dict(started=started)])
        task.update(extra)
        self.store.put('tasks', task)
        return dict(task_id='t1', lease=lease, worker_id='w1')

    def complete(self, info, text):
        future = Future()
        future.set_result(text)
        with self.engine.lock:
            self.engine._complete(future, info)
        return self.store.get('tasks', 't1')

    def tool(self, name):
        self.store.event('tool_started', 'r1', task_id='t1', tool=name, message=name)

    def kinds(self):
        return [e['kind'] for e in self.store.events(limit=500)]

    def test_read_only_attempt_is_retried_once_then_fails(self):
        info = self.start_attempt('L1')
        self.tool('read_file')
        self.tool('list_files')
        task = self.complete(info, ERROR_TEXT)
        self.assertEqual(task['state'], 'retry')
        self.assertTrue(task['inference_retry'])
        self.assertTrue(task['error'].startswith('Notion inference error: ' + ERROR_TEXT))
        self.assertFalse(task.get('side_effect_hold'))
        self.assertEqual(self.store.get('workers', 'w1')['lease'], '')
        info = self.start_attempt('L2', attempts=2)
        task = self.complete(info, ERROR_TEXT)
        self.assertEqual(task['state'], 'blocked')
        self.assertEqual(task['error'], 'Notion inference error: ' + ERROR_TEXT)
        self.assertNotEqual(task['state'], 'done')
        self.assertEqual(self.kinds().count('notion_inference_error'), 2)
        event = next(e for e in self.store.events(limit=500) if e['kind'] == 'notion_inference_error')
        self.assertEqual((event['task_id'], event['message']), ('t1', ERROR_TEXT))

    def test_attempt_with_writes_is_held_not_resent(self):
        info = self.start_attempt('L1')
        self.tool('read_file')
        self.tool('write_file')
        task = self.complete(info, 'Working on it.\n' + ERROR_TEXT)
        self.assertEqual(task['state'], 'blocked')
        self.assertTrue(task.get('side_effect_hold'))
        self.assertIn('reconciled, not resent', task['error'])
        self.assertIn('retry_refused_side_effects', self.kinds())
        self.assertIn('notion_inference_error', self.kinds())

    def test_newly_claimed_file_counts_as_a_write(self):
        info = self.start_attempt('L1', assigned_files=[], files=['docs/a.md'])
        task = self.complete(info, ERROR_TEXT)
        self.assertEqual(task['state'], 'blocked')
        self.assertTrue(task.get('side_effect_hold'))

    def test_normal_answer_with_the_phrase_in_the_middle_stays_done(self):
        info = self.start_attempt('L1')
        text = 'DONE\nNotion said "Agent inference failed" once, then the review passed.\nAll checks passed.'
        task = self.complete(info, text)
        self.assertEqual(task['state'], 'done')
        self.assertEqual(json.loads(task['result'])['status'], 'done')
        self.assertNotIn('notion_inference_error', self.kinds())


if __name__ == '__main__':
    unittest.main()
