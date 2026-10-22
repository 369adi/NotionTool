"""tools/context_latency_report.py on a temp DB with the real store schema (read-only check)."""
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, 'tools', 'context_latency_report.py')


def _load_tool():
    spec = importlib.util.spec_from_file_location('context_latency_report_under_test', TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


clr = _load_tool()


def sha256(path):
    with open(path, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def build_db(path, now):
    """Same tables as notiontool/store.py (objects, events), default journal mode."""
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE IF NOT EXISTS objects(kind TEXT, id TEXT, doc TEXT, PRIMARY KEY(kind,id))')
    con.execute('CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, run_id TEXT, kind TEXT, doc TEXT)')
    base = now - 3600

    def put(kind, ident, doc):
        con.execute('INSERT INTO objects(kind, id, doc) VALUES (?,?,?)', (kind, ident, json.dumps(doc)))

    put('workers', 'w-main', {'id': 'w-main', 'purpose': 'main', 'thread_id': 'th-main', 'retired_threads': []})
    put('workers', 'w-cow', {'id': 'w-cow', 'purpose': 'coworker', 'thread_id': 'th-cow2',
                             'retired_threads': ['th-cow1']})
    tasks = [
        dict(id='t1', worker_id='w-cow', thread_id='th-cow1', started=base, sent_at=base + 5,
             first_output_at=base + 20, remote_done_at=base + 40, before_seq=0,
             submitted_prompt='first task ' * 50, raw_response='ok ' * 40, state='done', attempts=1),
        dict(id='t2', worker_id='w-cow', thread_id='th-cow1', started=base + 50, sent_at=base + 55,
             first_output_at=base + 75, remote_done_at=base + 90, before_seq=4,
             submitted_prompt='second task ' * 60, raw_response='done ' * 30, state='failed', attempts=2),
        dict(id='t3', worker_id='w-cow', thread_id='th-cow2', started=base + 100, sent_at=base + 130,
             first_output_at=base + 150, remote_done_at=base + 170, before_seq=0,
             submitted_prompt='handoff ' * 80, raw_response='ack ' * 20, state='done', attempts=1),
        dict(id='t4', worker_id='w-main', thread_id='th-main', use_main=True, started=base + 10,
             sent_at=base + 12, first_output_at=base + 30, remote_done_at=base + 45, before_seq=0,
             submitted_prompt='main task', raw_response='main reply', state='done', attempts=1),
    ]
    for t in tasks:
        put('tasks', t['id'], t)
    put('thread_generations', 'g1', {'id': 'g1', 'mode': 'new_welcome', 'worker_id': 'w-cow',
                                     'thread_id': 'th-cow1', 'successor': 'th-cow2', 'archived_at': base + 115})
    put('thread_generations', 'g2', {'id': 'g2', 'mode': 'same_thread', 'worker_id': 'w-cow',
                                     'thread_id': 'th-cow2', 'archived_at': base + 200, 'base_seq': 3})
    put('settings', 'global', {'id': 'global', 'context_token_limit': 2000000, 'context_turn_limit': 600,
                               'coworker_fresh_context': True, 'coworker_rotate_mode': 'new_welcome'})

    def entities(start, kinds):
        return [{'sequence': i + 1, 'created_at': start + 3 * i, 'kind': k, 'text': 'x' * (90 * (i + 1))}
                for i, k in enumerate(kinds)]

    flow = ['user_message', 'tool_call', 'tool_result', 'assistant_message']
    put('thread_context', 'thread_context:th-cow1', {'entities': entities(base + 5, flow + flow)})
    put('thread_context', 'thread_context:th-cow2', {'entities': entities(base + 130, flow + flow)})
    put('thread_context', 'thread_context:th-main', {'entities': entities(base + 12, flow)})
    events = [(base + 115, 'rc2ae-test', 'context_rotated', {'worker_id': 'w-cow', 'successor': 'th-cow2'}),
              (base + 5, 'rc2ae-test', 'task_sent', {'task_id': 't1'}),
              (base + 130, 'rc2ae-test', 'task_sent', {'task_id': 't3'})]
    for ts, run_id, kind, doc in events:
        con.execute('INSERT INTO events(ts, run_id, kind, doc) VALUES (?,?,?,?)', (ts, run_id, kind, json.dumps(doc)))
    con.commit()
    con.close()


class ContextLatencyReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, 'notiontool.db')
        build_db(cls.db, time.time())
        cls.files_before = sorted(os.listdir(cls.tmp.name))
        cls.sha_before = sha256(cls.db)
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        t0 = time.monotonic()
        cls.proc = subprocess.run([sys.executable, TOOL, '--db', cls.db, '--days', '14'], cwd=ROOT, env=env,
                                  capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
        cls.elapsed = time.monotonic() - t0
        cls.out = cls.proc.stdout.rstrip('\r\n')
        cls.sha_after = sha256(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_exit_code_zero_under_30s(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertLess(self.elapsed, 30.0)

    def test_all_eight_buckets_present(self):
        self.assertEqual(len(clr.LABELS), 8)
        lines = self.out.splitlines()
        for label in clr.LABELS:
            self.assertTrue(any(line.startswith(label + ' ') for line in lines), 'bucket row missing: ' + label)

    def test_switch_line_present(self):
        lines = [line for line in self.out.splitlines() if line.startswith('switch (thread_generations new_welcome): ')]
        self.assertEqual(len(lines), 1, self.out)
        self.assertIn('n=1 matched=1 unmatched=0', lines[0])
        self.assertIn('context_rotated events=1', lines[0])

    def test_output_at_most_3900_chars(self):
        self.assertTrue(self.out)
        self.assertLessEqual(len(self.out), 3900)
        self.assertNotIn('[truncated]', self.out)

    def test_db_unchanged(self):
        self.assertEqual(self.sha_before, self.sha_after)
        self.assertEqual(self.files_before, sorted(os.listdir(self.tmp.name)))

    def test_insert_through_read_only_connection_fails(self):
        con = clr.connect(self.db)
        try:
            self.assertEqual(con.execute('PRAGMA query_only').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("INSERT INTO events(ts, run_id, kind, doc) VALUES (1, 'x', 'probe', '{}')")
                con.commit()
        finally:
            con.close()
        self.assertEqual(self.sha_before, sha256(self.db))


if __name__ == '__main__':
    unittest.main()
