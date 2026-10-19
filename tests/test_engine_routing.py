"""Phase 4 change 1 and 6: engine.add_task uses task_router.decide for routed runs; _machine_check passes the task risk."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from notiontool import task_router, verifier
from notiontool.engine import Engine
from notiontool.store import Store


class IdleAdapter:
    def run(self, worker, task, prompt, cancel, update):
        raise AssertionError('No task may be sent in these tests')

    def recover(self, worker, task, cancel):
        raise AssertionError('No reconciliation in these tests')


class EngineRoutingTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.store.put('chats', dict(id='c', title='Main', main_worker='m', folder_id='f', project_path=self.temp.name))
        self.engine = Engine(self.store, IdleAdapter())

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def run_dict(self, routing):
        run = dict(id='r1', chat_id='c', state='executing', project_path=self.temp.name, error='', capacity=1,
                   fixes=0, main_worker='m', created=time.time(), updated=time.time(), model_routing=routing)
        self.store.put('runs', run)
        return run

    def test_routed_run_uses_router_model_and_reason(self):
        run = self.run_dict(True)
        task = self.engine.add_task(run, 'executor', 'p', task_class='test', risk='low')
        expected = task_router.decide('test', 'low')
        self.assertEqual(task['model'], expected['model'])
        self.assertEqual(task['model'], 'haiku')
        self.assertTrue(task['model_reason'].startswith('task_router: '))
        self.assertIn(expected['model_reason'], task['model_reason'])
        self.assertEqual(task['risk'], 'low')
        self.assertEqual(self.store.get('tasks', task['id'])['model'], 'haiku')

    def test_routed_run_default_class_and_risk(self):
        run = self.run_dict(True)
        task = self.engine.add_task(run, 'executor', 'p')
        self.assertEqual(task['model'], task_router.decide('', None)['model'])
        self.assertTrue(task['model_reason'].startswith('task_router: '))
        self.assertNotIn('risk', task)

    def test_explicit_model_is_kept(self):
        run = self.run_dict(True)
        with mock.patch.object(task_router, 'decide', side_effect=AssertionError('must not route')):
            task = self.engine.add_task(run, 'executor', 'p', model='opus', model_reason='Architect: hard task',
                                        task_class='routine', risk='low')
        self.assertEqual(task['model'], 'opus')
        self.assertEqual(task['model_reason'], 'Architect: hard task')

    def test_unrouted_run_unchanged(self):
        run = self.run_dict(False)
        with mock.patch.object(task_router, 'decide', side_effect=AssertionError('must not route')):
            task = self.engine.add_task(run, 'executor', 'p', task_class='routine', risk='low')
        self.assertEqual(task['model'], 'opus')
        self.assertFalse(task['model_reason'].startswith('task_router'))

    def test_non_executor_roles_not_routed(self):
        run = self.run_dict(True)
        with mock.patch.object(task_router, 'decide', side_effect=AssertionError('must not route')):
            task = self.engine.add_task(run, 'planner', 'p')
        self.assertEqual(task['model'], 'opus')

    def test_router_error_or_unusable_result_falls_back(self):
        run = self.run_dict(True)
        for effect in (dict(side_effect=RuntimeError('boom')), dict(return_value=None),
                       dict(return_value={'model': None, 'model_reason': 'x', 'exhausted': True}),
                       dict(return_value={'model': 'gpt', 'model_reason': 'x'}),
                       dict(return_value={'model': 'haiku', 'model_reason': ''})):
            with mock.patch.object(task_router, 'decide', **effect):
                task = self.engine.add_task(run, 'executor', 'p', task_class='routine', risk='low')
            self.assertEqual(task['model'], 'opus', effect)
            self.assertFalse(task['model_reason'].startswith('task_router'), effect)
            self.assertNotIn('route_state', task)

    def test_plan_import_keeps_explicit_models(self):
        run = self.run_dict(True)
        plan = {'tasks': [dict(id='a', title='A', prompt='p', acceptance='ok', model='sonnet', model_reason='Architect pick')]}
        with mock.patch.object(task_router, 'decide', side_effect=AssertionError('must not route')):
            self.engine._import_plan(run, plan)
        [task] = self.engine.tasks('r1')
        self.assertEqual((task['model'], task['model_reason']), ('sonnet', 'Architect pick'))

    def test_risk_passed_to_verifier(self):
        run = self.run_dict(False)
        Path(self.temp.name, 'a.py').write_text('x = 1\n', encoding='utf-8')
        real = verifier.run_verifier
        seen = []

        def spy(task_id, criteria, checks, risk, *args, **kwargs):
            seen.append(risk)
            return real(task_id, criteria, checks, risk, *args, **kwargs)

        with mock.patch.object(verifier, 'run_verifier', side_effect=spy):
            for risk in ('medium', 'low', 'high', 'bogus', None):
                task = dict(id='t' + str(risk), files=['a.py'], risk=risk)
                self.assertEqual(self.engine._machine_check(run, task), '')
            self.assertEqual(self.engine._machine_check(run, dict(id='tn', files=['a.py'])), '')
        # high stays 'low': the engine has no model_review callable (verifier.py:153-159).
        self.assertEqual(seen, ['medium', 'low', 'low', 'low', 'low', 'low'])


if __name__ == '__main__':
    unittest.main()
