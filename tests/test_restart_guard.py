"""Safe restart (notiontool/restart_guard.py, POST /api/app/restart, Settings tab card). Fakes only."""
import json
import shutil
import subprocess
import types
import unittest
from pathlib import Path

from notiontool import restart_guard as rg

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_JS = ROOT / 'notiontool' / 'web' / 'desktop' / 'settings.js'
SERVER_PY = ROOT / 'notiontool' / 'server.py'
NODE = shutil.which('node') or (r'C:\Program Files\nodejs\node.exe' if Path(r'C:\Program Files\nodejs\node.exe').exists() else None)


class FakeStore:
    def __init__(self, tasks=()):
        self.tasks = [dict(t) for t in tasks]
        self.events = []

    def all(self, kind):
        return list(self.tasks) if kind == 'tasks' else []

    def event(self, kind, run_id='', **fields):
        self.events.append(dict(fields, kind=kind))


class FakeEngine:
    def _choose_worker(self, task, run, settings):
        return {'id': 'w1'}


class FakeServer:
    def __init__(self, tasks=()):
        self.store = FakeStore(tasks)
        self.engine = FakeEngine()

    def health(self):
        return {'ok': True, 'host': 'NotionTool'}


def make(tasks=(), fail=False):
    server = FakeServer(tasks)
    launched, backups, spawned = [], [], []

    def launcher(argv):
        if fail:
            raise OSError('no powershell')
        launched.append(argv)
        return 4242

    def backup(srv):
        backups.append(srv)
        return {'name': '20261019-120000'}
    ctl = rg.controller(server, launcher=launcher, backup=backup, spawn=spawned.append, sleep=lambda s: None)
    return server, ctl, launched, backups, spawned


RUNNING = [{'id': 't1', 'title': 'Fix login', 'state': 'running'},
           {'id': 't2', 'title': 'Write docs', 'state': 'uncertain'},
           {'id': 't3', 'title': 'Old one', 'state': 'done'}]


