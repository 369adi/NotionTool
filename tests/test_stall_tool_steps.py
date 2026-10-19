"""Stall watch sees tool steps: a coworker that calls tools but streams no text is not reported as stalled."""
import json
import shutil
import time
import types
import unittest
import uuid
from pathlib import Path

from notiontool.server import Application, TOOL_STEP_KINDS
from notiontool.store import Store

ROOT = Path(__file__).resolve().parent / '.test-data'


class StallToolStepTests(unittest.TestCase):
    def setUp(self):
        self.dir = ROOT / ('stall-tool-' + uuid.uuid4().hex[:8])
        self.store = Store(self.dir)
        self.app = types.SimpleNamespace(store=self.store)
        self.now = time.time()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def put_task(self, ident, **extra):
        task = dict(id=ident, run_id='r1', state='running', worker_id='w1', thread_id='th1',
                    sent_at=self.now - 600, first_output_at=None, remote_done_at=None, **extra)
        self.store.put('tasks', task)
        return task

    def add_event(self, kind, task_id, ts):
        with self.store.lock:
            self.store.db.execute('INSERT INTO events(ts,run_id,kind,doc) VALUES(?,?,?,?)',
                                  (ts, 'r1', kind, json.dumps(dict(task_id=task_id))))
            self.store.db.commit()

    def stalled_ids(self):
        return [e.get('task_id') for e in self.store.events(limit=500) if e['kind'] == 'task_stalled']

    def test_newest_tool_event_wins(self):
        task = self.put_task('t1')
        self.add_event('tool_started', 't1', self.now - 120)
        self.add_event('verify_done', 't1', self.now - 20)
        self.add_event('tool_started', 'other', self.now - 1)
        self.assertAlmostEqual(Application.last_tool_step(self.app, task), self.now - 20, places=3)

    def test_stored_value_kept_when_newer_or_no_events(self):
        self.assertEqual(Application.last_tool_step(self.app, self.put_task('t2', last_tool_at=self.now - 5)), self.now - 5)
        self.add_event('tool_finished', 't3', self.now - 300)
        self.assertEqual(Application.last_tool_step(self.app, self.put_task('t3', last_tool_at=self.now - 5)), self.now - 5)
        self.assertIsNone(Application.last_tool_step(self.app, self.put_task('t4')))

    def test_other_event_kinds_ignored(self):
        self.add_event('task_started', 't5', self.now - 1)
        self.assertIsNone(Application.last_tool_step(self.app, self.put_task('t5')))
        self.assertIn('tool_started', TOOL_STEP_KINDS)

    def test_store_without_events_table_never_raises(self):
        fake = types.SimpleNamespace(store=types.SimpleNamespace())
        self.assertEqual(Application.last_tool_step(fake, dict(id='x', last_tool_at=7.0)), 7.0)

    def test_check_stalls_quiet_while_tools_run(self):
        self.put_task('busy')
        self.add_event('tool_started', 'busy', self.now - 10)
        self.put_task('idle')
        Application.check_stalls(self.app, self.now)
        ids = self.stalled_ids()
        self.assertIn('idle', ids)
        self.assertNotIn('busy', ids)


if __name__ == '__main__':
    unittest.main()