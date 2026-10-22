"""Problem inbox: Live view 'Problems' list (live.js liveUtil.problems) and POST /api/ui/error."""
import json
import os
import subprocess
import types
import unittest
from pathlib import Path

from notiontool import problems

ROOT = Path(__file__).resolve().parents[1]
LIVE_JS = ROOT / 'notiontool' / 'web' / 'desktop' / 'live.js'
SERVER = ROOT / 'notiontool' / 'server.py'
PROBLEMS_PY = ROOT / 'notiontool' / 'problems.py'
NODE = os.environ.get('NODE') or r'C:\Program Files\nodejs\node.exe'


class FakeStore:
    def __init__(self):
        self.events = []

    def event(self, kind, **fields):
        self.events.append(dict(kind=kind, **fields))


class FakeServer:
    def __init__(self, clock=None):
        self.store = FakeStore()
        if clock is not None:
            self._nt_ui_errors = problems.UiErrorLimiter(clock=clock)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class UiErrorTests(unittest.TestCase):
    def test_record_writes_one_plain_event(self):
        server = FakeServer()
        result = problems.handle(server, {'message': 'x is   not defined', 'part': 'live', 'line': 12})
        self.assertEqual(result, {'ok': True, 'recorded': True})
        self.assertEqual(server.store.events, [{'kind': 'ui_error', 'message': 'Overlay error in live: x is not defined (line 12)'}])

    def test_identical_errors_are_deduped(self):
        clock = Clock()
        server = FakeServer(clock)
        body = {'message': 'boom', 'part': 'live'}
        self.assertTrue(problems.record(server, body)['recorded'])
        again = problems.record(server, body)
        self.assertEqual((again['recorded'], again['reason']), (False, 'duplicate'))
        self.assertEqual(len(server.store.events), 1)
        clock.now += problems.REPEAT_SECONDS + 1
        self.assertTrue(problems.record(server, body)['recorded'])

    def test_rate_limited_per_minute(self):
        clock = Clock()
        server = FakeServer(clock)
        results = [problems.record(server, {'message': 'error %d' % i}) for i in range(problems.MAX_PER_WINDOW + 3)]
        self.assertEqual(sum(1 for r in results if r['recorded']), problems.MAX_PER_WINDOW)
        self.assertEqual(results[-1]['reason'], 'rate_limited')
        clock.now += problems.WINDOW_SECONDS + 1
        self.assertTrue(problems.record(server, {'message': 'later'})['recorded'])
        self.assertEqual(len(server.store.events), problems.MAX_PER_WINDOW + 1)

    def test_bad_bodies(self):
        server = FakeServer()
        with self.assertRaises(ValueError):
            problems.record(server, {'message': '   '})
        with self.assertRaises(ValueError):
            problems.record(server, None)
        self.assertTrue(problems.record(server, {'message': 'm', 'line': 'abc'})['recorded'])
        self.assertLessEqual(len(server.store.events[0]['message']), 400)

    def test_install_desk_actions_is_idempotent_and_keeps_handlers(self):
        def desktop_handlers(server):
            return {'state': lambda args: 'state'}
        desktop_handlers.nt_restart_actions = True
        mod = types.SimpleNamespace(DESK_ACTIONS=('state',), desktop_handlers=desktop_handlers)
        problems.install_desk_actions(mod)
        problems.install_desk_actions(mod)
        self.assertEqual(mod.DESK_ACTIONS, ('state', 'ui_error'))
        self.assertTrue(mod.desktop_handlers.nt_restart_actions)
        server = FakeServer()
        handlers = mod.desktop_handlers(server)
        self.assertEqual(handlers['state']({}), 'state')
        self.assertTrue(handlers['ui_error']({'message': 'from page'})['recorded'])
        self.assertEqual(server.store.events[0]['kind'], 'ui_error')

    def test_real_desktop_shell_gets_ui_error(self):
        from notiontool import desktop_shell
        problems.install_desk_actions(desktop_shell)
        self.assertIn('ui_error', desktop_shell.DESK_ACTIONS)
        self.assertTrue(getattr(desktop_shell.desktop_handlers, 'nt_problem_actions', False))


class FileTests(unittest.TestCase):
    def test_server_route_and_install_keep_crlf(self):
        data = SERVER.read_bytes()
        route = b"if method=='POST' and path=='/api/ui/error':"
        self.assertEqual(data.count(route), 1)
        start = data.index(route)
        block = data[start:data.index(b"path=='/api/run/confirm'", start)]
        self.assertIn(b'problems.handle(app,self.read_body())', block)
        self.assertEqual(block.count(b'\n'), block.count(b'\r\n'))
        self.assertEqual(data.count(b'problems.install_desk_actions()'), 2)
        for line in data.split(b'\n'):
            if b'problems.install_desk_actions()' in line or b'from . import problems' in line:
                self.assertTrue(line.endswith(b'\r'), line)

    def test_new_python_is_lf(self):
        self.assertNotIn(b'\r\n', PROBLEMS_PY.read_bytes())
        self.assertNotIn(b'\r\n', LIVE_JS.read_bytes())

    def test_live_static(self):
        js = LIVE_JS.read_text(encoding='utf-8')
        for needle in ("section('needs', 'Problems'", "addEventListener('error', onError)", "removeEventListener('error', onError)",
                       "ctx.call('ui_error'", 'What to do: ', "'Dismiss'", 'tabBadge(count)', 'problems: problems'):
            self.assertIn(needle, js)
        self.assertNotIn('fetch(', js)
        self.assertNotIn('innerHTML', js)

    @unittest.skipUnless(os.path.exists(NODE), 'node not installed')
    def test_node_check(self):
        subprocess.run([NODE, '--check', str(LIVE_JS)], check=True, capture_output=True)