class RestartGuardTests(unittest.TestCase):
    def test_when_idle_pauses_new_tasks_then_restarts_once_idle(self):
        server, ctl, launched, backups, spawned = make(RUNNING)
        res = ctl.request('idle')
        self.assertEqual(res['state'], 'waiting')
        self.assertEqual([t['id'] for t in res['running']], ['t1', 't2'])
        self.assertEqual(len(spawned), 1)
        self.assertIsNone(server.engine._choose_worker({}, {}, {}))  # no new task starts
        self.assertFalse(ctl.check())
        self.assertEqual(launched, [])
        server.store.tasks[0]['state'] = 'preparing'
        server.store.tasks[1]['state'] = 'done'
        self.assertFalse(ctl.check())  # preparing still counts as busy
        server.store.tasks[0]['state'] = 'done'
        self.assertTrue(ctl.check())
        self.assertEqual(len(launched), 1)
        self.assertEqual(len(backups), 1)
        self.assertEqual(ctl.status()['state'], 'restarting')
        kinds = [e['kind'] for e in server.store.events]
        self.assertEqual(kinds, ['app_restart_waiting', 'app_restart'])
        self.assertEqual(server.store.events[-1]['backup'], '20261019-120000')

    def test_when_idle_without_tasks_restarts_right_away(self):
        server, ctl, launched, backups, spawned = make()
        res = ctl.request()
        self.assertTrue(res['started'])
        self.assertEqual(res['state'], 'restarting')
        self.assertEqual(len(launched), 1)
        self.assertEqual(spawned, [])

    def test_now_lists_running_tasks_and_notes_manual_check(self):
        server, ctl, launched, backups, spawned = make(RUNNING)
        res = ctl.request('now')
        self.assertTrue(res['started'])
        self.assertEqual([t['title'] for t in res['running']], ['Fix login', 'Write docs'])
        note = server.store.events[-1]
        self.assertEqual(note['kind'], 'app_restart')
        self.assertIn('manual checking', note['message'])
        self.assertEqual(note['running'], ['t1', 't2'])
        self.assertEqual(len(backups), 1)

    def test_never_restarts_twice(self):
        server, ctl, launched, backups, spawned = make()
        ctl.request('now')
        again = ctl.request('now')
        self.assertFalse(again['ok'])
        self.assertTrue(again['already'])
        self.assertFalse(ctl.request('idle')['ok'])
        self.assertEqual(len(launched), 1)
        server2, ctl2, launched2, _, _ = make(RUNNING)
        ctl2.request('idle')
        self.assertTrue(ctl2.request('now')['already'])  # waiting also blocks a second restart
        self.assertEqual(launched2, [])

    def test_failed_launch_releases_hold(self):
        server, ctl, launched, backups, spawned = make(RUNNING, fail=True)
        ctl.request('idle')
        server.store.tasks = []
        ctl.check()
        self.assertEqual(ctl.state, 'failed')
        self.assertIn('could not be started', ctl.error)
        self.assertEqual(server.engine._choose_worker({}, {}, {}), {'id': 'w1'})
        self.assertNotIn('_choose_worker', server.engine.__dict__)
        self.assertEqual(server.store.events[-1]['kind'], 'app_restart_failed')
        self.assertTrue(ctl.request('now').get('ok') is False)  # retry allowed after a failure

    def test_cancel_lets_tasks_start_again(self):
        server, ctl, launched, backups, spawned = make(RUNNING)
        ctl.request('idle')
        res = ctl.request('cancel')
        self.assertEqual(res['state'], 'idle')
        self.assertEqual(server.engine._choose_worker({}, {}, {}), {'id': 'w1'})
        self.assertTrue(ctl.check())
        self.assertEqual(launched, [])
        self.assertFalse(ctl.request('cancel')['ok'])

    def test_bad_mode_refused(self):
        _, ctl, _, _, _ = make()
        with self.assertRaises(ValueError):
            ctl.request('reboot')

    def test_default_backup_uses_backup_module_when_present(self):
        from notiontool import backup
        server = FakeServer()
        calls = []
        old = getattr(backup, 'before_restart', None)
        backup.before_restart = lambda srv: calls.append(srv) or {'name': 'b1'}
        try:
            ctl = rg.RestartController(server, launcher=lambda argv: 1, spawn=lambda fn: None)
            ctl.request('now')
        finally:
            if old is None:
                del backup.before_restart
            else:
                backup.before_restart = old
        self.assertEqual(calls, [server])
        self.assertEqual(ctl.backup_name, 'b1')

    def test_restart_argv_is_detached_hidden_start_process(self):
        argv = rg.restart_argv(Path(r'C:\x y\work\_restart_bg.ps1'))
        self.assertEqual(argv[0], 'powershell.exe')
        self.assertIn('Hidden', argv)
        self.assertTrue(argv[-1].startswith('Start-Process powershell -WindowStyle Hidden -ArgumentList'))
        self.assertIn('-File "C:\\x y\\work\\_restart_bg.ps1"', argv[-1])
        self.assertTrue(str(rg.SCRIPT).endswith('_restart_bg.ps1'))
        self.assertTrue(rg.SCRIPT.exists())

    def test_handle_and_desk_handlers(self):
        server, ctl, launched, _, _ = make()
        self.assertEqual(rg.handle(server, {'status': True})['state'], 'idle')
        self.assertEqual(launched, [])
        desk = rg.desk_handlers(server)
        self.assertEqual(desk['app_health']({})['ok'], True)
        self.assertEqual(desk['app_restart']({'mode': 'now'})['state'], 'restarting')
        self.assertEqual(desk['app_restart_status']({})['state'], 'restarting')
        self.assertEqual(len(launched), 1)

    def test_install_desk_actions_is_idempotent(self):
        fake = types.SimpleNamespace(DESK_ACTIONS=('state',), desktop_handlers=lambda server, data_dir=None: {'state': 1})
        rg.install_desk_actions(fake)
        rg.install_desk_actions(fake)
        self.assertEqual(fake.DESK_ACTIONS, ('state', 'app_restart', 'app_restart_status', 'app_health'))
        handlers = fake.desktop_handlers(FakeServer(), None)
        self.assertEqual(sorted(handlers), ['app_health', 'app_restart', 'app_restart_status', 'state'])

    def test_install_on_real_desktop_shell(self):
        from notiontool import desktop_shell
        rg.install_desk_actions()
        for name in rg.DESK_ACTION_NAMES:
            self.assertIn(name, desktop_shell.DESK_ACTIONS)

    def test_server_route_and_line_endings(self):
        raw = SERVER_PY.read_bytes()
        self.assertIn(b"if path=='/api/app/restart':", raw)
        self.assertIn(b'return restart_guard.handle(self,body)', raw)
        self.assertIn(b'restart_guard.install_desk_actions()', raw)
        self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'))  # server.py stays CRLF
        self.assertNotIn(b'\r\n', (ROOT / 'notiontool' / 'restart_guard.py').read_bytes())


