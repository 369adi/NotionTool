"""Three-stage acceptance: machine check -> Main -> done (Engine._finish / Engine.confirm_run)."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from notiontool.engine import Engine
from notiontool.store import Store


class RecordingAdapter:
    def __init__(self):
        self.runs = []

    def run(self, worker, task, prompt, cancel, update):
        self.runs.append((worker['id'], task['id']))
        raise AssertionError('A run awaiting Main confirmation must dispatch nothing')

    def recover(self, worker, task, cancel):
        raise AssertionError('Nothing to recover in these tests')


class AcceptanceGateTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.adapter = RecordingAdapter()
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        for ident in ('first', 'second'):
            self.store.put('workers', dict(id=ident, account_id='a', thread_id=ident, purpose='worker', enabled=True,
                                           state='ready', lease='', label=ident, last_used=0))
        self.store.put('chats', dict(id='c', title='Main', main_worker='first', folder_id='f', project_path=self.temp.name))
        self.engine = Engine(self.store, self.adapter)

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def run_with(self, results, state='executing', operator=True, **task_extra):
        self.store.put('runs', dict(id='r1', chat_id='c', state=state, operator=operator, project_path=self.temp.name,
                                    error='', capacity=2, worker_budget=2, fixes=0, main_worker='first',
                                    created=time.time(), updated=time.time()))
        for index, result in enumerate(results, 1):
            task = dict(id='t%d' % index, run_id='r1', role='executor', title='Task %d' % index, prompt='p',
                        state='done', result=result, deps=[], files=[], attempts=1)
            task.update(task_extra.get('t%d' % index, {}))
            self.store.put('tasks', task)
        return self.store.get('runs', 'r1')

    def advance(self):
        self.engine._advance(self.store.get('runs', 'r1'))
        return self.store.get('runs', 'r1')

    def kinds(self):
        return [e['kind'] for e in self.store.events(limit=500) if e.get('run_id') == 'r1']

    def waiting_run(self):
        self.run_with(['Fixed.\nRESULT: DONE', json.dumps(dict(status='done', summary='No access.\nRESULT: NOT DONE: no access'))])
        return self.advance()

    def test_passing_run_goes_straight_to_done(self):
        self.run_with(['Fixed.\nRESULT: DONE', 'result: done'])
        run = self.advance()
        self.assertEqual(run['state'], 'done')
        self.assertNotIn('needs_main', run)
        self.assertNotIn('awaiting_main', self.kinds())

    def test_not_done_task_waits_for_main(self):
        run = self.waiting_run()
        self.assertEqual(run['state'], 'awaiting_main')
        self.assertEqual(run['needs_main'], ['t2'])
        self.assertEqual(run['unaccepted'], ['t2'])
        self.assertIn('awaiting_main', self.kinds())

    def test_requires_review_waits_for_main(self):
        integrator = dict(role='integrator', result=json.dumps(dict(summary='All done', context='')))
        self.run_with(['{}', ''], state='integrating', operator=False, t1=dict(requires_review=True), t2=integrator)
        run = self.advance()
        self.assertEqual((run['state'], run['needs_main']), ('awaiting_main', ['t1']))

    def test_non_operator_run_without_review_is_done(self):
        integrator = dict(role='integrator', result=json.dumps(dict(summary='All done', context='')))
        self.run_with(['{}', ''], state='integrating', operator=False, t2=integrator)
        self.assertEqual(self.advance()['state'], 'done')

    def test_confirm_run_moves_to_done_with_event(self):
        self.waiting_run()
        result = self.engine.confirm_run('r1')
        run = self.store.get('runs', 'r1')
        self.assertEqual((run['state'], result['state']), ('done', 'done'))
        self.assertGreater(run['confirmed_at'], 0)
        self.assertEqual(self.kinds().count('run_confirmed'), 1)

    def test_confirm_run_refuses_other_states(self):
        for state in ('done', 'executing'):
            self.run_with(['RESULT: DONE'], state=state)
            with self.assertRaisesRegex(ValueError, 'awaiting Main confirmation'):
                self.engine.confirm_run('r1')
            self.assertEqual(self.store.get('runs', 'r1')['state'], state)
        with self.assertRaisesRegex(ValueError, 'Run not found'):
            self.engine.confirm_run('nope')
        self.assertNotIn('run_confirmed', self.kinds())

    def test_restore_keeps_awaiting_main_and_dispatches_nothing(self):
        self.waiting_run()
        self.store.put('tasks', dict(id='t9', run_id='r1', role='executor', title='Late', prompt='p', state='queued',
                                     result='', deps=[], files=[], attempts=0, next_at=0))
        self.engine.close()
        self.engine = Engine(self.store, self.adapter)  # simulated restart runs _restore
        for _ in range(3):
            self.engine.tick()
        time.sleep(.05)
        self.assertEqual(self.store.get('runs', 'r1')['state'], 'awaiting_main')
        self.assertEqual(self.store.get('tasks', 't9')['state'], 'queued')
        self.assertEqual(self.adapter.runs, [])

    def test_cancel_works_while_awaiting_main(self):
        self.waiting_run()
        self.engine.cancel('r1')
        self.assertEqual(self.store.get('runs', 'r1')['state'], 'cancelled')
        with self.assertRaises(ValueError):
            self.engine.confirm_run('r1')


if __name__ == '__main__':
    unittest.main()
