"""Honest run outcomes: task outcome field, run counts (outcomes, fixes, reviews) and the summary text."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from notiontool.engine import Engine
from notiontool.store import Store

SKIP_MARK = 'read-only task: no changed files to review'


class IdleAdapter:
    def run(self, worker, task, prompt, cancel, update):
        raise AssertionError('No task is sent in these tests')

    def recover(self, worker, task, cancel):
        raise AssertionError('No reconciliation in these tests')


class OutcomeHonestTests(unittest.TestCase):
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
        self.temp.cleanup()

    def finished(self, ident, result, role='executor', **extra):
        self.store.put('tasks', dict(id=ident, run_id='r1', role=role, title='Task ' + ident, prompt='p',
                                     state='done', result=result, error='', deps=[], files=[], attempts=1,
                                     worker_id='', lease='', **extra))

    def advance(self, check=None):
        run = self.store.get('runs', 'r1')
        with mock.patch.object(Engine, '_machine_check', lambda self_, run_, task: (check or {}).get(task['id'], '')):
            with self.engine.lock:
                self.engine._advance(run)
        return run

    def outcome(self, ident):
        return self.store.get('tasks', ident).get('outcome')

    def test_not_done_is_counted_and_state_stays_done(self):
        self.finished('t1', 'ok\nRESULT: DONE')
        self.finished('t2', 'could not finish\nRESULT: NOT DONE: tests missing')
        run = self.advance()
        self.assertEqual(run['outcomes'], dict(accepted=1, not_done=1, check_failed=0))
        self.assertEqual(self.outcome('t1'), 'accepted')
        self.assertEqual(self.outcome('t2'), 'not_done')
        self.assertEqual(self.store.get('tasks', 't2')['state'], 'done')
        self.assertIn('1 not done', run['summary'])
        self.assertIn('1 accepted', run['summary'])

    def test_missing_result_line_counts_as_not_done(self):
        self.finished('t1', 'I changed the file but wrote no result line')
        run = self.advance()
        self.assertEqual(self.outcome('t1'), 'not_done')
        self.assertEqual(run['outcomes'], dict(accepted=0, not_done=1, check_failed=0))

    def test_check_failed_is_counted(self):
        self.finished('t1', 'ok\nRESULT: DONE')
        self.finished('t2', 'ok\nRESULT: DONE')
        run = self.advance(check={'t2': 'Machine check failed: file missing'})
        self.assertEqual(self.outcome('t1'), 'accepted')
        self.assertEqual(self.outcome('t2'), 'check_failed')
        self.assertEqual(run['outcomes'], dict(accepted=1, not_done=0, check_failed=1))
        self.assertIn('1 check failed', run['summary'])
        self.assertEqual(list(run['machine_check_failed']), ['t2'])

    def test_skipped_review_is_counted_as_skipped_never_approved(self):
        self.finished('t1', 'ok\nRESULT: DONE', review_state='approved', review_skipped=SKIP_MARK)
        self.finished('t2', 'ok\nRESULT: DONE', review_state='approved')
        self.finished('r2', json.dumps(dict(passed=False, summary='bug', fixes=[])), role='reviewer', review_of='t2')
        run = self.advance()
        self.assertEqual(run['reviews'], dict(approved=1, changes_requested=1, skipped=1))
        self.assertIn('reviews: 1 approved, 1 changes requested, 1 skipped', run['summary'])

    def test_fixes_are_counted_from_fix_of(self):
        self.finished('t1', 'first try\nRESULT: DONE', fixed_by='t2')
        self.finished('t2', 'fixed\nRESULT: DONE', fix_of='t1')
        self.finished('t3', 'fixed again\nRESULT: DONE', fix_of='t2')
        run = self.advance()
        self.assertEqual(run['fixes'], 2)
        self.assertIn('fixes: 2', run['summary'])

    def test_old_summary_keys_are_still_present(self):
        self.finished('t1', 'ok\nRESULT: DONE')
        self.finished('t2', 'no result line')
        run = self.advance()
        self.assertEqual(run['unaccepted'], ['t2'])
        self.assertEqual(run['state'], 'awaiting_main')
        self.assertIn('needs_main_reasons', run)
        self.assertIn('machine_check_failed', run)
        self.assertIn('2 delegated task(s) finished; 1 accepted with RESULT: DONE; not accepted: t2', run['summary'])
        self.assertTrue(run['summary'].endswith('; waiting for the Main: t2'), run['summary'])

    def test_clean_run_summary_is_unchanged(self):
        self.finished('t1', 'ok\nRESULT: DONE')
        run = self.advance()
        self.assertEqual(run['outcomes'], dict(accepted=1, not_done=0, check_failed=0))
        self.assertEqual(run['summary'], '1 delegated task(s) finished; 1 accepted with RESULT: DONE; the operator reads the reports')


if __name__ == '__main__':
    unittest.main()
