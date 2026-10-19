import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from notiontool.store import Store
from notiontool.notion import NotionAdapter


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(Path(self.temp.name))
        self.adapter = NotionAdapter(self.store)
        self.config = {'model': 'auto-model', 'modelFromUser': False, 'oracleThreadType': 'onboarding'}
        self.client = Mock(info={'space_id': 'space'})
        self.accept = True
        self.calls = []
        self.client.call.side_effect = self.call

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def call(self, endpoint, body):
        self.calls.append(endpoint)
        if endpoint == 'saveTransactionsFanout':
            if self.accept:
                self.config.update(body['transactions'][0]['operations'][0]['args'])
            return {}
        table = body['requests'][0]['pointer']['table']
        if table == 'thread':
            return {'recordMap': {'thread': {'thread': {'space_id': 'space', 'type': 'workflow', 'messages': ['config']}}}}
        return {'recordMap': {'thread_message': {'config': {
            'parent_id': 'thread', 'step': {'type': 'config', 'value': copy.deepcopy(self.config) | {'type': 'workflow'}}}}}}

    def test_persisted_auto_is_corrected_and_verified_without_inference(self):
        result = self.adapter.ensure_thread_model(self.client, 'thread', 'opus')
        self.assertEqual(result['corrected'], 1)
        self.assertEqual(self.config['model'], 'opus')
        self.assertIs(self.config['modelFromUser'], True)
        self.assertEqual(self.config['oracleThreadType'], 'onboarding')
        self.assertNotIn('runInferenceTranscript', self.calls)
        self.calls.clear()
        self.assertEqual(self.adapter.ensure_thread_model(self.client, 'thread', 'opus')['corrected'], 0)
        self.assertNotIn('saveTransactionsFanout', self.calls)

    def test_successful_http_without_persisted_change_is_rejected(self):
        self.accept = False
        with self.assertRaisesRegex(ValueError, 'did not confirm'):
            self.adapter.ensure_thread_model(self.client, 'thread', 'opus')

    def test_foreign_thread_is_rejected_before_model_change(self):
        self.client.call.side_effect = lambda *args: {'recordMap': {'thread': {'thread': {
            'space_id': 'other', 'type': 'workflow', 'messages': ['config']}}}}
        with self.assertRaisesRegex(ValueError, 'owned workflow'):
            self.adapter.ensure_thread_model(self.client, 'thread', 'opus')
        self.assertEqual(self.client.call.call_count, 1)

    def test_same_chat_switches_models_per_assignment_without_inheriting_previous_choice(self):
        for model in ('haiku-id', 'sonnet-id', 'opus-id'):
            self.adapter.ensure_thread_model(self.client, 'thread', model)
            self.assertEqual(self.config['model'], model)
            self.assertIs(self.config['modelFromUser'], True)

    def test_model_contract_rejects_auto_missing_choices_and_non_opus_main(self):
        from notiontool.model_routing import validate_models
        from notiontool.protocol import ContractError
        for task in ({'model':'Auto','model_reason':'cheap'}, {'model_reason':'cheap'},
                     {'model':'Haiku 5.5','model_reason':'cheap','use_main':True}):
            with self.subTest(task=task), self.assertRaises(ContractError):
                validate_models({'tasks':[task]})
        tasks=validate_models({'tasks':[{'model':'Sonnet 5.5','model_reason':'Moderate coding'}]})
        self.assertEqual(tasks[0]['model'],'sonnet')
