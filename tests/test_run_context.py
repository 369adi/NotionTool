import tempfile
import time
import unittest
from pathlib import Path

from notiontool import plain_protocol
from notiontool.engine import Engine
from notiontool.store import Store

from test_engine import FakeAdapter


class RunContextTests(unittest.TestCase):
    """Phase 5b isolation: a new run never carries earlier answers or project memory in run.context.

    Context lives on the operator's PC (handoff files, project memory); engine.submit sets
    run['context'] = '' so nothing from earlier runs reaches the task prompts.
    """

    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(self.temp.name)
        self.adapter = FakeAdapter()
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        for ident, purpose in (('main', 'main'), ('w0', 'worker'), ('w1', 'worker')):
            self.store.put('workers', dict(id=ident, account_id='a', thread_id=ident, purpose=purpose, enabled=True,
                                           state='ready', lease='', label=ident, last_used=0))
        self.store.put('chats', dict(id='c', title='Main', main_worker='main', folder_id='f', project_path=''))
        self.engine = Engine(self.store, self.adapter)

    def tearDown(self):
        self.adapter.gate.set()
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def earlier_answer(self, answer, remembered=None):
        self.store.put('runs', dict(id='rdone', chat_id='c', state='done', goal='Earlier request', summary=answer))
        self.store.message('c', 'user', 'Earlier request', 'rdone')
        self.store.message('c', 'assistant', answer, 'rdone')
        self.store.remember('f', 'Durable finding: the vault holds the key.', 't1')
        self.store.remember('f', answer if remembered is None else remembered, 'rdone')

    def next_run(self, *markers):
        """Submit the next request and check that no marker reaches run.context or a task prompt."""
        run = self.engine.submit('c', 'Next request')
        self.assertEqual(run['context'], '')
        task = dict(role='executor', prompt='Do part one', acceptance='part one done', files=[])
        prompt = plain_protocol.prompt(task, run, '', 'token', False)
        for marker in markers + ('Project memory:', 'Durable finding', 'earlier work'):
            self.assertNotIn(marker, run['context'])
            self.assertNotIn(marker, prompt)
        return run

    def test_remembered_answer_is_not_injected(self):
        self.earlier_answer('ORCA-12 final answer with its checks.')
        self.next_run('ORCA-12')

    def test_long_answer_is_not_injected(self):
        answer = 'LONG-ANSWER ' + 'z' * 7000
        self.earlier_answer(answer, answer[:6000])
        self.next_run('LONG-ANSWER')

    def test_finding_stays_in_project_memory_only(self):
        finding = 'Finding: ORCA-12 final answer with its checks. Keep this sentence.'
        self.earlier_answer('ORCA-12 final answer with its checks.', finding)
        self.next_run(finding, 'ORCA-12')
        # The finding is kept on the PC side (project memory), not dropped.
        self.assertIn(finding, self.store.get('memory', 'f', {}).get('text', ''))

    def test_fresh_chat_sends_no_empty_reference(self):
        run = self.engine.submit('c', 'First request')
        self.assertEqual(run['context'], '')
        task = dict(role='executor', prompt='Do part one', acceptance='part one done', files=[])
        self.assertNotIn('earlier work', plain_protocol.prompt(task, run, '', 'token', False))


if __name__ == '__main__':
    unittest.main()
