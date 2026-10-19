import unittest
from notiontool.model_routing import (MODELS, DEFAULT_MODEL, choice, validate_models,
                                      routing_reason, with_reason)
from notiontool.protocol import ContractError
from tests import test_engine as fixtures

ROLES = ('planner', 'reviewer', 'integrator', 'executor')


def selected(task, purpose='worker'):
    # Mirrors notiontool/notion.py:805, the only place that picks the model for a turn.
    return 'opus' if purpose == 'main' else task.get('model', 'opus')


def engine_task(role, model='opus', model_reason=''):
    # Mirrors the add_task defaults in notiontool/engine.py:117-122.
    return {'role': role, 'model': model, 'model_reason': model_reason}


class ModelRoutingReasonTest(unittest.TestCase):
    def test_every_role_gets_a_non_empty_reason(self):
        for role in ROLES:
            with self.subTest(role=role):
                reason = with_reason(engine_task(role))['model_reason']
                self.assertTrue(reason.strip())
                self.assertIn(role, reason)
                self.assertIn('Opus 5.5', reason)

    def test_engine_roles_are_default_and_executor_without_choice_is_fallback(self):
        for role in ('planner', 'reviewer', 'integrator'):
            with self.subTest(role=role):
                reason = with_reason(engine_task(role))['model_reason']
                self.assertIn('is the default model', reason)
                self.assertNotIn('fallback', reason)
        self.assertIn('is the fallback model', with_reason({'role': 'executor'})['model_reason'])
        self.assertIn('is the fallback model', with_reason({})['model_reason'])

    def test_reason_names_task_class_or_risk(self):
        self.assertEqual(routing_reason('planner'), 'planner (planning): Opus 5.5 is the default model')
        reason = with_reason({'role': 'executor', 'model': 'haiku', 'task_class': 'docs'})['model_reason']
        self.assertEqual(reason, 'executor (docs): Haiku 5.5 is the fallback model')
        reason = with_reason({'role': 'reviewer', 'model': 'opus', 'risk': 'high'})['model_reason']
        self.assertEqual(reason, 'reviewer (risk high): Opus 5.5 is the default model')

    def test_existing_architect_reason_is_kept(self):
        task = {'role': 'executor', 'model': 'sonnet', 'model_reason': 'Moderate coding'}
        self.assertEqual(with_reason(dict(task)), task)

    def test_chosen_model_is_identical_before_and_after(self):
        cases = [engine_task(role) for role in ROLES] + [
            {'role': 'executor'}, {'role': 'executor', 'model': 'haiku'},
            {'role': 'executor', 'model': 'sonnet', 'model_reason': ''}, {}]
        for task in cases:
            with self.subTest(task=task):
                before = dict(task)
                after = with_reason(dict(task))
                self.assertEqual('model' in before, 'model' in after)
                self.assertEqual(before.get('model'), after.get('model'))
                for purpose in ('worker', 'main'):
                    self.assertEqual(selected(before, purpose), selected(after, purpose))

    def test_choice_and_validation_are_unchanged(self):
        self.assertEqual(DEFAULT_MODEL, 'opus')
        self.assertEqual({key: choice(label) for key, label in MODELS.items()}, {key: key for key in MODELS})
        tasks = validate_models({'tasks': [{'model': 'Sonnet 5.5', 'model_reason': 'Moderate coding'}]})
        self.assertEqual(tasks[0]['model'], 'sonnet')
        with self.assertRaises(ContractError):
            validate_models({'tasks': [{'model': 'Opus 5.5', 'model_reason': ''}]})
        with self.assertRaises(ContractError):
            choice('Auto')


class EngineModelReasonTest(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp
    tearDown = fixtures.EngineTests.tearDown
    worker = fixtures.EngineTests.worker

    def test_engine_tasks_of_all_roles_store_a_reason_and_keep_the_model(self):
        run = dict(id='r-reason', chat_id='c')
        cases = [('planner', {}, 'opus', 'default'), ('reviewer', {}, 'opus', 'default'),
                 ('integrator', {}, 'opus', 'default'), ('executor', {}, 'opus', 'rule'),
                 ('executor', {'model': 'haiku'}, 'haiku', 'fallback'),
                 ('executor', {'model': 'sonnet', 'model_reason': 'Moderate coding'}, 'sonnet', None)]
        for role, extra, model, source in cases:
            with self.subTest(role=role, extra=extra):
                task = self.engine.add_task(run, role, 'Do the part', **extra)
                saved = self.store.get('tasks', task['id'])
                self.assertEqual(saved['model'], model)
                self.assertTrue(saved['model_reason'].strip())
                if source:
                    self.assertIn(role, saved['model_reason'])
                    if source == 'rule':  # unrouted executor: reason names the real task_router rule
                        self.assertIn('model rule:', saved['model_reason'])
                    else:
                        self.assertIn(f'is the {source} model', saved['model_reason'])
                else:
                    self.assertEqual(saved['model_reason'], 'Moderate coding')


if __name__ == '__main__':
    unittest.main()
