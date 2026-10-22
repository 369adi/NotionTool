"""Owner rule: at the hard usage limit ALL chats stop at once, the Main chat too.

The Main only works through the MCP bridge, so the bridge refuses its tools (and the coworkers'
tools) with a 'LIMIT_STOP:' error while the 6 h token budget is used up. Only a read-only allow
list still works. Temporary Store only: no real Notion, no data/notiontool.db and no port 8790
(urllib.request.urlopen is mocked, so nt_api never opens a connection).
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from notiontool import token_budget
from notiontool.bridge import LIMIT_EVENT, TOOL_SERVER, Bridge
from notiontool.engine import Engine
from notiontool.store import Store
import tests.test_engine as base

LIMIT = 1000
TOKEN = 'tok-cowork'


class FakeResponse:
    status = 200

    def __init__(self, body):
        self.body = body

    def read(self, size=-1):
        return json.dumps(self.body).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class MainLimitStopTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.project = (Path(self.temp.name) / 'project').resolve()
        (self.project / 'docs').mkdir(parents=True)
        self.store = Store(Path(self.temp.name) / 'data')
        self.store.put('settings', dict(id='app', token_budget_6h=LIMIT))
        self.engine = Engine(self.store, base.FakeAdapter())
        self.store.put('runs', dict(id='r1', chat_id='c', folder_id='', goal='Docs', state='executing',
                                    project_path=str(self.project)))
        self.store.put('tasks', dict(id='t1', run_id='r1', state='running', lease=TOKEN, files=['docs/A.md'],
                                     role='executor', title='t1', worker_id='w1'))
        self.requests = []

        def urlopen(req, timeout=None):
            self.requests.append((req.get_method(), req.full_url))
            return FakeResponse({'ok': True})

        self.dev = mock.MagicMock(return_value={'exit_code': 0, 'stdout': 'ok', 'stderr': ''})
        for patcher in (mock.patch('notiontool.devtools._backup', return_value=''),
                        mock.patch('urllib.request.urlopen', side_effect=urlopen),
                        mock.patch('notiontool.bridge.dev_call', self.dev)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bridge = Bridge(self.store, self.engine)
        self.now = float(int(time.time()))
        self.bridge._limit_now = lambda: self.now      # window clock for hard_stop_state
        self.mono = [1000.0]
        self.bridge._limit_clock = lambda: self.mono[0]  # event throttle clock
        self.app = SimpleNamespace(port=1, host_api_key='test-key', adapter=None)

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    # -- helpers ---------------------------------------------------------------------------------
    def over_limit(self):
        token_budget.charge(self.store, dict(id='main', label='Main', purpose='main'), LIMIT + 1, now=self.now)

    def next_window(self):
        self.now = token_budget.window_start(self.now) + token_budget.WINDOW_SECONDS + 1

    def events(self, kind):
        return [e for e in self.store.events(limit=2000) if e['kind'] == kind]

    def assert_stopped(self, fn, *args):
        with self.assertRaises(token_budget.LimitStop) as caught:
            fn(*args)
        text = str(caught.exception)
        self.assertTrue(text.startswith('LIMIT_STOP:'), text)
        for part in ('hard usage limit reached', 'Stop now', 'do no further work', 'tell the user', 'resets in about'):
            self.assertIn(part, text)
        self.assertLessEqual(len(text), 400)  # rpc keeps 400 characters of the error
        return text

    def rpc(self, name, arguments, operator=None):
        reply = self.bridge.rpc(dict(jsonrpc='2.0', id=1, method='tools/call',
                                     params=dict(name=name, arguments=arguments)), operator)
        return reply['result']

    def write(self, text='new\n'):
        return self.bridge.call('write_file', {'path': 'docs/A.md', 'text': text}, TOKEN)

    # -- token_budget.hard_stop_state --------------------------------------------------------------
    def test_hard_stop_state_follows_exhausted(self):
        state = token_budget.hard_stop_state(self.store, self.now)
        self.assertEqual(state, dict(stop=False, reason='', wait=0))
        self.over_limit()
        state = token_budget.hard_stop_state(self.store, self.now)
        self.assertTrue(state['stop'])
        self.assertEqual(state['wait'], token_budget.exhausted(self.store, self.now))
        self.assertTrue(0 < state['wait'] <= token_budget.WINDOW_SECONDS)
        self.assertIn('6 h token budget', state['reason'])
        self.next_window()
        self.assertFalse(token_budget.hard_stop_state(self.store, self.now)['stop'])

    def test_hard_stop_state_never_raises(self):
        broken = SimpleNamespace(get=mock.Mock(side_effect=RuntimeError('db locked')), settings=lambda: {})
        self.assertEqual(token_budget.hard_stop_state(broken), dict(stop=False, reason='', wait=0))
        self.assertEqual(token_budget.hard_stop_state(None), dict(stop=False, reason='', wait=0))

    # -- under the limit ---------------------------------------------------------------------------
    def test_under_limit_tools_pass(self):
        self.assertEqual(self.bridge.operator_call(self.app, 'nt_api', dict(method='POST', path='/api/send', body={'x': 1}))['status'], 200)
        self.assertEqual(self.requests, [('POST', 'http://127.0.0.1:1/api/send')])
        self.bridge.operator_call(self.app, 'nt_shell', dict(command='echo ok'))
        self.assertEqual(self.dev.call_count, 1)
        self.write('before\n')
        self.assertEqual((self.project / 'docs' / 'A.md').read_bytes(), b'before\n')
        self.assertEqual(self.events(LIMIT_EVENT), [])

    # -- over the limit: Main ------------------------------------------------------------------------
    def test_over_limit_main_tools_refused(self):
        self.over_limit()
        target = self.project / 'docs' / 'Main.md'
        for name, args in (('nt_shell', dict(command='echo ok')),
                           ('nt_api', dict(method='POST', path='/api/send', body={'x': 1})),
                           ('nt_api', dict(path='/api/send', body={'x': 1})),        # body without method = POST
                           ('nt_api', dict(method='POST', path='/api/health')),       # health only as GET
                           ('nt_api', dict(method='GET', path='/api/state')),         # GET outside the allow list
                           ('nt_write_file', dict(path=str(target), content='x')),
                           ('nt_edit_file', dict(path=str(target), old_text='a', new_text='b')),
                           ('nt_read_file', dict(path=str(target))),
                           ('nt_local_mcp_call', dict(server='roblox', name='x')),
                           ('nt_notion_call', dict(method='getSpaces', body={})),
                           ('nt_restart_app', {})):
            self.assert_stopped(self.bridge.operator_call, self.app, name, args)
        self.assertEqual(self.dev.call_count, 0)
        self.assertEqual(self.requests, [])
        self.assertFalse(target.exists())

    def test_over_limit_main_via_rpc_gets_limit_stop_text(self):
        self.over_limit()
        local = next(n for n in TOOL_SERVER if n.endswith('_tools'))
        for name, args, operator in (('nt_shell', dict(command='echo ok'), self.app),
                                     ('nt_api', dict(method='POST', path='/api/send', body={}), self.app),
                                     (local, {}, self.app),
                                     # Main chat scope (no operator, no token): coworker tools through mcp_call
                                     ('mcp_call', dict(tool_name='screen_capture', arguments={}), None),
                                     ('mcp_call', dict(tool_name='write_file', arguments={'path': 'docs/A.md', 'text': 'x'}), None)):
            result = self.rpc(name, args, operator)
            self.assertTrue(result.get('isError'), (name, result))
            self.assertTrue(result['content'][0]['text'].startswith('LIMIT_STOP:'), result)
        self.assertEqual(self.dev.call_count, 0)
        self.assertEqual(self.requests, [])

    # -- over the limit: coworkers ---------------------------------------------------------------------
    def test_over_limit_coworker_tools_refused(self):
        self.over_limit()
        target = self.project / 'docs' / 'A.md'
        self.assert_stopped(self.write)
        self.assert_stopped(self.bridge.call, 'read_file', {'path': 'docs/A.md'}, TOKEN)
        self.assert_stopped(self.bridge.call, 'run_shell', {'command': 'echo ok'}, TOKEN)
        self.assert_stopped(self.bridge.call, 'write_file', {'path': 'docs/A.md', 'text': 'x'}, 'not-a-token')
        self.assert_stopped(self.bridge.operator_call, self.app, 'nt_write_file',
                            dict(path=str(target), content='x', assignment_token=TOKEN))
        result = self.rpc('mcp_call', dict(tool_name='write_file', assignment_token=TOKEN,
                                          arguments={'path': 'docs/A.md', 'text': 'x'}))
        self.assertTrue(result['content'][0]['text'].startswith('LIMIT_STOP:'))
        self.assertFalse(target.exists())
        self.assertEqual(self.events('tool_started'), [])  # refused before any tool started

    # -- allow list ------------------------------------------------------------------------------------
    def test_allow_list_passes_over_limit(self):
        # A long job started before the limit keeps running; nt_job_result still reads it.
        job = self.bridge.long_jobs().run('operator', 'nt_shell', {}, lambda: (time.sleep(.3), {'done': True})[1], limit=0)
        self.assertTrue(job['still_running'])
        self.over_limit()
        result = self.rpc('nt_job_result', dict(job_id=job['job_id'], wait_s=5), self.app)
        self.assertFalse(result.get('isError'), result)
        self.assertEqual(json.loads(result['content'][0]['text']), {'done': True})
        unknown = self.rpc('nt_job_result', dict(job_id='job-missing'), self.app)
        self.assertFalse(unknown['content'][0]['text'].startswith('LIMIT_STOP:'))
        for name, args in (('mcp_servers', {}), ('mcp_list_tools', {}), ('mcp_list_tools', {'tool_name': 'write_file'})):
            result = self.rpc(name, args, self.app)
            self.assertFalse(result.get('isError'), (name, result))
            self.assertFalse(self.rpc(name, args).get('isError'))  # also in the Main's chat scope
        for path in ('/api/health', '/api/usage', '/api/usage?window=6h'):
            self.assertEqual(self.bridge.operator_call(self.app, 'nt_api', dict(method='GET', path=path))['status'], 200)
        self.assertEqual(self.bridge.operator_call(self.app, 'nt_api', dict(path='/api/health'))['status'], 200)
        self.assertEqual([m for m, _ in self.requests], ['GET'] * 4)
        self.assertEqual(self.events(LIMIT_EVENT), [])

    # -- event ------------------------------------------------------------------------------------------
    def test_one_event_per_minute(self):
        self.over_limit()
        self.assert_stopped(self.bridge.operator_call, self.app, 'nt_shell', dict(command='a'))
        self.assert_stopped(self.bridge.operator_call, self.app, 'nt_shell', dict(command='b'))
        self.assert_stopped(self.write)
        events = self.events(LIMIT_EVENT)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]['tool'], events[0]['caller']), ('nt_shell', 'main'))
        self.assertGreater(events[0]['wait_s'], 0)
        self.mono[0] += 61
        self.assert_stopped(self.write)
        events = self.events(LIMIT_EVENT)
        self.assertEqual(len(events), 2)
        self.assertEqual((events[1]['tool'], events[1]['caller']), ('write_file', 'worker'))

    # -- window reset --------------------------------------------------------------------------------------
    def test_tools_pass_again_after_window_reset(self):
        self.over_limit()
        self.assert_stopped(self.bridge.operator_call, self.app, 'nt_shell', dict(command='echo ok'))
        self.assert_stopped(self.write)
        self.next_window()
        self.bridge.operator_call(self.app, 'nt_shell', dict(command='echo ok'))
        self.assertEqual(self.dev.call_count, 1)
        self.assertEqual(self.bridge.operator_call(self.app, 'nt_api', dict(method='POST', path='/api/send', body={}))['status'], 200)
        self.write('after reset\n')
        self.assertEqual((self.project / 'docs' / 'A.md').read_bytes(), b'after reset\n')


if __name__ == '__main__':
    unittest.main()