NODE_SCRIPT = r'''
var fs = require('fs'), vm = require('vm');
var sandbox = { window: { __ntDesk: {} }, console: console };
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[process.argv.length - 1], 'utf8'), sandbox);
var u = sandbox.window.__ntDesk.liveUtil;
var now = 1800000000000, t = now / 1000;
var state = {
  runs: [{ id: 'r1', state: 'blocked', goal: 'Fix login', created: t - 50 },
         { id: 'r2', state: 'awaiting_main', goal: 'Write docs', finished: t - 10 },
         { id: 'r3', state: 'running', goal: 'Other' }],
  tasks: [{ id: 't1', run_id: 'r1', state: 'failed', title: 'Run tests', error: 'Timeout. More text', finished: t - 40 },
          { id: 't2', run_id: 'r3', state: 'done', title: 'Lost one', remote_unknown: true, finished: t - 30 },
          { id: 't3', run_id: 'r3', state: 'done', title: 'Proofed', proof: { tests_status: 'failed', created: t - 20 } },
          { id: 't4', run_id: 'r3', state: 'done', title: 'Checked', remote_unknown: true, checked: true },
          { id: 't5', run_id: 'r3', state: 'done', title: 'Green', proof: { tests_status: 'passed' } }],
  events: [{ kind: 'welcome_warning', message: 'Team chat could not be created.', ts: t - 5 },
           { kind: 'welcome_warning', message: 'Team chat could not be created.', ts: t - 3 },
           { kind: 'ui_error', message: 'Overlay error in live: boom', ts: t - 2 },
           { kind: 'run_confirmed', message: 'fine', ts: t - 1 }]
};
var all = u.problems(state, { now: now, connection: u.connection(null, now, 0) });
var dismissed = u.problems(state, { now: now, dismissed: { 'retry-r1': 1, conn: 1 }, connection: u.connection(null, now, 0) });
var online = u.problems(state, { now: now, connection: u.connection({}, now, now - 1000) });
var stale = u.problems(state, { now: now, connection: u.connection({}, now, now - 120000) });
function slim(p) { return { key: p.key, what: p.what, todo: p.todo, action: p.def ? p.def.action : null, count: p.count, at: p.at }; }
process.stdout.write(JSON.stringify({ all: all.map(slim), dismissed: dismissed.map(slim).map(function (p) { return p.key; }),
  online: online.map(function (p) { return p.key; }), stale: stale.filter(function (p) { return p.key === 'conn'; }).map(slim),
  empty: u.problems({}, {}).length }));
'''


@unittest.skipUnless(os.path.exists(NODE), 'node not installed')
class NodeProblemsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run([NODE, '-e', NODE_SCRIPT, str(LIVE_JS)], check=True, capture_output=True, text=True,
                             encoding='utf-8')
        cls.data = json.loads(out.stdout)
        cls.items = {p['key']: p for p in cls.data['all']}

    def test_all_sources_collected(self):
        keys = set(self.items)
        for key in ('retry-r1', 'ok-r2', 'task-t1', 'accept-t2', 'proof-t3', 'conn'):
            self.assertIn(key, keys)
        self.assertTrue(any(k.startswith('ev-welcome_warning') for k in keys))
        self.assertTrue(any(k.startswith('ev-ui-') for k in keys))
        self.assertNotIn('accept-t4', keys)
        self.assertNotIn('proof-t5', keys)
        self.assertFalse(any('run_confirmed' in k or 'r3' in k for k in keys))
        self.assertEqual(self.data['empty'], 0)

    def test_every_item_has_plain_sentences_and_time(self):
        for p in self.data['all']:
            self.assertTrue(p['what'].endswith('.'), p)
            self.assertTrue(p['todo'].endswith('.'), p)
            self.assertIsNotNone(p['at'], p)

    def test_actions_only_where_possible(self):
        self.assertEqual(self.items['retry-r1']['action'], 'run_retry')
        self.assertEqual(self.items['ok-r2']['action'], 'run_confirm')
        self.assertEqual(self.items['accept-t2']['action'], 'task_reconcile')
        for key in ('task-t1', 'proof-t3', 'conn'):
            self.assertIsNone(self.items[key]['action'])
        actions = [p['action'] for p in self.data['all']]
        first_none = actions.index(None)
        self.assertTrue(all(a is None for a in actions[first_none:]), 'action items come first')

    def test_task_reason_and_hint(self):
        task = self.items['task-t1']
        self.assertIn('Timeout', task['what'])
        self.assertNotIn('More text', task['what'])
        self.assertIn('Try again', task['todo'])

    def test_identical_problems_deduped(self):
        warnings = [p for p in self.data['all'] if p['key'].startswith('ev-welcome_warning')]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]['count'], 2)

    def test_dismissed_and_connection(self):
        self.assertNotIn('retry-r1', self.data['dismissed'])
        self.assertNotIn('conn', self.data['dismissed'])
        self.assertIn('ok-r2', self.data['dismissed'])
        self.assertNotIn('conn', self.data['online'])
        self.assertEqual(len(self.data['stale']), 1)
        self.assertIn('no update for', self.data['stale'][0]['what'])
        self.assertEqual(self.items['conn']['what'], 'The harness is not connected.')


if __name__ == '__main__':
    unittest.main()
