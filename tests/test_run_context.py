import tempfile
import time
import unittest
from pathlib import Path

from notiontool import plain_protocol
from notiontool.engine import Engine
from notiontool.store import Store

from test_engine import FakeAdapter


class RunContextTests(unittest.TestCase):
    """Remembered final answers reach the prompts once; independent findings stay."""

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

    def test_remembered_answer_is_quoted_once(self):
        self.earlier_answer('ORCA-12 final answer with its checks.')
        context = self.engine.submit('c', 'Next request')['context']
        self.assertEqual(context.count('ORCA-12'), 1)
        self.assertIn('Project memory:\nDurable finding: the vault holds the key.', context)

    def test_long_answer_prefix_is_quoted_once(self):
        answer = 'LONG-ANSWER ' + 'z' * 7000
        self.earlier_answer(answer, answer[:6000])
        context = self.engine.submit('c', 'Next request')['context']
        self.assertEqual(context.count('LONG-ANSWER'), 1)
        self.assertIn('Durable finding', context)

    def test_finding_that_only_quotes_part_of_an_answer_stays(self):
        finding = 'Finding: ORCA-12 final answer with its checks. Keep this sentence.'
        self.earlier_answer('ORCA-12 final answer with its checks.', finding)
        self.assertIn(finding, self.engine.submit('c', 'Next request')['context'])

    def test_fresh_chat_sends_no_empty_reference(self):
        run = self.engine.submit('c', 'First request')
        self.assertEqual(run['context'], '')
        task = dict(role='executor', prompt='Do part one', acceptance='part one done', files=[])
        self.assertNotIn('earlier work', plain_protocol.prompt(task, run, '', 'token', False))


if __name__ == '__main__':
    unittest.main()
