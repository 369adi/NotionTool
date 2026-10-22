"""Owner rule: at the hard usage limit ALL chats stop at once, the Main chat too.

The Main only works through the MCP bridge, so the bridge refuses its tools (and the coworkers'
tools) with a 'LIMIT_STOP:' error while the 6 h token budget is used up. During a burst pause only
coworker tools are refused. A read-only allow list still works. Temporary Store only: no real
Notion, no data/notiontool.db and no port 8790 (urllib.request.urlopen is mocked, so nt_api never
opens a connection; dev_call and the screen capture are mocked too).
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from notiontool import token_budget
from notiontool.bridge import LIMIT_ALLOWED_API_POSTS, LIMIT_EVENT, TOOL_SERVER, Bridge
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

        self.mono = [1000.0]
        self.dev = mock.MagicMock(return_value={'exit_code': 0, 'stdout': 'ok', 'stderr': ''})
        token_budget.clear_hard_stop_cache()
        self.addCleanup(token_budget.clear_hard_stop_cache)
        for patcher in (mock.patch('notiontool.devtools._backup', return_value=''),
                        mock.patch('urllib.request.urlopen', side_effect=urlopen),
                        mock.patch('notiontool.bridge.dev_call', self.dev),
                        mock.patch.object(token_budget, '_clock', lambda: self.mono[0])):  # 1 s cache clock
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bridge = Bridge(self.store, self.engine)
        self.now = float(int(time.time()))
        self.bridge._limit_now = lambda: self.now      # window clock for hard_stop_state
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

    def burst_pause(self):
        # Coworker tokens only: 9M in 15 min is above burst_hard_15m (8M); the 6 h budget is untouched.
        token_budget._rate_add(self.store, self.now, 9_000_000, 'worker', True)

    def next_window(self):
        self.now = token_budget.window_start(self.now) + token_budget.WINDOW_SECONDS + 1

    def events(self, kind):
        return [e for e in self.store.events(limit=2000) if e['kind'] == kind]

    def assert_stopped(self, fn, *args):
        with self.assertRaises(token_budget.LimitStop) as caught:
            fn(*args)
        text = str(caught.exception)
        self.assertTrue(text.startswith('LIMIT_STOP:'), text)
        for part in ('hard usage limit reached', 'Stop now', 'do no further work', 'tell the user', 'resets in about',
                     'Read-only tools still work'):
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
        clear = dict(stop=False, source='', reason='', wait=0)
        self.assertEqual(token_budget.hard_stop_state(self.store, 'main', self.now), clear)
        self.over_limit()
        self.mono[0] += 2  # past the 1 s cache
        for purpose in ('main', 'worker'):  # the 6 h budget stops everyone
            state = token_budget.hard_stop_state(self.store, purpose, self.now)
            self.assertTrue(state['stop'])
            self.assertEqual(state['source'], '6h')
            self.assertEqual(state['wait'], token_budget.exhausted(self.store, self.now))
            self.assertTrue(0 < state['wait'] <= token_budget.WINDOW_SECONDS)
            self.assertIn('6 h token budget', state['reason'])
        self.next_window()
        self.assertEqual(token_budget.hard_stop_state(self.store, 'main', self.now), clear)

    def test_hard_stop_state_burst_pause_only_for_worker(self):
        self.burst_pause()
        self.assertEqual(token_budget.burst_state(self.store, self.now)['level'], 'pause')
        self.assertFalse(token_budget.hard_stop_state(self.store, 'main', self.now)['stop'])
        state = token_budget.hard_stop_state(self.store, 'worker', self.now)
        self.assertTrue(state['stop'])
        self.assertEqual(state['source'], 'burst')
        self.assertEqual(state['wait'], 120)
        self.assertIn('coworkers paused', state['reason'])

    def test_hard_stop_state_cached_for_one_second_per_purpose(self):
        self.assertFalse(token_budget.hard_stop_state(self.store, 'main', self.now)['stop'])
        self.over_limit()
        self.mono[0] += .5
        self.assertFalse(token_budget.hard_stop_state(self.store, 'main', self.now)['stop'])  # cached
        self.assertTrue(token_budget.hard_stop_state(self.store, 'worker', self.now)['stop'])  # own entry
        self.mono[0] += .6  # 1.1 s after the first check
        self.assertTrue(token_budget.hard_stop_state(self.store, 'main', self.now)['stop'])

    def test_hard_stop_state_never_raises_and_logs_fail_open(self):
        broken = SimpleNamespace(get=mock.Mock(side_effect=RuntimeError('db locked')), settings=lambda: {},
                                 event=mock.Mock())
        clear = dict(stop=False, source='', reason='', wait=0)
        self.assertEqual(token_budget.hard_stop_state(broken, 'main'), clear)
        broken.event.assert_called_once()
        self.assertEqual(broken.event.call_args[0][0], 'token_budget_failed')
        self.assertEqual(broken.event.call_args[1]['error'], 'RuntimeError')
        broken.event.side_effect = RuntimeError('event table locked')  # the log itself may fail too
        self.assertEqual(token_budget.hard_stop_state(broken, 'worker'), clear)
        self.assertEqual(token_budget.hard_stop_state(None, 'main'), clear)

    # -- design test 1: under the limit ---------------------------------------------------------------
    def test_under_limit_tools_pass(self):
        self.assertEqual(self.bridge.operator_call(self.app, 'nt_api', dict(method='POST', path='/api/send', body={'x': 1}))['status'], 200)
        self.assertEqual(self.requests, [('POST', 'http://127.0.0.1:1/api/send')])
        self.bridge.operator_call(self.app, 'nt_shell', dict(command='echo ok'))
        self.assertEqual(self.dev.call_count, 1)
        self.write('before\n')
        self.assertEqual((self.project / 'docs' / 'A.md').read_bytes(), b'before\n')
        self.assertEqual(self.events(LIMIT_EVENT), [])

    # -- design test 2: over the limit, Main -------------------------------------------------------------
    def test_over_limit_main_tools_refused(self):
        self.over_limit()
        target = self.project / 'docs' / 'Main.md'
        for name, args in (('nt_shell', dict(command='echo ok')),
                           ('nt_api', dict(method='POST', path='/api/send', body={'x': 1})),
                           ('nt_api', dict(path='/api/send', body={'x': 1})),        # body without method = POST
                           ('nt_api', dict(method='POST', path='/api/health')),       # POST outside the stop routes
                           ('nt_api', dict(method='POST', path='/api/settings', body={})),
                           ('nt_api', dict(method='DELETE', path='/api/run/cancel')),
                           ('nt_write_file', dict(path=str(target), content='x')),
                           ('nt_edit_file', dict(path=str(target), old_text='a', new_text='b')),
                           ('nt_memory_update', dict(text='x')),
                           ('nt_local_mcp_call', dict(server='roblox', name='x')),
                           ('nt_local_mcp', dict(refresh=True)),
                           ('nt_notion_call', dict(method='getSpaces', body={})),
                           ('nt_restart_app', {})):
            self.assert_stopped(self.bridge.operator_call, self.app, name, args)
        self.assertEqual(self.dev.call_count, 0)
        self.assertEqual(self.requests, [])
        self.assertFalse(target.exists())
        self.assertEqual(self.events('tool_started'), [])  # refused before any tool started

    def test_over_limit_main_via_rpc_gets_limit_stop_text(self):
        self.over_limit()
        cases = [('nt_shell', dict(command='echo ok'), self.app),
                 ('nt_api', dict(method='POST', path='/api/send', body={}), self.app),
                 # design test 4: Main chat scope (no operator, no token), coworker tool through mcp_call
                 ('mcp_call', dict(tool_name='write_file', arguments={'path': 'docs/A.md', 'text': 'x'}), None)]
        local = next((n for n in TOOL_SERVER if not n.endswith('_tools')), None)
        if local:
            cases.append((local, {}, self.app))  # a local MCP tool (not discovery) is refused too
        for name, args, operator in cases:
            result = self.rpc(name, args, operator)
            self.assertTrue(result.get('isError'), (name, result))
            self.assertTrue(result['content'][0]['text'].startswith('LIMIT_STOP:'), result)
        self.assertEqual(self.dev.call_count, 0)
        self.assertEqual(self.requests, [])
        self.assertFalse((self.project / 'docs' / 'A.md').exists())

    # -- design test 5: over the limit, coworkers -----------------------------------------------------------
    def test_over_limit_coworker_write_refused_read_allowed(self):
        target = self.project / 'docs' / 'A.md'
        target.write_bytes(b'old\n')
        self.over_limit()
        self.assert_stopped(self.write)
        self.assert_stopped(self.bridge.call, 'run_shell', {'command': 'echo ok'}, TOKEN)
        self.assert_stopped(self.bridge.call, 'write_file', {'path': 'docs/A.md', 'text': 'x'}, 'not-a-token')
        self.assert_stopped(self.bridge.operator_call, self.app, 'nt_write_file',
                            dict(path=str(target), content='x', assignment_token=TOKEN))
        result = self.rpc('mcp_call', dict(tool_name='write_file', assignment_token=TOKEN,
                                          arguments={'path': 'docs/A.md', 'text': 'x'}))
        self.assertTrue(result['content'][0]['text'].startswith('LIMIT_STOP:'))
        self.assertEqual(target.read_bytes(), b'old\n')
        self.assertEqual(self.events('tool_started'), [])  # refused before any tool started
        read = self.bridge.call('read_file', {'path': 'docs/A.md'}, TOKEN)
        self.assertIn('old', json.dumps(read))
        self.bridge.call('list_files', {'path': 'docs'}, TOKEN)

    # -- design test 3: allow list ------------------------------------------------------------------------
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
        self.assertEqual(self.events(LIMIT_EVENT), [])

    def test_design_allow_list_read_tools_api_get_and_stop_routes(self):
        target = self.project / 'docs' / 'Main.md'
        target.write_bytes(b'x\n')
        self.over_limit()
        for name in ('nt_read_file', 'nt_list_dir', 'nt_search_code', 'nt_file_outline', 'nt_memory_read'):
            args = dict(path=str(target)) if name in ('nt_read_file', 'nt_file_outline') else \
                dict(path=str(self.project), pattern='x') if name == 'nt_search_code' else \
                dict(path=str(self.project)) if name == 'nt_list_dir' else {}
            self.bridge.operator_call(self.app, name, args)
        self.assertEqual(self.dev.call_count, 5)
        for path in ('/api/state', '/api/health', '/api/usage?window=6h'):
            self.assertEqual(self.bridge.operator_call(self.app, 'nt_api', dict(method='GET', path=path))['status'], 200)
        self.assertEqual(self.bridge.operator_call(self.app, 'nt_api', dict(path='/api/state'))['status'], 200)
        for path in LIMIT_ALLOWED_API_POSTS:
            self.assertEqual(self.bridge.operator_call(self.app, 'nt_api', dict(method='POST', path=path, body={'id': 'r1'}))['status'], 200)
        self.assertEqual([m for m, _ in self.requests], ['GET'] * 4 + ['POST'] * 2)
        self.assertEqual(sorted(LIMIT_ALLOWED_API_POSTS), ['/api/run/cancel', '/api/run/pause'])
        screen = SimpleNamespace(call=mock.Mock(return_value={'windows': []}))
        self.bridge._screen = screen
        with mock.patch.object(self.bridge, 'screen_access', return_value=True):
            self.bridge.operator_call(self.app, 'nt_screen_windows', {})
            self.bridge.operator_call(self.app, 'nt_screen_capture', {})
            result = self.rpc('mcp_call', dict(tool_name='screen_capture', arguments={}))  # Main chat scope
            self.assertFalse(str(result['content'][0].get('text', '')).startswith('LIMIT_STOP:'), result)
        self.assertGreaterEqual(screen.call.call_count, 2)
        for name in (n for n in TOOL_SERVER if n.endswith('_tools')):  # local MCP discovery
            self.assertTrue(Bridge.limit_allowed(name, {}))
        self.assertFalse(Bridge.limit_allowed('nt_local_mcp', {}))
        self.assertEqual(self.events(LIMIT_EVENT), [])

    # -- design test 6: burst pause -----------------------------------------------------------------------
    def test_burst_pause_refuses_coworker_not_main(self):
        self.burst_pause()
        text = self.assert_stopped(self.write)
        self.assertIn('burst', text)
        self.assertFalse((self.project / 'docs' / 'A.md').exists())
        self.bridge.operator_call(self.app, 'nt_shell', dict(command='echo ok'))  # the Main is not paused
        self.assertEqual(self.dev.call_count, 1)
        events = self.events(LIMIT_EVENT)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]['purpose'], events[0]['source'], events[0]['wait_s']), ('worker', 'burst', 120))

    # -- refusal event ------------------------------------------------------------------------------------
    def test_one_event_per_minute(self):
        self.over_limit()
        self.assert_stopped(self.bridge.operator_call, self.app, 'nt_shell', dict(command='a'))
        self.assert_stopped(self.bridge.operator_call, self.app, 'nt_shell', dict(command='b'))
        events = self.events(LIMIT_EVENT)
        self.assertEqual(len(events), 1)  # same (caller, tool) within 60 s
        first = events[0]
        self.assertEqual((first['tool'], first['caller'], first['purpose'], first['source']),
                         ('nt_shell', 'operator', 'main', '6h'))
        self.assertGreater(first['wait_s'], 0)
        self.assertNotIn(TOKEN, json.dumps(first))
        self.assert_stopped(self.write)  # another (caller, tool): own event
        self.assert_stopped(self.write)
        events = self.events(LIMIT_EVENT)
        self.assertEqual(len(events), 2)
        self.assertEqual({(e['tool'], e['caller'], e['purpose']) for e in events},
                         {('nt_shell', 'operator', 'main'), ('write_file', 'worker', 'worker')})
        self.mono[0] += 61
        self.assert_stopped(self.bridge.operator_call, self.app, 'nt_shell', dict(command='c'))
        self.assertEqual(len(self.events(LIMIT_EVENT)), 3)

    # -- design test 7: window reset ----------------------------------------------------------------------
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
