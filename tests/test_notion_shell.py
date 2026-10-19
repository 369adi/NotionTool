"""Tests for notiontool.notion_shell (fake browser and DevTools session, no live calls)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from notiontool import notion_shell as ns


SECRET = 'SECRET-TOKEN-123456'
HOST_KEY = 'HOSTKEY-abcdef'
CSS = 'body{color:red}\n.nt-panel{display:block}'
JS = 'window.__ntHarnessPush=function(s){window.__ntLast=s};window.__ntHarnessReply=function(i,r){};'
REPLY_PREFIX = 'window.__ntHarnessReply&&window.__ntHarnessReply('
PUSH_PREFIX = ns._PUSH_JS.split('%s')[0]


class FakeSession:
    def __init__(self):
        self.calls = []
        self.on_event = None
        self.closed = False

    def send(self, method, params=None, timeout=15):
        if self.closed:
            raise RuntimeError('closed')
        self.calls.append((method, dict(params or {})))
        if method == 'Page.addScriptToEvaluateOnNewDocument':
            return {'identifier': 'script-1'}
        return {}

    def close(self):
        self.closed = True

    def emit(self, method, params):
        self.on_event(method, params)

    def methods(self):
        return [m for m, _ in self.calls]

    def evaluated(self):
        return [p['expression'] for m, p in self.calls if m == 'Runtime.evaluate']


class FakeBrowser:
    def __init__(self):
        self.ensured = 0
        self.opened = []
        self.http_calls = []
        self.sent = []

    def ensure(self):
        self.ensured += 1

    def new_tab(self, url):
        self.opened.append(url)
        return {'id': 'T1', 'url': url, 'webSocketDebuggerUrl': 'ws://devtools/page/T1'}

    def tab(self, ident=''):
        return {'id': ident or 'T0', 'webSocketDebuggerUrl': 'ws://devtools/page/' + (ident or 'T0')}

    def send(self, tab, method, params=None, timeout=10):
        self.sent.append((method, params))
        return {'targetId': 'T2'}

    def http(self, path):
        self.http_calls.append(path)


class SyncExecutor:
    def submit(self, fn, *args):
        fn(*args)

    def shutdown(self, wait=False):
        pass


class FakeTimer:
    def __init__(self, registry, delay, fn):
        self.delay, self.fn, self.started, self.cancelled = delay, fn, False, False
        self.daemon = False
        registry.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class FakeStore:
    def __init__(self, data=None, settings=None):
        self.data = {k: [dict(r) for r in v] for k, v in (data or {}).items()}
        self._settings = dict(settings or {})

    def all(self, kind):
        return [dict(r) for r in self.data.get(kind, [])]

    def get(self, kind, ident, default=None):
        for row in self.data.get(kind, []):
            if row.get('id') == ident:
                return dict(row)
        return default

    def put(self, kind, row):
        rows = self.data.setdefault(kind, [])
        rows[:] = [r for r in rows if r.get('id') != row['id']]
        rows.append(dict(row))

    def settings(self):
        return dict(self._settings)


def parse_reply(expression):
    assert expression.startswith(REPLY_PREFIX), expression
    decoder = json.JSONDecoder()
    ident, pos = decoder.raw_decode(expression, len(REPLY_PREFIX))
    assert expression[pos] == ','
    payload, end = decoder.raw_decode(expression, pos + 1)
    assert expression[end:] == ')', expression[end:]
    return ident, payload


def parse_push(expression):
    assert expression.startswith(PUSH_PREFIX), expression
    state, end = json.JSONDecoder().raw_decode(expression, len(PUSH_PREFIX))
    assert expression[end:] == ')'
    return state


class ShellCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        Path(self.tmp.name, 'overlay.css').write_text(CSS, encoding='utf-8')
        Path(self.tmp.name, 'overlay.js').write_text(JS, encoding='utf-8')
        self.browser = FakeBrowser()
        self.session = FakeSession()
        self.clock = [100.0]
        self.timers = []
        self.calls = []
        self.handlers = {
            'state': lambda args: {'chats': [], 'n': args.get('n', 0)},
            'run_cancel': lambda args: self.calls.append(('run_cancel', args)) or {'ok': True},
            'settings_get': lambda args: {'theme': 'dark'},
            'mcp_list': lambda args: [{'key': 'fs', 'bridge_token': 'abc', 'headers': {'x': 1}}],
            'runs': lambda args: 1 / 0,
            'mcp_refresh': lambda args: {'note': 'value ' + SECRET},
        }
        self.shell = self.make_shell()

    def make_shell(self, **kw):
        return ns.NotionShell(self.browser, 'acc1', self.handlers, url='https://notion.test/chat',
                              asset_dir=kw.get('asset_dir', self.tmp.name), secrets=(SECRET, HOST_KEY),
                              connect=lambda tab: self.session, clock=lambda: self.clock[0],
                              timer=lambda delay, fn: FakeTimer(self.timers, delay, fn), executor=SyncExecutor())

    def call(self, payload):
        before = len(self.session.evaluated())
        self.session.emit('Runtime.bindingCalled', {'name': 'ntHarness', 'payload': payload})
        new = self.session.evaluated()[before:]
        self.assertEqual(len(new), 1, new)
        return parse_reply(new[0])


class InjectionTests(ShellCase):
    def test_start_registers_binding_and_overlay_before_loading_notion(self):
        info = self.shell.start()
        self.assertTrue(info['started'])
        self.assertEqual(self.browser.ensured, 1)
        self.assertEqual(self.browser.opened, ['about:blank'])
        methods = self.session.methods()
        for name in ('Page.enable', 'Runtime.enable', 'Runtime.addBinding', 'Page.addScriptToEvaluateOnNewDocument',
                     'Page.navigate'):
            self.assertIn(name, methods)
        self.assertLess(methods.index('Runtime.addBinding'), methods.index('Page.navigate'))
        self.assertLess(methods.index('Page.addScriptToEvaluateOnNewDocument'), methods.index('Page.navigate'))
        calls = dict(self.session.calls)
        self.assertEqual(calls['Runtime.addBinding'], {'name': 'ntHarness'})
        self.assertEqual(calls['Page.navigate'], {'url': 'https://notion.test/chat'})
        self.assertEqual(self.shell.script_id, 'script-1')
        self.assertEqual(self.shell.start()['target_id'], 'T1')  # second start is a no-op
        self.assertEqual(self.session.methods().count('Runtime.addBinding'), 1)

    def test_injected_source_wraps_css_in_style_injector_and_appends_js(self):
        self.shell.start()
        source = dict(self.session.calls)['Page.addScriptToEvaluateOnNewDocument']['source']
        self.assertIn(json.dumps(CSS), source)
        self.assertIn("document.createElement('style')", source)
        self.assertIn('MutationObserver' if 'MutationObserver' in JS else 'DOMContentLoaded', source)
        self.assertIn(JS, source)
        self.assertIn('window.top !== window', source)
        self.assertTrue(source.rstrip().endswith('})();'))

    def test_fallback_target_create_when_new_tab_fails(self):
        self.browser.new_tab = lambda url: (_ for _ in ()).throw(OSError('no /json/new'))
        self.shell.start()
        self.assertEqual(self.browser.sent[0][0], 'Target.createTarget')
        self.assertEqual(self.shell.target_id, 'T2')

    def test_default_chat_url_points_to_notion_chat(self):
        self.assertTrue(ns.chat_url().endswith('/chat'))
        self.assertTrue(ns.chat_url('ab-cd').endswith('/chat?t=abcd'))
        self.assertIn('notion', ns.chat_url())


class DispatchTests(ShellCase):
    def setUp(self):
        super().setUp()
        self.shell.start()

    def test_whitelist_rejects_other_actions(self):
        for action in ('eval', 'run_shell', '__proto__', 'STATE', 'state ', None, 5):
            ident, payload = self.call(json.dumps({'id': 'x', 'action': action, 'args': {}}))
            self.assertEqual(ident, 'x')
            self.assertEqual(payload, {'ok': False, 'error': 'Action not allowed'})
        self.assertEqual(ns.ACTIONS, ('state', 'runs', 'run_pause', 'run_cancel', 'run_retry', 'task_reconcile',
                                      'settings_get', 'settings_set', 'mcp_list', 'mcp_refresh', 'run_confirm'))

    def test_handlers_outside_whitelist_are_dropped(self):
        shell = ns.NotionShell(self.browser, 'a', {'eval': lambda a: 1, 'state': lambda a: 2},
                               connect=lambda t: self.session, executor=SyncExecutor())
        self.assertEqual(set(shell.handlers), {'state'})

    def test_missing_handler_is_not_available(self):
        ident, payload = self.call(json.dumps({'id': 7, 'action': 'run_retry', 'args': {'id': 'r1'}}))
        self.assertEqual((ident, payload), (7, {'ok': False, 'error': 'Action not available'}))

    def test_reply_format_ok(self):
        ident, payload = self.call(json.dumps({'id': 'r1', 'action': 'state', 'args': {'n': 3}}))
        self.assertEqual(ident, 'r1')
        self.assertEqual(payload, {'ok': True, 'result': {'chats': [], 'n': 3}})
        ident, payload = self.call(json.dumps({'id': 'r2', 'action': 'run_cancel', 'args': {'id': 'run9'}}))
        self.assertEqual(payload, {'ok': True, 'result': {'ok': True}})
        self.assertEqual(self.calls, [('run_cancel', {'id': 'run9'})])

    def test_missing_args_default_to_empty_object(self):
        _, payload = self.call(json.dumps({'id': 'r3', 'action': 'state'}))
        self.assertEqual(payload['result']['n'], 0)

    def test_bad_json_and_exceptions_do_not_crash(self):
        for bad in ('{not json', '[1,2]', '"text"', None, ''):
            ident, payload = self.call(bad)
            self.assertIsNone(ident)
            self.assertFalse(payload['ok'])
            self.assertIn('Bad request', payload['error'])
        _, payload = self.call(json.dumps({'id': 'a', 'action': 'state', 'args': [1]}))
        self.assertEqual(payload, {'ok': False, 'error': 'args must be an object'})
        ident, payload = self.call(json.dumps({'id': 'b', 'action': 'runs'}))
        self.assertEqual(ident, 'b')
        self.assertFalse(payload['ok'])
        self.assertIn('division', payload['error'])
        _, payload = self.call(json.dumps({'id': 'c', 'action': 'state'}))
        self.assertTrue(payload['ok'])  # still working afterwards

    def test_other_bindings_and_events_are_ignored(self):
        before = len(self.session.evaluated())
        self.session.emit('Runtime.bindingCalled', {'name': 'other', 'payload': '{"id":1,"action":"state"}'})
        self.session.emit('Network.requestWillBeSent', {})
        self.assertEqual(len(self.session.evaluated()), before)

    def test_results_are_scrubbed_and_secrets_withheld(self):
        _, payload = self.call(json.dumps({'id': 'm', 'action': 'mcp_list'}))
        self.assertEqual(payload, {'ok': True, 'result': [{'key': 'fs'}]})
        _, payload = self.call(json.dumps({'id': 's', 'action': 'mcp_refresh'}))
        self.assertEqual(payload, {'ok': False, 'error': 'Reply withheld: it contained a credential'})


class PushTests(ShellCase):
    def setUp(self):
        super().setUp()
        self.shell.start()

    def pushes(self):
        return [parse_push(e) for e in self.session.evaluated() if e.startswith(PUSH_PREFIX)]

    def test_throttle_and_change_detection(self):
        self.assertTrue(self.shell.push({'ts': 1, 'v': 'A'}))
        self.assertFalse(self.shell.push({'ts': 2, 'v': 'A'}))  # only ts changed
        self.clock[0] += 0.2
        self.assertFalse(self.shell.push({'ts': 3, 'v': 'B'}))  # too soon: queued
        self.assertEqual(len(self.timers), 1)
        self.assertAlmostEqual(self.timers[0].delay, 0.8)
        self.assertTrue(self.timers[0].started)
        self.clock[0] += 0.3
        self.shell.push({'ts': 4, 'v': 'C'})  # replaces the queued state, no second timer
        self.assertEqual(len(self.timers), 1)
        self.assertEqual([p['v'] for p in self.pushes()], ['A'])
        self.clock[0] += 0.5
        self.timers[0].fn()
        self.assertEqual([p['v'] for p in self.pushes()], ['A', 'C'])
        self.clock[0] += 5
        self.assertFalse(self.shell.push({'ts': 9, 'v': 'C'}))  # unchanged
        self.assertTrue(self.shell.push({'ts': 10, 'v': 'D'}))
        self.assertEqual([p['v'] for p in self.pushes()], ['A', 'C', 'D'])

    def test_queued_state_dropped_when_back_to_sent(self):
        self.shell.push({'v': 'A'})
        self.clock[0] += 0.1
        self.shell.push({'v': 'B'})
        self.shell.push({'v': 'A'})
        self.clock[0] += 1
        self.timers[0].fn()
        self.assertEqual([p['v'] for p in self.pushes()], ['A'])

    def test_pushes_never_closer_than_one_second(self):
        times = []
        for step in range(30):
            self.clock[0] += 0.25
            before = len(self.pushes())
            self.shell.push({'v': step})
            for timer in list(self.timers):
                if not timer.cancelled and self.clock[0] >= timer.delay:
                    pass
            if len(self.pushes()) > before:
                times.append(self.clock[0])
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(g >= 1.0 - 1e-9 for g in gaps), gaps)

    def test_repush_after_new_context(self):
        self.shell.push({'v': 'A'})
        self.clock[0] += 2
        self.session.emit('Runtime.executionContextCreated', {'context': {'id': 2, 'auxData': {'isDefault': True}}})
        self.assertEqual([p['v'] for p in self.pushes()], ['A', 'A'])
        self.clock[0] += 2
        self.session.emit('Runtime.executionContextCreated', {'context': {'id': 3, 'auxData': {'isDefault': False}}})
        self.assertEqual(len(self.pushes()), 2)  # isolated worlds do not count

    def test_repush_without_state_does_nothing(self):
        self.session.emit('Runtime.executionContextCreated', {'context': {'id': 2, 'auxData': {'isDefault': True}}})
        self.assertEqual(self.pushes(), [])

    def test_push_waits_for_overlay_in_new_document(self):
        self.shell.push({'v': 1})
        expression = self.session.evaluated()[-1]
        self.assertIn('typeof window.__ntHarnessPush==="function"', expression)
        self.assertIn('setTimeout(go,250)', expression)


class CloseTests(ShellCase):
    def test_close_is_clean_and_idempotent(self):
        self.shell.start()
        self.shell.push({'v': 1})
        self.clock[0] += 0.1
        self.shell.push({'v': 2})
        self.assertTrue(self.shell.close())
        calls = self.session.calls
        self.assertIn(('Page.removeScriptToEvaluateOnNewDocument', {'identifier': 'script-1'}), calls)
        self.assertIn(('Runtime.removeBinding', {'name': 'ntHarness'}), calls)
        self.assertIn(('Target.closeTarget', {'targetId': 'T1'}), calls)
        self.assertTrue(self.session.closed)
        self.assertIsNone(self.session.on_event)
        self.assertTrue(self.timers[0].cancelled)
        self.assertEqual(self.shell.handlers, {})
        self.assertFalse(self.shell.alive)
        count = len(calls)
        self.assertFalse(self.shell.close())
        self.assertEqual(len(self.session.calls), count)
        self.shell._on_event('Runtime.bindingCalled', {'name': 'ntHarness', 'payload': '{"id":1,"action":"state"}'})
        self.assertFalse(self.shell.push({'v': 3}))
        self.assertEqual(len(self.session.calls), count)
        with self.assertRaises(RuntimeError):
            self.shell.start()

    def test_close_before_start(self):
        self.assertTrue(self.shell.close())
        self.assertEqual(self.session.calls, [])

    def test_close_falls_back_to_json_close(self):
        self.shell.start()
        original = self.session.send

        def send(method, params=None, timeout=15):
            if method == 'Target.closeTarget':
                raise RuntimeError('not here')
            return original(method, params, timeout)
        self.session.send = send
        self.shell.close()
        self.assertEqual(self.browser.http_calls, ['/json/close/T1'])


class SecretTests(ShellCase):
    def test_no_token_in_any_injected_or_evaluated_string(self):
        self.shell.start()
        self.call(json.dumps({'id': 1, 'action': 'state'}))
        self.call(json.dumps({'id': 2, 'action': 'mcp_list'}))
        self.call(json.dumps({'id': 3, 'action': 'mcp_refresh'}))
        self.call('{bad')
        self.shell.push({'v': 1, 'nested': {'api_key': HOST_KEY, 'ok': 1}})
        self.clock[0] += 2
        self.shell.push({'v': 'leak ' + SECRET})
        strings = [p.get('source', '') + p.get('expression', '') for _, p in self.session.calls]
        self.assertTrue(strings)
        for text in strings:
            for marker in (SECRET, HOST_KEY, 'Bearer ', 'X-NotionTool-Token', 'token_v2', 'document.cookie'):
                self.assertNotIn(marker, text)
        self.assertNotIn('127.0.0.1', dict(self.session.calls)['Page.addScriptToEvaluateOnNewDocument']['source'])

    def test_overlay_with_secret_or_local_host_is_not_injected(self):
        for bad in ('var t = "' + SECRET + '";', 'fetch("http://127.0.0.1:8765/api/state")', 'h["Authorization"]="Bearer x"'):
            Path(self.tmp.name, 'overlay.js').write_text(bad, encoding='utf-8')
            shell = self.make_shell()
            with self.assertRaises(ValueError):
                shell.start()
        self.assertEqual(self.session.calls, [])

    def test_missing_overlay_fails_before_opening_a_tab(self):
        shell = self.make_shell(asset_dir=Path(self.tmp.name, 'missing'))
        with self.assertRaises(OSError):
            shell.start()
        self.assertEqual(self.browser.opened, [])

    @unittest.skipUnless((ns.ASSET_DIR / 'overlay.js').exists() and (ns.ASSET_DIR / 'overlay.css').exists(),
                         'overlay assets not present yet')
    def test_real_overlay_assets_are_injectable(self):
        source = ns.NotionShell(self.browser, 'a', {}, secrets=(SECRET,), connect=lambda t: self.session,
                                executor=SyncExecutor()).injected_source()
        for marker in ns.INJECT_MARKERS + (SECRET,):
            self.assertNotIn(marker, source)


def sample_store():
    return FakeStore({
        'accounts': [{'id': 'a1'}, {'id': 'a2'}],
        'workers': [
            {'id': 'w2', 'account_id': 'a1', 'purpose': 'worker', 'label': 'Coworker 1', 'state': 'busy', 'enabled': True},
            {'id': 'w1', 'account_id': 'a1', 'purpose': 'main', 'label': 'Main 1', 'state': 'busy'},
            {'id': 'w3', 'account_id': 'a1', 'purpose': 'worker', 'label': 'Coworker 2', 'state': 'ready'},
            {'id': 'w4', 'account_id': 'a1', 'purpose': 'worker', 'label': 'Coworker 3', 'state': 'busy'},
            {'id': 'x1', 'account_id': 'a2', 'purpose': 'main', 'label': 'Other', 'state': 'ready'},
            {'id': 'p1', 'account_id': 'a1', 'purpose': 'probe', 'label': 'Probe', 'state': 'ready'},
        ],
        'chats': [{'id': 'c1', 'main_worker': 'w1'}, {'id': 'c2', 'main_worker': 'x1'}],
        'runs': [
            {'id': 'r1', 'chat_id': 'c1', 'main_worker': 'w1', 'account_id': 'a1', 'goal': 'Build the shell',
             'state': 'executing', 'stage': 'execute', 'created': 10},
            {'id': 'r0', 'chat_id': 'c1', 'account_id': 'a1', 'goal': '', 'state': 'done', 'stage': '', 'created': 5},
            {'id': 'rx', 'chat_id': 'c2', 'main_worker': 'x1', 'account_id': 'a2', 'goal': 'Other account',
             'state': 'executing', 'created': 20},
        ],
        'tasks': [
            {'id': 't1', 'run_id': 'r1', 'role': 'planner', 'title': 'Plan', 'state': 'done', 'worker_id': 'w1',
             'started': 900, 'finished': 990},
            {'id': 't2', 'run_id': 'r1', 'role': 'executor', 'title': 'Write shell', 'state': 'running',
             'worker_id': 'w2', 'started': 950},
            {'id': 't3', 'run_id': 'r1', 'role': 'executor', 'title': 'Write tests', 'state': 'done',
             'worker_id': 'w3', 'started': 940, 'finished': 990},
            {'id': 't4', 'run_id': 'r1', 'role': 'reviewer', 'title': 'Review', 'state': 'uncertain',
             'worker_id': 'w4', 'started': 960},
            {'id': 't5', 'run_id': 'r1', 'role': 'executor', 'title': 'Old', 'state': 'done', 'worker_id': 'w2',
             'started': 100, 'finished': 200},
            {'id': 't6', 'run_id': 'r1', 'role': 'executor', 'title': 'Queued', 'state': 'queued', 'worker_id': ''},
            {'id': 'tx', 'run_id': 'rx', 'role': 'executor', 'title': 'X', 'state': 'running', 'worker_id': 'x1'},
        ],
    }, settings={'harness_account_id': 'a1'})


class BuildStateTests(unittest.TestCase):
    def test_mapping_to_pushed_shape(self):
        state = ns.build_state(sample_store(), 'a1', {'limit_per_minute': 180, 'window_count': 42}, now=1000.0)
        self.assertEqual(set(state), {'ts', 'account_id', 'chats', 'links', 'runs', 'limits',
                                      'awaiting_main', 'stalled', 'stale_context_tasks'})
        self.assertEqual(state['ts'], 1000.0)
        self.assertEqual(state['account_id'], 'a1')
        self.assertEqual([c['id'] for c in state['chats']], ['w1', 'w2', 'w3', 'w4'])
        for chat in state['chats']:
            self.assertEqual(set(chat), {'id', 'title', 'role', 'state', 'task_title', 'task_role', 'started'})
        main, w2, w3, w4 = state['chats']
        self.assertEqual((main['role'], main['title'], main['task_title']), ('main', 'Main 1', ''))
        self.assertEqual((w2['role'], w2['state'], w2['task_title'], w2['task_role'], w2['started']),
                         ('coworker', 'busy', 'Write shell', 'executor', 950.0))
        self.assertEqual((w3['task_title'], w3['started']), ('', 0.0))
        self.assertEqual((w4['task_title'], w4['task_role']), ('Review', 'reviewer'))
        self.assertEqual(state['links'], [
            {'from': 'w1', 'to': 'w2', 'kind': 'assign'},
            {'from': 'w3', 'to': 'w1', 'kind': 'result'},
            {'from': 'w1', 'to': 'w4', 'kind': 'review'},
        ])
        self.assertEqual(state['runs'], [
            {'id': 'r0', 'title': 'r0', 'state': 'done', 'stage': '', 'tasks_done': 0, 'tasks_total': 0,
             'reconcile_task_ids': []},
            {'id': 'r1', 'title': 'Build the shell', 'state': 'executing', 'stage': 'execute', 'tasks_done': 3,
             'tasks_total': 6, 'reconcile_task_ids': []},
        ])
        self.assertEqual(state['limits'], {'requests_per_min': 180, 'window_used': 42})
        json.dumps(state)

    def test_selected_account_and_empty_limits(self):
        state = ns.build_state(sample_store(), None, None, now=1000.0)
        self.assertEqual(state['account_id'], 'a1')
        self.assertEqual(state['limits'], {'requests_per_min': 0, 'window_used': 0})

    def test_result_link_expires(self):
        state = ns.build_state(sample_store(), 'a1', {}, now=1100.0)
        self.assertNotIn('result', [link['kind'] for link in state['links']])

    def test_empty_store(self):
        state = ns.build_state(FakeStore(), 'a1', {}, now=1.0)
        self.assertEqual((state['chats'], state['links'], state['runs']), ([], [], []))


    def test_reconcile_task_ids_lists_remote_unknown_tasks(self):
        store = sample_store()
        store.put('tasks', {'id': 'tx1', 'run_id': 'r1', 'worker_id': 'w2', 'state': 'blocked', 'remote_unknown': True})
        store.put('tasks', {'id': 'tx2', 'run_id': 'r1', 'worker_id': 'w3', 'state': 'remote_unknown'})
        store.put('tasks', {'id': 'tx3', 'run_id': 'r1', 'worker_id': 'w3', 'state': 'failed', 'remote_unknown': False})
        store.put('tasks', {'id': 'tx4', 'run_id': 'r0', 'state': 'done'})
        base = ns.build_state(sample_store(), 'a1', {}, now=1000.0)
        state = ns.build_state(store, 'a1', {}, now=1000.0)
        runs = {r['id']: r for r in state['runs']}
        self.assertEqual(runs['r1']['reconcile_task_ids'], ['tx1', 'tx2'])
        self.assertEqual(runs['r0']['reconcile_task_ids'], [])
        for run in state['runs']:
            self.assertEqual(set(run), {'id', 'title', 'state', 'stage', 'tasks_done', 'tasks_total',
                                        'reconcile_task_ids'})
        self.assertEqual(state['chats'], base['chats'])
        self.assertEqual(state['links'], base['links'])
        self.assertEqual(set(state), set(base))


class FakeServer:
    def __init__(self, store, final_state='done'):
        self.store = store
        self.final_state = final_state
        self.calls = []
        self.token = SECRET
        self.host_api_key = HOST_KEY
        self.engine = SimpleNamespace(coordination=lambda: {'limit_per_minute': 180, 'window_count': 7})
        self.mcp_servers = SimpleNamespace(public=lambda: [{'key': 'fs', 'name': 'Files', 'tools': [1, 2]}])

    def action(self, path, body):
        self.calls.append((path, body))
        if path == '/api/main/create':
            op_id = 'create_main:' + body['request_id']
            if self.final_state:
                self.store.put('operations', {'id': op_id, 'state': self.final_state, 'chat_id': 'c9', 'title': 'Main 2'})
            self.store.put('chats', {'id': 'c9', 'main_worker': 'w9'})
            self.store.put('workers', {'id': 'w9', 'account_id': 'a1', 'purpose': 'main', 'thread_id': '1234-abcd'})
            return {'id': op_id, 'state': 'reserved', 'title': 'Main 2'}
        return {'ok': True}


class DefaultHandlerTests(unittest.TestCase):
    def setUp(self):
        self.store = sample_store()
        self.store._settings.update(theme='dark', max_workers=3, host_api_allow_network=False, host_api_port=8765,
                                    browser_path='C:/edge.exe', public_mcp_url='https://x', harness_enabled=True)
        self.server = FakeServer(self.store)
        self.handlers = ns.default_handlers(self.server, sleep=lambda s: None)

    def test_all_actions_present(self):
        self.assertEqual(tuple(self.handlers), ns.ACTIONS)

    def test_state_and_runs(self):
        state = self.handlers['state']({})
        self.assertEqual(state['account_id'], 'a1')
        self.assertEqual(state['limits'], {'requests_per_min': 180, 'window_used': 7})
        self.assertEqual([r['id'] for r in self.handlers['runs']({})], ['r0', 'r1'])

    def test_run_actions_go_through_server_action(self):
        self.handlers['run_pause']({'id': 'r1'})
        self.handlers['run_pause']({'id': 'r1', 'paused': False})
        self.handlers['run_cancel']({'id': 'r1'})
        self.handlers['run_retry']({'id': 'r1'})
        self.handlers['task_reconcile']({'id': 't2'})
        self.handlers['mcp_refresh']({})
        self.assertEqual(self.server.calls, [
            ('/api/run/pause', {'id': 'r1', 'paused': True}), ('/api/run/pause', {'id': 'r1', 'paused': False}),
            ('/api/run/cancel', {'id': 'r1'}), ('/api/run/retry', {'id': 'r1'}),
            ('/api/task/reconcile', {'id': 't2'}), ('/api/mcp/refresh', {})])
        with self.assertRaises(ValueError):
            self.handlers['run_cancel']({})

    def test_settings_get_and_set_use_allowed_keys_only(self):
        got = self.handlers['settings_get']({})
        self.assertEqual(got['theme'], 'dark')
        self.assertEqual(got['max_workers'], 3)
        for key in ('harness_account_id', 'host_api_allow_network', 'host_api_port', 'browser_path', 'public_mcp_url'):
            self.assertNotIn(key, got)
        self.handlers['settings_set']({'theme': 'light', 'max_workers': 2})
        self.handlers['settings_set']({'values': {'harness_enabled': False}})
        self.assertEqual(self.server.calls, [('/api/settings', {'theme': 'light', 'max_workers': 2}),
                                             ('/api/settings', {'harness_enabled': False})])
        for bad in ({'host_api_allow_network': True}, {'harness_account_id': 'a2'}, {'token': 'x'}, {}):
            with self.assertRaises(ValueError):
                self.handlers['settings_set'](bad)
        self.assertEqual(len(self.server.calls), 2)
        self.assertTrue(set(ns.page_settings_keys()).isdisjoint(ns.PAGE_EXCLUDED_SETTINGS))

    def test_mcp_list_drops_tools(self):
        self.assertEqual(self.handlers['mcp_list']({}), [{'key': 'fs', 'name': 'Files'}])

    def test_page_offers_no_chat_creation(self):
        # Phase 8b: the harness never creates chats from the Notion page, so there is no handler for it.
        self.assertNotIn('new_welcome_chat', ns.ACTIONS)
        for final in ('done', 'failed', 'unconfirmed', None):
            handlers = ns.default_handlers(FakeServer(sample_store(), final), sleep=lambda s: None)
            self.assertNotIn('new_welcome_chat', handlers)
        self.assertNotIn('/api/main/create', [p for p, _ in self.server.calls])

    def test_shell_secrets(self):
        self.assertEqual(ns.shell_secrets(self.server), (SECRET, HOST_KEY))

    def test_end_to_end_through_shell(self):
        session = FakeSession()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        Path(tmp.name, 'overlay.css').write_text(CSS, encoding='utf-8')
        Path(tmp.name, 'overlay.js').write_text(JS, encoding='utf-8')
        shell = ns.NotionShell(FakeBrowser(), 'a1', self.handlers, url='https://notion.test/chat', asset_dir=tmp.name,
                               secrets=ns.shell_secrets(self.server), connect=lambda t: session,
                               executor=SyncExecutor())
        shell.start()
        session.emit('Runtime.bindingCalled', {'name': 'ntHarness',
                                               'payload': json.dumps({'id': 'n', 'action': 'new_welcome_chat'})})
        ident, payload = parse_reply(session.evaluated()[-1])
        self.assertEqual(ident, 'n')
        self.assertEqual(payload, {'ok': False, 'error': 'Action not allowed'})
        session.emit('Runtime.bindingCalled', {'name': 'ntHarness',
                                               'payload': json.dumps({'id': 's', 'action': 'state'})})
        ident, payload = parse_reply(session.evaluated()[-1])
        self.assertEqual(ident, 's')
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['result']['account_id'], 'a1')
        self.assertNotIn('/api/main/create', [p for p, _ in self.server.calls])
        shell.push(self.handlers['state']({}))
        state = parse_push(session.evaluated()[-1])
        self.assertEqual(state['account_id'], 'a1')
        for _, params in session.calls:
            text = json.dumps(params)
            self.assertNotIn(SECRET, text)
            self.assertNotIn(HOST_KEY, text)
        shell.close()


class NoChatCreationTests(ShellCase):
    """Hard rule (Phase 8b): the shell started by the server only opens existing chats."""

    def server_handlers(self, server):
        from notiontool.server import Application
        return Application.shell_handlers(server)

    def assert_no_create(self, server):
        for path, _ in server.calls:
            self.assertNotIn('create', path.lower(), 'the Notion shell used a create-chat path: ' + path)

    def test_server_handlers_have_no_create_chat_action(self):
        server = FakeServer(sample_store())
        handlers = self.server_handlers(server)
        self.assertNotIn('new_welcome_chat', handlers)
        self.assertTrue(set(handlers) <= set(ns.ACTIONS))
        for fn in handlers.values():
            try:
                fn({'id': 'r1', 'values': {'theme': 'dark'}})
            except Exception:
                pass
        self.assert_no_create(server)

    def test_page_cannot_trigger_chat_creation_through_the_shell(self):
        server = FakeServer(sample_store())
        shell = ns.NotionShell(self.browser, 'a1', self.server_handlers(server), url='https://notion.test/chat',
                               asset_dir=self.tmp.name, connect=lambda tab: self.session, executor=SyncExecutor())
        shell.start()
        self.addCleanup(shell.close)
        for action in ('new_welcome_chat', 'create_chat', 'new_chat'):
            ident, payload = self.call(json.dumps({'id': action, 'action': action}))
            self.assertEqual(ident, action)
            self.assertFalse(payload['ok'])
        self.assert_no_create(server)

    def test_server_starts_the_existing_shell_without_a_create_handler(self):
        from unittest import mock
        from notiontool.server import Application
        started = []

        class Recorder:
            def __init__(self, browser, account_id, handlers, **kw):
                started.append((browser, account_id, dict(handlers), kw))

            def start(self):
                return {}

        app = FakeServer(FakeStore(sample_store().data, settings={'harness_account_id': 'a1', 'ui_mode': 'notion'}))
        app.adapter = SimpleNamespace(edge_transport=lambda account_id: SimpleNamespace(browser=self.browser))
        app.shell_handlers = lambda: Application.shell_handlers(app)
        with mock.patch.object(ns, 'NotionShell', Recorder):
            shell = Application.start_notion_shell(app)
        self.assertIsInstance(shell, Recorder)
        browser, account_id, handlers, kw = started[0]
        self.assertIs(browser, self.browser)
        self.assertEqual(account_id, 'a1')
        self.assertNotIn('new_welcome_chat', handlers)
        self.assertEqual(kw['secrets'], (SECRET, HOST_KEY))
        self.assert_no_create(app)


class UiModeSettingTests(unittest.TestCase):
    def app(self):
        import threading
        return SimpleNamespace(store=FakeStore(settings={'id': 'app', 'ui_mode': 'classic'}),
                               engine=SimpleNamespace(lock=threading.RLock()),
                               bridge=SimpleNamespace(close_local=lambda off=None: None))

    def test_default_is_classic(self):
        from notiontool.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'data')
            try:
                self.assertEqual(store.settings()['ui_mode'], 'classic')
            finally:
                store.close()

    def test_only_classic_and_notion_are_accepted(self):
        from notiontool.server import Application
        app = self.app()
        with self.assertRaisesRegex(ValueError, "ui_mode must be 'classic' or 'notion'"):
            Application.action(app, '/api/settings', {'ui_mode': 'dark'})
        self.assertIsNone(app.store.get('settings', 'app'))
        Application.action(app, '/api/settings', {'ui_mode': 'notion'})
        self.assertEqual(app.store.get('settings', 'app')['ui_mode'], 'notion')


if __name__ == '__main__':
    unittest.main()
