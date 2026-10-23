"""db_query / db_schema: safe read-only SQL for coworkers (notiontool/db_query.py and the bridge tools)."""
import inspect
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from notiontool import db_query
from notiontool.bridge import Bridge, DB_TOOLS, READ_ONLY_ROLES, TOOLS
from notiontool import engine as engine_module
from notiontool.engine import Engine
from notiontool.store import Store
import tests.test_engine as base

SLOW = 'WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c'


def scratch_root():
    root = Path(__file__).resolve().parent / '.test-data'
    root.mkdir(exist_ok=True)
    return root


class DbQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=scratch_root())
        self.db = Path(self.temp.name) / 'test.db'
        con = sqlite3.connect(self.db)
        con.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, note TEXT)')
        con.executemany('INSERT INTO items (name, note) VALUES (?, ?)', [('n%d' % i, 'x') for i in range(700)])
        con.execute('CREATE TABLE wide (id INTEGER PRIMARY KEY, body TEXT)')
        con.executemany('INSERT INTO wide (body) VALUES (?)', [('y' * 250,) for _ in range(100)])
        con.commit()
        con.close()
        self.before = self.db.read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def test_select_works(self):
        result = db_query.run_query(self.db, 'SELECT id, name FROM items WHERE id <= 3 ORDER BY id')
        self.assertEqual(result, dict(columns=['id', 'name'], rows=[[1, 'n0'], [2, 'n1'], [3, 'n2']],
                                      more=False, truncated=False))

    def test_with_select_and_recursive_cte_work(self):
        result = db_query.run_query(self.db, 'WITH t AS (SELECT name FROM items WHERE id = 5) SELECT * FROM t')
        self.assertEqual(result['rows'], [['n4']])
        result = db_query.run_query(self.db, 'WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 5) SELECT sum(x) FROM c')
        self.assertEqual(result['rows'], [[15]])

    def test_semicolon_inside_text_or_comment_is_not_a_second_statement(self):
        self.assertEqual(db_query.run_query(self.db, "SELECT ';' AS s")['rows'], [[';']])
        self.assertEqual(db_query.run_query(self.db, "SELECT 'it''s; fine'")['rows'], [["it's; fine"]])
        self.assertEqual(db_query.run_query(self.db, 'SELECT 1 -- ; DROP TABLE items')['rows'], [[1]])
        self.assertEqual(db_query.run_query(self.db, 'SELECT 1;')['rows'], [[1]])

    def test_writes_and_other_statements_are_refused(self):
        statements = (
            "INSERT INTO items (name) VALUES ('x')",
            "UPDATE items SET name = 'x'",
            'DELETE FROM items',
            'DROP TABLE items',
            'CREATE TABLE t2 (a)',
            'ALTER TABLE items ADD COLUMN c',
            'REPLACE INTO items (id, name) VALUES (1, 2)',
            "ATTACH DATABASE ':memory:' AS other",
            'PRAGMA user_version = 5',
            'PRAGMA query_only = OFF',
            'PRAGMA journal_mode = DELETE',
            'VACUUM',
            'WITH x AS (SELECT 1) DELETE FROM items WHERE id IN (SELECT * FROM x)',
            'WITH x AS (SELECT 1) UPDATE items SET name = 1',
            'WITH x AS (SELECT 1 AS a) INSERT INTO items (name) SELECT a FROM x',
            '  /* hide */ delete from items',
            "SELECT load_extension('nothing')",
            '',
            '   ',
        )
        for sql in statements:
            with self.subTest(sql=sql):
                with self.assertRaises(ValueError):
                    db_query.run_query(self.db, sql)
        with self.assertRaises(ValueError):
            db_query.run_query(self.db, None)
        self.assertEqual(self.db.read_bytes(), self.before)  # the file was never touched
        self.assertEqual(db_query.run_query(self.db, 'SELECT count(*) FROM items')['rows'], [[700]])

    def test_two_statements_are_refused(self):
        for sql in ('SELECT 1; SELECT 2', 'SELECT 1; DROP TABLE items', "SELECT 1; INSERT INTO items (name) VALUES ('x')",
                    'SELECT 1 /* x */ ; DELETE FROM items', 'SELECT 1;; SELECT 2'):
            with self.subTest(sql=sql):
                with self.assertRaisesRegex(ValueError, 'one statement'):
                    db_query.run_query(self.db, sql)
        self.assertEqual(self.db.read_bytes(), self.before)

    def test_row_limit(self):
        default = db_query.run_query(self.db, 'SELECT id FROM items')
        self.assertEqual((len(default['rows']), default['more']), (50, True))
        self.assertEqual(len(db_query.run_query(self.db, 'SELECT id FROM items', limit=7)['rows']), 7)
        top = db_query.run_query(self.db, 'SELECT id FROM items', limit=100000)
        self.assertEqual((len(top['rows']), top['more'], top['truncated']), (500, True, False))
        self.assertEqual(len(db_query.run_query(self.db, 'SELECT id FROM items', limit=0)['rows']), 1)
        self.assertEqual(len(db_query.run_query(self.db, 'SELECT id FROM items', limit='3')['rows']), 3)
        everything = db_query.run_query(self.db, 'SELECT id FROM items WHERE id <= 10', limit=10)
        self.assertEqual((len(everything['rows']), everything['more']), (10, False))
        with self.assertRaises(ValueError):
            db_query.run_query(self.db, 'SELECT 1', limit='many')

    def test_cell_limit(self):
        result = db_query.run_query(self.db, 'SELECT hex(zeroblob(500)) AS big, 5 AS small')
        big = result['rows'][0][0]
        self.assertEqual((len(big), big.endswith('...'), result['truncated']), (db_query.MAX_CELL, True, True))
        self.assertEqual(result['rows'][0][1], 5)

    def test_total_text_limit(self):
        result = db_query.run_query(self.db, 'SELECT body FROM wide', limit=100)
        size = sum(len(name) for name in result['columns']) + sum(len(str(c)) for row in result['rows'] for c in row)
        self.assertLessEqual(size, db_query.MAX_CHARS)
        self.assertTrue(0 < len(result['rows']) < 100)
        self.assertTrue(result['truncated'] and result['more'])

    def test_slow_recursive_query_is_stopped(self):
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, 'stopped after 1 s'):
            db_query.run_query(self.db, SLOW, timeout=1)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(db_query.run_query(self.db, 'SELECT 1')['rows'], [[1]])  # still usable afterwards

    def test_documented_limits_and_defaults(self):
        self.assertEqual((db_query.DEFAULT_ROWS, db_query.MAX_ROWS, db_query.MAX_CELL, db_query.MAX_CHARS, db_query.TIMEOUT_S),
                         (50, 500, 300, 6000, 30))
        params = inspect.signature(db_query.run_query).parameters
        self.assertEqual((params['limit'].default, params['timeout'].default), (50, 30))

    def test_sql_errors_and_missing_file_are_clear(self):
        with self.assertRaisesRegex(ValueError, 'SQL error'):
            db_query.run_query(self.db, 'SELECT * FROM nothing_here')
        missing = Path(self.temp.name) / 'missing.db'
        with self.assertRaisesRegex(ValueError, 'not found'):
            db_query.run_query(missing, 'SELECT 1')
        self.assertFalse(missing.exists())  # opening read-only never creates a file

    def test_schema_lists_tables_and_columns(self):
        self.assertEqual(db_query.schema(self.db), dict(tables=[
            dict(name='items', columns=['id', 'name', 'note']), dict(name='wide', columns=['id', 'body'])]))
        self.assertEqual(self.db.read_bytes(), self.before)


class BridgeDbToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=scratch_root())
        project = (Path(self.temp.name) / 'project').resolve()
        project.mkdir()
        self.store = Store(Path(self.temp.name) / 'data')
        self.engine = Engine(self.store, base.FakeAdapter())
        self.store.put('runs', dict(id='r1', chat_id='c', folder_id='', goal='Docs', state='executing',
                                    project_path=str(project)))
        self.roles = list(READ_ONLY_ROLES) + ['auditor', 'executor']
        for role in self.roles:
            self.store.put('tasks', dict(id='a-' + role, run_id='r1', state='running', lease='lease-' + role, files=[],
                                         role=role, title=role, worker_id='w-' + role))
        settings = dict(self.store.settings(), role_definitions=[
            dict(name='auditor', tools='read_only', description='Custom read-only role')])
        patcher = mock.patch.object(self.store, 'settings', return_value=settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bridge = Bridge(self.store, self.engine)

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def test_tools_are_registered(self):
        self.assertEqual(DB_TOOLS, ('db_query', 'db_schema'))
        for name in DB_TOOLS:
            self.assertIn(name, TOOLS)
            self.assertIn(name, self.bridge.tools())
        self.assertEqual(set(TOOLS['db_query'][1]), {'sql', 'limit'})

    def test_database_path_comes_from_the_store(self):
        self.assertEqual(self.bridge.db_path(), Path(self.store.directory) / 'notiontool.db')
        self.assertTrue(self.bridge.db_path().is_file())

    def test_every_role_including_read_only_roles_can_use_both_tools(self):
        for role in self.roles:
            with self.subTest(role=role):
                token = 'lease-' + role
                if role != 'executor':
                    self.assertTrue(self.bridge.role_is_read_only(role))
                schema = self.bridge.call('db_schema', {}, token)
                self.assertTrue(schema['tables'])
                self.assertTrue(all(t['name'] and t['columns'] for t in schema['tables']))
                result = self.bridge.call('db_query', {'sql': "SELECT name FROM sqlite_master WHERE type='table'", 'limit': 5}, token)
                self.assertEqual(result['columns'], ['name'])
                self.assertTrue(0 < len(result['rows']) <= 5)

    def test_read_only_role_stays_unable_to_write_but_writes_through_the_tool_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'read-only PC access'):
            self.bridge.call('write_file', {'path': 'x.txt', 'text': 'x'}, 'lease-reviewer')
        for sql in ('DELETE FROM objects', 'PRAGMA query_only = OFF', 'SELECT 1; SELECT 2',
                    'WITH x AS (SELECT 1) DELETE FROM objects'):
            with self.subTest(sql=sql):
                with self.assertRaises(ValueError):
                    self.bridge.call('db_query', {'sql': sql}, 'lease-reviewer')
        self.assertEqual(self.store.get('runs', 'r1')['goal'], 'Docs')

    def test_db_tools_count_as_read_only_tools_for_the_engine(self):
        self.assertTrue(set(DB_TOOLS) <= set(engine_module.READ_ONLY_TOOLS))

    def test_db_calls_are_not_side_effects_so_a_resend_stays_allowed(self):
        task = dict(self.store.get('tasks', 'a-reviewer'), started=time.time() - 60)
        self.bridge.call('db_query', {'sql': 'SELECT 1'}, 'lease-reviewer')  # logs a real tool_started event
        self.bridge.call('db_schema', {}, 'lease-reviewer')
        self.store.event('tool_started', 'r1', task_id=task['id'], tool='db_query', message='Database: SELECT 1')
        self.assertFalse(self.engine._attempt_writes(task))
        self.store.event('tool_started', 'r1', task_id=task['id'], tool='write_file', message='Write: x')
        self.assertTrue(self.engine._attempt_writes(task))  # control: a real write still counts

    def test_tool_needs_a_valid_assignment(self):
        with self.assertRaises(Exception):
            self.bridge.call('db_query', {'sql': 'SELECT 1'}, 'no-such-lease')


if __name__ == '__main__':
    unittest.main()
