"""Backups (data/backups) and the read-only usage view (notiontool/backup.py)."""
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from notiontool import backup
from notiontool.store import Store

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_JS = ROOT / 'notiontool' / 'web' / 'desktop' / 'settings.js'


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store = Store(self.dir)
        (self.dir / 'desktop_app.json').write_text('{"app_compact": true}', encoding='utf-8')
        self.store.event('run_created', message='hello')

    def tearDown(self):
        try:
            self.store.db.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_backup_copies_db_app_file_and_settings(self):
        info = backup.backup_now(self.dir, self.store, reason='manual', now=1_800_000_000)
        folder = Path(info['path'])
        self.assertEqual(folder.parent, self.dir / 'backups')
        self.assertEqual(sorted(info['files']), ['desktop_app.json', 'notiontool.db', 'settings.json'])
        copy = sqlite3.connect(str(folder / 'notiontool.db'))
        try:
            kinds = [k for (k,) in copy.execute('SELECT kind FROM events')]
        finally:
            copy.close()
        self.assertIn('run_created', kinds)
        self.assertEqual(json.loads((folder / 'desktop_app.json').read_text())['app_compact'], True)
        self.assertIn('max_workers', json.loads((folder / 'settings.json').read_text()))
        self.assertEqual(backup.last_backup(self.dir)['name'], folder.name)
        self.assertFalse([p for p in (self.dir / 'backups').iterdir() if p.name.startswith('.partial-')])

    def test_backup_without_store_uses_db_file(self):
        info = backup.backup_now(self.dir, None, now=1_800_000_000)
        self.assertIn('notiontool.db', info['files'])
        self.assertNotIn('settings.json', info['files'])

    def test_keeps_last_ten(self):
        for i in range(12):
            backup.backup_now(self.dir, self.store, now=1_800_000_000 + i * 60)
        names = [b['name'] for b in backup.list_backups(self.dir)]
        self.assertEqual(len(names), 10)
        newest = time.strftime('%Y%m%d-%H%M%S', time.localtime(1_800_000_000 + 11 * 60))
        oldest = time.strftime('%Y%m%d-%H%M%S', time.localtime(1_800_000_000))
        self.assertEqual(names[0], newest)
        self.assertNotIn(oldest, names)

    def test_same_second_gets_unique_folder(self):
        a = backup.backup_now(self.dir, self.store, now=1_800_000_000)
        b = backup.backup_now(self.dir, self.store, now=1_800_000_000)
        self.assertNotEqual(a['name'], b['name'])

    def test_daily_once_per_day(self):
        t = 1_800_000_000
        self.assertIsNotNone(backup.maybe_daily(self.dir, self.store, now=t))
        self.assertIsNone(backup.maybe_daily(self.dir, self.store, now=t + 3600))
        self.assertIsNotNone(backup.maybe_daily(self.dir, self.store, now=t + backup.DAY + 1))

    def test_before_restart_with_fake_app_never_raises(self):
        class App:
            pass
        app = App()
        app.store, app.data_dir = self.store, str(self.dir)
        self.assertEqual(backup.before_restart(app)['reason'], 'restart')
        broken = App()
        broken.store, broken.data_dir = None, str(self.dir / 'missing' / '\0bad')
        self.assertIsNone(backup.before_restart(broken))

    def test_status_text(self):
        self.assertEqual(backup.status(self.dir)['last_text'], 'No backup yet')
        backup.backup_now(self.dir, self.store)
        st = backup.status(self.dir)
        self.assertEqual(st['count'], 1)
        self.assertEqual(st['keep'], 10)

    def test_desk_handlers(self):
        class Server:
            pass
        server = Server()
        server.store, server.data_dir = self.store, str(self.dir)
        h = backup.desk_handlers(server)
        self.assertEqual(sorted(h), ['backup_now', 'backup_status', 'usage_report'])
        h['backup_now']({})
        self.assertEqual(h['backup_status']({})['count'], 1)


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store = Store(self.dir)
        self.store.put('workers', dict(id='acc:w1', label='Cowork one', purpose='worker'))
        self.store.put('tasks', dict(id='t1', run_id='r', model='opus', worker_id='acc:w1', state='done'))
        self.store.put('tasks', dict(id='t2', run_id='r', model='sonnet', worker_id='acc:w2', state='done'))
        for task in ('t1', 't1', 't2'):
            self.store.event('remote_sent', task_id=task, worker_id='acc:w1')
        self.store.event('task_attempt_finished', task_id='t1', worker_id='acc:w1', state='done', duration=100.0)
        self.store.event('task_attempt_finished', task_id='t2', worker_id='acc:w2', state='done', duration=200.0)
        self.store.event('task_attempt_finished', task_id='t3', worker_id='acc:w2', state='failed', duration=999.0)

    def tearDown(self):
        try:
            self.store.db.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_report(self):
        before = self.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        report = backup.usage_report(self.store, budget=dict(limit_per_minute=12, window_count=3))
        self.assertEqual(report['requests']['last_minute'], 3)
        self.assertEqual(report['requests']['limit_per_minute'], 12)
        turns = {(t['model'], t['turns']) for t in report['turns']}
        self.assertEqual(turns, {('opus', 2), ('sonnet', 1)})
        team = {c['name']: (c['tasks'], c['done']) for c in report['coworkers']}
        self.assertEqual(team['Cowork one'], (1, 1))
        self.assertEqual(team['w2'], (2, 1))
        self.assertEqual(report['average_task_seconds'], 150.0)  # only finished (done) tasks
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], before)

    def test_report_without_budget_counts_events(self):
        report = backup.usage_report(self.store, budget={})
        self.assertEqual(report['requests']['last_minute'], 3)
        self.assertEqual(report['requests']['limit_per_minute'], 0)

    def test_report_is_read_only_connection(self):
        db = backup._readonly(self.store)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("INSERT INTO counters(name,value) VALUES('x',1)")
        finally:
            db.close()