JS_HARNESS = r"""
global.window = {};
global.document = {};
require(process.argv[2]);
var L = window.__ntDesk.settingsLogic;
var input = JSON.parse(process.argv[3]);
var script = input.script.slice(), states = [], calls = [];
function fakeCall(action, args) {
  calls.push(action);
  var next = script.shift();
  if (!next) return Promise.reject(new Error('down'));
  if (next.reject) return Promise.reject(new Error(next.reject));
  return Promise.resolve(next);
}
L.watchRestart(fakeCall, { resumed: input.resumed, interval: 1, tries: 20,
  wait: function () { return Promise.resolve(); },
  onState: function (s) { states.push(s); } }).then(function (res) {
  console.log(JSON.stringify({ res: res, states: states, calls: calls,
    confirm: L.restartConfirmText(input.running || []),
    empty: L.restartConfirmText([]),
    waiting: L.restartText({ state: 'waiting', running: input.running || [] }),
    refused: L.restartText({ ok: false, error: 'A restart is already in progress' }) }));
});
"""


@unittest.skipUnless(NODE, 'node not installed')
class RestartCardJsTests(unittest.TestCase):
    def run_js(self, payload):
        out = subprocess.run([NODE, '-e', JS_HARNESS, '', str(SETTINGS_JS), json.dumps(payload)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_restarting_then_offline_then_back_online(self):
        r = self.run_js({'script': [{'state': 'restarting'}, {'reject': 'down'}, {'state': 'idle'}, {'ok': True}],
                         'running': [{'id': 't1', 'title': 'Fix login', 'state': 'running'}]})
        self.assertEqual(r['res']['state'], 'online')
        self.assertEqual(r['states'], ['restarting', 'offline', 'online'])
        self.assertEqual(r['calls'][-1], 'app_health')
        self.assertIn('Fix login (running)', r['confirm'])
        self.assertIn('Their answers may need manual checking.', r['confirm'])
        self.assertIn('No task is running', r['empty'])
        self.assertIn('1 task to finish', r['waiting'])
        self.assertEqual(r['refused'], 'A restart is already in progress')

    def test_resumed_after_reinject_goes_online(self):
        r = self.run_js({'script': [{'state': 'idle'}, {'ok': True}], 'resumed': True})
        self.assertEqual(r['states'], ['online'])

    def test_nothing_pending_stays_quiet(self):
        r = self.run_js({'script': [{'state': 'idle'}]})
        self.assertEqual(r['res']['state'], 'idle')
        self.assertEqual(r['states'], [])

    def test_waits_while_old_server_answers_health_before_going_down(self):
        r = self.run_js({'script': [{'state': 'waiting', 'running': [1]}, {'state': 'restarting'},
                                    {'reject': 'down'}, {'reject': 'down'}, {'state': 'idle'}, {'ok': True}]})
        self.assertEqual(r['states'], ['waiting', 'restarting', 'offline', 'offline', 'online'])


if __name__ == '__main__':
    unittest.main()
