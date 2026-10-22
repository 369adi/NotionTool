"""O8 stall watch (report only) and P5 Main context usage in /api/state. Fakes only, no network."""
import copy
import types
import unittest
from unittest import mock

import notiontool.chat_context as chat_context
from notiontool import stall_watch
from notiontool.server import Application

NOW = 10_000.0
ACCOUNT = 'acc-1'


class FakeStore:
    def __init__(self):
        self.data = {'accounts': {ACCOUNT: dict(id=ACCOUNT, state='ready')}}
        self._settings = dict(context_token_limit=1200000, context_turn_limit=600)
        self.logged = []
        self.writes = 0

    def get(self, kind, ident, default=None):
        return copy.deepcopy(self.data.get(kind, {}).get(ident, default))

    def all(self, kind):
        return [copy.deepcopy(v) for v in self.data.get(kind, {}).values()]

    def put(self, kind, doc):
        self.writes += 1
        self.data.setdefault(kind, {})[doc['id']] = copy.deepcopy(doc)

    def settings(self):
        return dict(self._settings)

    def event(self, kind, run_id='', **fields):
        self.logged.append((kind, run_id, fields))

    def events(self, after=0, limit=150):
        return list(self.logged)

    def of(self, kind):
        return [e for e in self.logged if e[0] == kind]


class Recorder:
    """Records any control call; report-only code must never make one."""
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
        return record


def make_app(store):
    app = object.__new__(Application)
    app.store = store
    app.engine = Recorder()
    app.adapter = Recorder()
    app.port, app.bind = 8790, '127.0.0.1'
    app.mcp_servers = types.SimpleNamespace(public=lambda: [])
    app.mains = types.SimpleNamespace(public=lambda o: o)
    app.chat_list = lambda: []
    app.limits = lambda: {}
    return app


def stand_in(store):
    """Same stand-in shape as tests/test_ui_state.py (no Application methods)."""
    app = types.SimpleNamespace(store=store, port=8790, bind='127.0.0.1')
    app.mcp_servers = types.SimpleNamespace(public=lambda: [])
    app.mains = types.SimpleNamespace(public=lambda o: o)
    app.chat_list = lambda: []
    app.limits = lambda: {}
    app.safe_task = lambda t: t
    return app


def put_task(store, ident, sent_at, state='running', **extra):
    store.put('tasks', dict(id=ident, run_id='r1', state=state, worker_id='w1', thread_id='th1',
                            sent_at=sent_at, **extra))


def snapshot(app):
    with mock.patch('notiontool.harness_account.selected_account', lambda store: ACCOUNT), \
            mock.patch('notiontool.harness_account.main_chat', lambda store, account: None):
        return Application.snapshot(app)


class StallReportTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.store.put('workers', dict(id='w1', label='Coworker Ada', account_id=ACCOUNT, enabled=True, purpose='worker',
                                       state='busy', thread_id='th1'))

    def test_stalled_task_gets_exactly_one_event_and_no_state_change(self):
        put_task(self.store, 't1', NOW - 600)
        app = make_app(self.store)
        before, writes = copy.deepcopy(self.store.data), self.store.writes
        for tick in range(5):
            app.check_stalls(now=NOW + 60 * tick)
        events = self.store.of('task_stalled')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1], 'r1')
        self.assertEqual(events[0][2]['task_id'], 't1')
        self.assertIn('Coworker Ada stalled', events[0][2]['message'])
        self.assertIn('report only, nothing was stopped or resent', events[0][2]['message'])
        self.assertIn('Continue prompt', events[0][2]['message'])
        self.assertEqual(self.store.data, before)
        self.assertEqual(self.store.writes, writes)
        self.assertEqual(app.engine.calls, [])
        self.assertEqual(app.adapter.calls, [])

    def test_fresh_task_gets_no_event(self):
        put_task(self.store, 't1', NOW - 10)
        put_task(self.store, 't2', NOW - 900, first_output_at=NOW - 30)
        app = make_app(self.store)
        app.check_stalls(now=NOW)
        self.assertEqual(self.store.logged, [])
        self.assertEqual(app.stalled_tasks, {})

    def test_only_running_tasks_are_checked(self):
        put_task(self.store, 't1', NOW - 900, state='queued')
        put_task(self.store, 't2', NOW - 900, state='done')
        put_task(self.store, 't3', NOW - 900, remote_done_at=NOW - 800)
        app = make_app(self.store)
        app.check_stalls(now=NOW)
        self.assertEqual(self.store.logged, [])

    def test_recovered_then_stalled_again_still_one_event(self):
        put_task(self.store, 't1', NOW - 600)
        app = make_app(self.store)
        app.check_stalls(now=NOW)
        self.store.data['tasks']['t1']['last_output_at'] = NOW + 50
        app.check_stalls(now=NOW + 60)
        self.assertEqual(app.stalled_tasks, {})
        app.check_stalls(now=NOW + 600)
        self.assertIn('t1', app.stalled_tasks)
        self.assertEqual(len(self.store.of('task_stalled')), 1)

    def test_stall_check_failure_only_writes_an_event(self):
        app = make_app(self.store)
        writes = self.store.writes

        def broken(kind):
            raise RuntimeError('store down')
        self.store.all = broken
        self.assertEqual(app.check_stalls(now=NOW), {})
        self.assertEqual(app.check_stalls(now=NOW + 60), {})
        failed = self.store.of('stall_check_failed')
        self.assertEqual(len(failed), 1)  # one per distinct failure, not one per tick
        self.assertIn('store down', failed[0][2]['message'])
        self.assertEqual([e[0] for e in self.store.logged], ['stall_check_failed'])
        self.assertEqual(self.store.writes, writes)
        self.assertEqual(app.engine.calls, [])

    def test_report_stalled_never_returns_control_actions(self):
        turns = [dict(task_id='t1', thread_id='th1', sent_at=NOW - 600)]
        reports = stall_watch.report_stalled(turns, [], NOW, {})
        self.assertEqual([r['kind'] for r in reports], ['stalled'])
        self.assertEqual(reports[0]['idle_s'], 600)
        self.assertEqual(stall_watch.report_stalled(turns, [{'thread_id': 'th1'}], NOW, {}), [])
        self.assertEqual(stall_watch.report_stalled(turns, [], NOW, {'stall_watch': False}), [])
        self.assertEqual(turns, [dict(task_id='t1', thread_id='th1', sent_at=NOW - 600)])


class StateTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.store.put('workers', dict(id='w1', label='Coworker Ada', account_id=ACCOUNT, enabled=True, purpose='worker',
                                       state='busy', thread_id='th1'))
        self.store.put('workers', dict(id='m1', label='Main', account_id=ACCOUNT, enabled=True, purpose='main', state='ready',
                                       thread_id='th-main',
                                       context_usage=dict(estimated_tokens=300000, turns=150)))
        self.store.put('workers', dict(id='m2', label='Main 2', account_id=ACCOUNT, enabled=True, purpose='main', state='ready',
                                       thread_id='th-main2', context_usage=0.42))

    def test_state_has_stalled_flag_and_main_context_usage(self):
        put_task(self.store, 't1', NOW - 600)
        put_task(self.store, 't2', NOW - 5)  # fresh: never stalled
        app = make_app(self.store)
        app.check_stalls(now=NOW)
        state = snapshot(app)
        tasks = {t['id']: t for t in state['tasks']}
        self.assertTrue(tasks['t1']['stalled'])
        self.assertGreaterEqual(tasks['t1']['stall_age_s'], 600)
        self.assertNotIn('stalled', tasks['t2'])
        self.assertEqual([s['task_id'] for s in state['stalled']], ['t1'])
        usage = {u['worker_id']: u for u in state['main_context_usage']}
        self.assertEqual(set(usage), {'m1', 'm2'})
        self.assertEqual((usage['m1']['tokens'], usage['m1']['turns']), (300000, 150))
        self.assertEqual((usage['m1']['token_limit'], usage['m1']['turn_limit']), (1200000, 600))
        self.assertEqual((usage['m1']['token_percent'], usage['m1']['turn_percent']), (25.0, 25.0))
        self.assertTrue(usage['m1']['measured'])
        self.assertFalse(usage['m1']['over_limit'])
        self.assertFalse(usage['m2']['measured'])
        self.assertEqual(usage['m2']['tokens'], 0)
        self.assertNotIn('stalled', self.store.data['tasks']['t1'])  # flag lives only in /api/state
        self.assertEqual(app.engine.calls, [])

    def test_state_works_on_the_ui_state_stand_in(self):
        state = snapshot(stand_in(self.store))
        self.assertEqual(state['stalled'], [])
        self.assertEqual(len(state['main_context_usage']), 2)
        self.assertEqual(self.store.logged, [])

    def test_context_usage_failure_only_writes_an_event(self):
        app = make_app(self.store)
        self.store._settings['context_token_limit'] = 'not a number'
        writes = self.store.writes
        self.assertEqual(Application.main_context_usage(app, self.store.all('workers')), [])
        self.assertEqual(Application.main_context_usage(app, self.store.all('workers')), [])
        self.assertEqual([e[0] for e in self.store.logged], ['main_context_usage_failed'])
        self.assertEqual(self.store.writes, writes)

    def test_usage_is_reused_not_recomputed(self):
        with mock.patch.object(chat_context.ChatContext, 'usage', side_effect=AssertionError('recomputed')), \
                mock.patch.object(chat_context.ChatContext, 'observe', side_effect=AssertionError('recomputed')):
            rows = Application.main_context_usage(make_app(self.store), self.store.all('workers'))
        self.assertEqual(len(rows), 2)


if __name__ == '__main__':
    unittest.main()