class WiringTests(unittest.TestCase):
    def test_server_routes_and_hooks(self):
        raw = (ROOT / 'notiontool' / 'server.py').read_bytes()
        self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'))  # server.py stays CRLF
        text = raw.decode('utf-8')
        for needle in ("path=='/api/backup'", "path=='/api/backup/now'", "path=='/api/usage'",
                       'before_restart(self)', "name='daily-backup'"):
            self.assertIn(needle, text)

    def test_backup_py_is_lf(self):
        raw = (ROOT / 'notiontool' / 'backup.py').read_bytes()
        self.assertNotIn(b'\r\n', raw)

    def test_settings_js_cards(self):
        raw = SETTINGS_JS.read_bytes()
        self.assertNotIn(b'\r\n', raw)
        text = raw.decode('utf-8')
        for needle in ('Back up now', "call('backup_status'", "call('backup_now'", "call('usage_report'",
                       "'Details'", "'Backups'"):
            self.assertIn(needle, text)

    def test_settings_js_usage_lines_in_node(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        script = ("global.window={};global.document={};require(%s);var L=window.__ntDesk.settingsLogic;"
                  "var r={requests:{last_minute:3,limit_per_minute:12},average_task_seconds:150,days:7,"
                  "turns:[{day:'2026-10-19',model:'opus',turns:2}],coworkers:[{name:'Cowork one',tasks:1,done:1}]};"
                  "console.log(JSON.stringify([L.usageLines(r),L.backupText({}),L.backupText({last:{time_text:'2026-10-19 14:00'}})]));"
                  ) % json.dumps(str(SETTINGS_JS))
        out = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines, empty, last = json.loads(out.stdout)
        self.assertEqual(lines[0], 'Requests in the last minute: 3 of 12 allowed')
        self.assertEqual(lines[1], 'Average task time: 2 min 30 s')
        self.assertIn('Turns on 2026-10-19 with opus: 2', lines)
        self.assertIn('Tasks by Cowork one: 1 (1 done)', lines)
        self.assertEqual(empty, 'Last backup: none yet')
        self.assertEqual(last, 'Last backup: 2026-10-19 14:00')


if __name__ == '__main__':
    unittest.main()
