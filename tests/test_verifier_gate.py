"""P6-2: local machine check before the Main confirmation stage (Engine._machine_check / _main_decisions)."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from notiontool.engine import Engine
from notiontool.store import Store


class NoSendAdapter:
    def __init__(self):
        self.runs = []

    def run(self, worker, task, prompt, cancel, update):
        self.runs.append(task['id'])
        raise AssertionError('A machine check must never resend work')

    def recover(self, worker, task, cancel):
        raise AssertionError('Nothing to recover')


class VerifierGateTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.project = Path(self.temp.name).resolve() / 'project'
        (self.project / 'src').mkdir(parents=True)
        (self.project / 'src' / 'a.py').write_text('x = 1\n')
        (self.project / 'README.md').write_text('readme\n')
        self.store = Store(str(Path(self.temp.name) / 'data'))
        self.adapter = NoSendAdapter()
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        self.store.put('workers', dict(id='first', account_id='a', thread_id='first', purpose='worker', enabled=True,
                                       state='ready', lease='', label='first', last_used=0))
        self.store.put('chats', dict(id='c', title='Main', main_worker='first', folder_id='f', project_path=str(self.project)))
        self.engine = Engine(self.store, self.adapter)

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def finish(self, *file_lists, operator=True):
        self.store.put('runs', dict(id='r1', chat_id='c', state='executing', operator=operator,
                                    project_path=str(self.project), error='', capacity=2, worker_budget=2, fixes=0,
                                    main_worker='first', created=time.time(), updated=time.time()))
        for index, files in enumerate(file_lists, 1):
            self.store.put('tasks', dict(id='t%d' % index, run_id='r1', role='executor', title='Task %d' % index,
                                         prompt='p', state='done', result='Done.\nRESULT: DONE', deps=[],
                                         files=list(files), attempts=1))
        self.engine._advance(self.store.get('runs', 'r1'))
        return self.store.get('runs', 'r1')

    def events(self, kind):
        return [e for e in self.store.events(limit=500) if e['kind'] == kind and e.get('run_id') == 'r1']

    def test_missing_declared_file_waits_for_main_with_reason(self):
        run = self.finish(['src/a.py', 'src/missing.py'], ['README.md'])
        self.assertEqual((run['state'], run['needs_main']), ('awaiting_main', ['t1']))
        self.assertIn('missing: src/missing.py', run['needs_main_reasons']['t1'])
        self.assertIn('Machine check failed', run['machine_check_failed']['t1'])
        failed = self.events('machine_check_failed')
        self.assertEqual([e.get('task_id') for e in failed], ['t1'])
        self.assertIn('src/missing.py', failed[0]['message'])

    def test_all_files_present_finishes_done_at_once(self):
        run = self.finish(['src/a.py', './src/../README.md'], ['src'])
        self.assertEqual(run['state'], 'done')
        self.assertEqual((run['needs_main_reasons'], run['machine_check_failed']), ({}, {}))
        self.assertEqual(self.events('machine_check_failed'), [])
        self.assertEqual(self.events('awaiting_main'), [])

    def test_path_outside_project_fails(self):
        outside = Path(self.temp.name).resolve() / 'outside.txt'
        outside.write_text('x\n')
        run = self.finish([str(outside)], ['../outside.txt'])
        self.assertEqual((run['state'], sorted(run['needs_main'])), ('awaiting_main', ['t1', 't2']))
        for ident in ('t1', 't2'):
            self.assertIn('outside the project folder', run['needs_main_reasons'][ident])

    def test_no_declared_files_is_unaffected(self):
        self.assertEqual(self.finish([], []).get('state'), 'done')

    def test_non_operator_run_is_checked_too(self):
        self.store.put('runs', dict(id='r1', chat_id='c', state='integrating', project_path=str(self.project), error='',
                                    capacity=1, worker_budget=1, fixes=0, main_worker='first'))
        self.store.put('tasks', dict(id='t1', run_id='r1', role='executor', state='done', result='{}', files=['gone.txt'],
                                     deps=[], attempts=1))
        self.store.put('tasks', dict(id='t2', run_id='r1', role='integrator', state='done', files=[], deps=[], attempts=1,
                                     result=json.dumps(dict(summary='ok', context=''))))
        self.engine._advance(self.store.get('runs', 'r1'))
        run = self.store.get('runs', 'r1')
        self.assertEqual((run['state'], run['needs_main']), ('awaiting_main', ['t1']))

    def test_failed_check_never_changes_tasks_or_resends(self):
        self.finish(['src/missing.py'])
        before = self.store.get('tasks', 't1')
        for _ in range(3):
            self.engine.tick()
        after = self.store.get('tasks', 't1')
        self.assertEqual((after['state'], after['attempts']), ('done', 1))
        self.assertEqual(after, before)
        self.assertEqual(self.adapter.runs, [])
        self.assertEqual(self.store.get('runs', 'r1')['state'], 'awaiting_main')
        self.assertEqual(len(self.events('machine_check_failed')), 1)
        self.assertEqual(self.engine.confirm_run('r1')['state'], 'done')


if __name__ == '__main__':
    unittest.main()
