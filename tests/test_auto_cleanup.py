"""Auto cleanup of stale or broken runs and tasks (owner rule 2026-10-22, stale_tasks.sweep, rule I5)."""
import tempfile
import time
import unittest
from pathlib import Path

from notiontool import file_registry
from notiontool import stale_tasks
from notiontool.engine import Engine
from notiontool.store import Store

H = 3600


class NoSendAdapter:
    """Fails on any send or reconciliation; turn_outcome is the only (read) call it allows."""
    def __init__(self):
        self.calls = []
        self.outcome = None

    def run(self, worker, task, prompt, cancel, update):
        self.calls.append(('run', task['id']))
        raise AssertionError('Auto cleanup must never send anything')

    def recover(self, worker, task, cancel):
        self.calls.append(('recover', task['id']))
        raise AssertionError('Auto cleanup must never start a reconciliation check')

    def client(self, account_id):
        return 'client-' + account_id

    def turn_outcome(self, client, thread_id):
        self.calls.append(('turn_outcome', thread_id))
        if self.outcome is None:
            raise RuntimeError('no thread record')
        return self.outcome


def fields(event):
    out = dict(event)
    for key in ('doc', 'fields'):
        if isinstance(event.get(key), dict):
            out.update(event[key])
    return out


class AutoCleanupTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.adapter = NoSendAdapter()
        self.now = time.time()
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        self.store.put('chats', dict(id='c', title='Main', main_worker='m', folder_id='f', project_path=self.temp.name))
        self.engine = Engine(self.store, self.adapter)

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()
        self.assertEqual([c for c in self.adapter.calls if c[0] in ('run', 'recover')], [])

    # helpers ---------------------------------------------------------------------------------------
    def run_(self, run_id, state, age=0.0, **extra):
        doc = dict(id=run_id, chat_id='c', state=state, operator=True, project_path=self.temp.name, error='',
                   capacity=2, worker_budget=2, fixes=0, main_worker='m',
                   created=self.now - age, updated=self.now - age)
        doc.update(extra)
        self.store.put('runs', doc)
        return doc

    def task(self, task_id, run_id, state, age=0.0, **extra):
        doc = dict(id=task_id, run_id=run_id, role='executor', title='Work ' + task_id, prompt='p', state=state,
                   result='', deps=[], files=[], attempts=0, worker_id='', lease='', error='',
                   created=self.now - age)
        doc.update(extra)
        self.store.put('tasks', doc)
        return doc

    def worker(self, worker_id, purpose='worker', lease='', state='ready'):
        self.store.put('workers', dict(id=worker_id, account_id='a', thread_id=worker_id, purpose=purpose,
                                       enabled=True, state=state, lease=lease, label=worker_id, last_used=0))

    def sweep(self, **kw):
        kw.setdefault('branch_exists', lambda project, branch: True)
        return stale_tasks.sweep(self.engine, now=self.now, **kw)

    def state(self, kind, ident):
        return self.store.get(kind, ident)['state']

    def cleanup_events(self):
        return [fields(e) for e in self.store.events(limit=5000) if e.get('kind') == 'auto_cleanup']

    # (a) + live example ------------------------------------------------------------------------------
    def test_live_example_conflict_tasks_in_done_runs_are_cancelled_and_runs_stay_done(self):
        pairs = (('t914d1f2', 'r4c50e0e'), ('t645508aa', 'ree80b0a2'), ('t22b5817', 'r82c79803'))
        for task_id, run_id in pairs:
            self.run_(run_id, 'done', age=50 * H, confirmed_at=self.now - 48 * H)
            self.task(task_id, run_id, 'queued', age=48 * H, kind='resolve_conflict',
                      title='Resolve conflict: task/' + task_id, patch_origin='t0' + task_id,
                      patch_branch='task/' + task_id, patch_worktree=str(Path(self.temp.name, 'wt', task_id)))
        taken = self.sweep()
        self.assertEqual(sorted(a['task_id'] for a in taken), sorted(t for t, _ in pairs))
        events = self.cleanup_events()
        for task_id, run_id in pairs:
            task = self.store.get('tasks', task_id)
            self.assertEqual(task['state'], 'cancelled')
            self.assertIn('run ' + run_id + ' is already done', task['error'])
            self.assertIn('Nothing was sent to Notion', task['error'])
            self.assertEqual(self.state('runs', run_id), 'done')  # a done run stays done
            match = [e for e in events if e.get('run_id') == run_id and e.get('task_id') == task_id]
            self.assertEqual(len(match), 1)
            self.assertTrue(match[0].get('reason'))
        self.assertEqual(self.sweep(), [])  # idempotent

    def test_a_queued_and_retry_tasks_of_finished_runs_are_cancelled_run_state_unchanged(self):
        self.run_('rd', 'done', age=3 * H)
        self.run_('rc', 'cancelled', age=3 * H)
        self.task('q1', 'rd', 'queued')
        self.task('q2', 'rd', 'retry', next_at=self.now + 60)
        self.task('q3', 'rc', 'queued')
        self.task('fin', 'rd', 'done')
        taken = self.sweep()
        self.assertEqual({a['rule'] for a in taken}, {'a'})
        for task_id in ('q1', 'q2', 'q3'):
            self.assertEqual(self.state('tasks', task_id), 'cancelled')
        self.assertEqual(self.state('tasks', 'fin'), 'done')
        self.assertEqual(self.state('runs', 'rd'), 'done')
        self.assertEqual(self.state('runs', 'rc'), 'cancelled')

    # (b) --------------------------------------------------------------------------------------------
    def test_b_resolve_conflict_task_without_worktree_or_branch_is_cancelled(self):
        self.run_('r1', 'executing')
        present = Path(self.temp.name, 'wt-present')
        present.mkdir()
        conflict = dict(kind='resolve_conflict', patch_origin='t0', patch_branch='task/b1')
        self.task('gone_wt', 'r1', 'queued', title='Resolve conflict: task/b1',
                  patch_worktree=str(Path(self.temp.name, 'wt-missing')), **conflict)
        self.task('keep', 'r1', 'queued', title='Resolve conflict: task/b1', patch_worktree=str(present), **conflict)
        self.sweep(branch_exists=lambda project, branch: True)
        self.assertEqual(self.state('tasks', 'gone_wt'), 'cancelled')
        self.assertIn('worktree', self.store.get('tasks', 'gone_wt')['error'])
        self.assertEqual(self.state('tasks', 'keep'), 'queued')
        # git cannot tell (None: no repository, git error): kept
        self.sweep(branch_exists=lambda project, branch: None)
        self.assertIsNone(stale_tasks.git_branch_exists(str(Path(self.temp.name, 'no-such-dir')), 'task/b1'))
        self.assertEqual(self.state('tasks', 'keep'), 'queued')
        self.sweep(branch_exists=lambda project, branch: False)
        self.assertEqual(self.state('tasks', 'keep'), 'cancelled')
        self.assertIn('branch task/b1', self.store.get('tasks', 'keep')['error'])
        self.assertEqual(self.state('runs', 'r1'), 'executing')
        rules = {e.get('task_id'): e.get('rule') for e in self.cleanup_events()}
        self.assertEqual(rules, dict(gone_wt='b', keep='b'))

    def test_b_blocked_conflict_task_of_finished_run_is_cancelled(self):
        self.run_('r1', 'done')
        self.task('b1', 'r1', 'blocked', title='Resolve conflict: task/x', patch_origin='t0', patch_branch='task/x')
        self.sweep()
        self.assertEqual(self.state('tasks', 'b1'), 'cancelled')
        self.assertEqual(self.state('runs', 'r1'), 'done')

    # (c) --------------------------------------------------------------------------------------------
    def test_c_old_queued_task_on_run_that_cannot_progress_is_cancelled(self):
        self.run_('rb', 'blocked')  # updated just now: not stale for rule (e)
        self.run_('rx', 'executing')
        self.task('old', 'rb', 'queued', age=7 * H)
        self.task('young', 'rb', 'queued', age=1 * H)
        self.task('busy_run', 'rx', 'queued', age=7 * H)
        self.task('orphan', 'gone', 'queued', age=7 * H)
        self.sweep()
        self.assertEqual(self.state('tasks', 'old'), 'cancelled')
        self.assertIn('cannot progress', self.store.get('tasks', 'old')['error'])
        self.assertEqual(self.state('tasks', 'orphan'), 'cancelled')
        self.assertEqual(self.state('tasks', 'young'), 'queued')
        self.assertEqual(self.state('tasks', 'busy_run'), 'queued')
        self.assertEqual(self.state('runs', 'rb'), 'blocked')

    def test_c_uses_setting_stale_queued_hours(self):
        self.store.put('settings', dict(id='app', stale_queued_hours=2))
        self.run_('rb', 'awaiting_main')
        self.task('q', 'rb', 'queued', age=3 * H)
        self.sweep()
        self.assertEqual(self.state('tasks', 'q'), 'cancelled')

    # (d) --------------------------------------------------------------------------------------------
    def uncertain(self, task_id, **extra):
        doc = dict(attempts=1, worker_id='w1', lease='L1', files=['a.py'], side_effect_hold=True,
                   reconcile_at=self.now + H, sent_at=self.now - 2 * H, remote_done_at=0,
                   error='Awaiting confirmation from Notion; request will not be resent')
        doc.update(extra)
        return self.task(task_id, 'r1', 'uncertain', age=3 * H, **doc)

    def test_d_remote_done_over_30_minutes_is_abandoned_via_resolve_uncertain(self):
        self.run_('r1', 'executing')
        self.uncertain('t1', remote_done_at=self.now - 2 * H)
        self.worker('w1', lease='L1', state='recovering')
        taken = self.sweep()
        self.assertEqual([(a['rule'], a['task_id']) for a in taken], [('d', 't1')])
        task = self.store.get('tasks', 't1')
        self.assertEqual(task['state'], 'failed')
        self.assertEqual(task['main_resolution'], 'abandon')
        self.assertIn('nothing was resent', task['error'])
        self.assertEqual(self.store.get('workers', 'w1')['lease'], '')
        self.assertEqual([e.get('task_id') for e in self.cleanup_events()], ['t1'])

    def test_d_recent_remote_done_is_left_alone(self):
        self.run_('r1', 'executing')
        self.uncertain('t1', remote_done_at=self.now - 10 * 60)
        self.worker('w1', lease='L1', state='recovering')
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.state('tasks', 't1'), 'uncertain')
        self.assertEqual(self.store.get('workers', 'w1')['lease'], 'L1')

    def test_d_unsent_proof_abandons(self):
        self.run_('r1', 'executing')
        task = self.uncertain('t1')
        self.worker('w1', lease='L1', state='recovering')
        latest = (task['sent_at'] - H) * 1000
        self.adapter.outcome = (latest, latest + 5000, 'done')
        taken = self.sweep()
        self.assertEqual([a['op'] for a in taken], ['proof'])
        task = self.store.get('tasks', 't1')
        self.assertEqual(task['state'], 'failed')
        self.assertEqual(task['unsent_proof']['sent_at'], task['sent_at'])
        self.assertEqual(self.store.get('workers', 'w1')['lease'], '')
        self.assertEqual(len(self.cleanup_events()), 1)

    def test_d_without_proof_is_left_alone_and_rechecked_rarely(self):
        self.run_('r1', 'executing')
        task = self.uncertain('t1')
        self.worker('w1', lease='L1', state='recovering')
        latest = (task['sent_at'] + 30) * 1000  # a turn after our send: the prompt may have arrived
        self.adapter.outcome = (latest, latest + 5000, 'done')
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.sweep(), [])  # same window: no second read
        self.assertEqual(self.state('tasks', 't1'), 'uncertain')
        self.assertEqual(self.store.get('workers', 'w1')['lease'], 'L1')
        self.assertEqual([c[0] for c in self.adapter.calls], ['turn_outcome'])
        self.assertEqual(self.cleanup_events(), [])

    # (e) --------------------------------------------------------------------------------------------
    def test_e_blocked_and_paused_runs_become_stale_and_release_everything(self):
        self.run_('rb', 'blocked', age=48 * H)
        self.run_('rp', 'paused', age=30 * H)
        self.task('failed1', 'rb', 'failed', age=48 * H, finished=self.now - 48 * H, worker_id='w1', lease='L1',
                  files=['a.py'])
        self.task('blocked1', 'rb', 'blocked', age=48 * H, files=['b.py'])
        self.task('retry1', 'rp', 'retry', age=30 * H, next_at=self.now - 30 * H)
        self.worker('w1', lease='L1', state='busy')
        self.sweep()
        for run_id, before in (('rb', 'blocked'), ('rp', 'paused')):
            run = self.store.get('runs', run_id)
            self.assertEqual(run['state'], 'cancelled')  # final state the server already treats as finished
            self.assertTrue(run['stale'])
            self.assertEqual(run['stale_from'], before)
            self.assertIn('no update for', run['stale_reason'])
            self.assertIn(run_id, [r['id'] for r in self.store.all('runs')])  # never deleted
        self.assertEqual(self.state('tasks', 'failed1'), 'failed')
        self.assertEqual(self.state('tasks', 'blocked1'), 'cancelled')
        self.assertEqual(self.state('tasks', 'retry1'), 'cancelled')
        self.assertEqual(self.store.get('workers', 'w1')['lease'], '')
        for task in self.store.all('tasks'):
            self.assertNotIn(task['state'], file_registry.ACTIVE)
            self.assertFalse(task.get('remote_unknown'))
        events = self.cleanup_events()
        self.assertTrue(any(e.get('run_id') == 'rb' and e.get('task_id') == '' and e.get('rule') == 'e' for e in events))
        self.assertTrue(any(e.get('run_id') == 'rb' and e.get('task_id') == 'blocked1' for e in events))
        self.assertTrue(all(e.get('reason') for e in events))
        # a stale run is final: queued leftovers would now fall under rule (a)
        self.task('late', 'rb', 'queued')
        self.sweep()
        self.assertEqual(self.state('tasks', 'late'), 'cancelled')
        self.assertEqual(self.state('runs', 'rb'), 'cancelled')

    def test_e_stale_run_is_finished_for_chat_list_and_live_stream(self):
        import io
        from types import SimpleNamespace
        from notiontool.server import Application

        class Handler:
            def __init__(self):
                self.headers = {}
                self.wfile = io.BytesIO()
                self.close_connection = False

            def send_response(self, code):
                self.code = code

            def send_header(self, key, value):
                pass

            def end_headers(self):
                pass

        self.worker('m', purpose='main')
        self.run_('rp', 'paused', age=48 * H)
        self.task('retry1', 'rp', 'retry', age=48 * H, next_at=self.now - 48 * H)
        app = SimpleNamespace(store=self.store, engine=self.engine)
        chat = [c for c in Application.chat_list(app) if c['id'] == 'c'][0]
        self.assertEqual(chat['active_run'], 'rp')  # a paused run counts as active before the sweep
        self.sweep()
        self.assertTrue(self.store.get('runs', 'rp')['stale'])
        chat = [c for c in Application.chat_list(app) if c['id'] == 'c'][0]
        self.assertEqual(chat['active_run'], '')  # the stale run is no longer the active run
        handler = Handler()
        started = time.time()
        Application.stream(app, handler, {'run_id': ['rp'], 'timeout': ['5']})
        self.assertLess(time.time() - started, 4)  # ended by run_finished, not by the 5 s timeout
        text = handler.wfile.getvalue().decode('utf-8')
        self.assertIn('event: run_finished', text)
        self.assertIn('"state": "cancelled"', text)
        self.assertIn('event: auto_cleanup', text)

    def test_e_run_with_live_task_or_recent_update_is_kept(self):
        self.run_('live', 'blocked', age=48 * H)
        self.task('u', 'live', 'uncertain', age=48 * H, sent_at=self.now - 60, worker_id='w1', lease='L1')
        self.worker('w1', lease='L1', state='recovering')
        self.run_('recent', 'paused', age=48 * H, updated=self.now - 2 * H)
        self.run_('event', 'blocked', age=48 * H)
        self.store.event('stage_changed', 'event', message='blocked')  # fresh activity in the history
        self.sweep()
        self.assertEqual(self.state('runs', 'live'), 'blocked')
        self.assertEqual(self.state('tasks', 'u'), 'uncertain')
        self.assertEqual(self.state('runs', 'recent'), 'paused')
        self.assertEqual(self.state('runs', 'event'), 'blocked')

    # guards -----------------------------------------------------------------------------------------
    def test_running_sending_and_main_or_operator_workers_are_never_touched(self):
        self.run_('rd', 'done', age=48 * H)
        self.task('run1', 'rd', 'running', age=48 * H, worker_id='w1', lease='L1')
        self.task('send1', 'rd', 'sending', age=48 * H)
        self.task('unk', 'rd', 'queued', age=48 * H, remote_unknown=True)
        self.run_('r1', 'executing')
        self.task('mainu', 'r1', 'uncertain', age=3 * H, worker_id='m', lease='LM', remote_done_at=self.now - 2 * H)
        self.run_('rb', 'blocked', age=48 * H)
        self.task('opfail', 'rb', 'failed', age=48 * H, finished=self.now - 48 * H, worker_id='op', lease='LO')
        self.worker('w1', lease='L1', state='busy')
        self.worker('m', purpose='main', lease='LM', state='recovering')
        self.worker('op', purpose='operator', lease='LO', state='busy')
        self.sweep()
        self.assertEqual(self.state('tasks', 'run1'), 'running')
        self.assertEqual(self.state('tasks', 'send1'), 'sending')
        self.assertEqual(self.state('tasks', 'unk'), 'queued')
        self.assertEqual(self.state('tasks', 'mainu'), 'uncertain')
        self.assertEqual(self.store.get('workers', 'w1')['lease'], 'L1')
        self.assertEqual(self.store.get('workers', 'm')['lease'], 'LM')
        self.assertEqual(self.store.get('workers', 'op')['lease'], 'LO')
        self.assertEqual(self.state('runs', 'rd'), 'done')
        self.assertEqual(self.state('runs', 'rb'), 'cancelled')  # the run is stale, the operator thread untouched
        self.assertTrue(self.store.get('runs', 'rb')['stale'])

    def test_inflight_task_is_skipped(self):
        self.run_('rd', 'done')
        self.task('q', 'rd', 'queued')
        self.engine.inflight[object()] = dict(task_id='q')
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.state('tasks', 'q'), 'queued')
        self.engine.inflight.clear()

    # wiring + dry run -------------------------------------------------------------------------------
    def test_engine_runs_the_sweep_periodically(self):
        self.run_('rd', 'done')
        self.task('q', 'rd', 'queued')
        clock = [1000.0]
        self.engine.clock = lambda: clock[0]
        self.assertIsNone(self.engine._auto_cleanup_maybe())  # first call only schedules (60 s delay)
        clock[0] += 30
        self.assertIsNone(self.engine._auto_cleanup_maybe())
        clock[0] += 31
        future = self.engine._auto_cleanup_maybe()
        self.assertIsNotNone(future)
        future.result(timeout=10)
        self.assertEqual(self.state('tasks', 'q'), 'cancelled')
        self.assertIsNone(self.engine._auto_cleanup_maybe())  # next sweep only after auto_cleanup_seconds
        clock[0] += stale_tasks.CLEANUP_INTERVAL + 1
        self.store.put('settings', dict(id='app', auto_cleanup=False))
        self.assertIsNone(self.engine._auto_cleanup_maybe())

    def test_coordinator_loop_calls_the_sweep(self):
        calls = []

        def fake():
            calls.append(1)
            self.engine.stop_event.set()
        self.engine._auto_cleanup_maybe = fake
        self.engine._loop()
        self.assertEqual(calls, [1])

    def test_dry_run_is_read_only(self):
        self.run_('rd', 'done')
        self.task('q', 'rd', 'queued')
        actions = stale_tasks.dry_run(Path(self.temp.name, 'notiontool.db'), now=self.now)
        self.assertEqual([(a['rule'], a['task_id']) for a in actions], [('a', 'q')])
        self.assertEqual(self.state('tasks', 'q'), 'queued')
        self.assertEqual(self.cleanup_events(), [])


if __name__ == '__main__':
    unittest.main()
