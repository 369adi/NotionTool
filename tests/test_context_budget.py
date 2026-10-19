import hashlib
import tempfile
import unittest
from pathlib import Path

from notiontool.chat_context import estimate_tokens
from notiontool.context_budget import artifact_block, build_context
from notiontool.store import Store


class ContextBudgetTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, ident, project_id, text):
        doc = dict(id=ident, text=text)
        if project_id is not None:
            doc['project_id'] = project_id
        return self.store.put('artifacts', doc)

    def ids(self, manifest):
        return [source['id'] for source in manifest['sources']]

    def test_only_artifacts_of_the_task_project(self):
        self.add('a1', 'p1', 'alpha')
        self.add('b1', 'p2', 'beta')
        self.add('c1', None, 'gamma')
        self.add('a2', 'p1', 'delta')
        text, manifest = build_context(self.store, {'id': 't', 'project_id': 'p1'}, 10000)
        self.assertEqual(self.ids(manifest), ['a1', 'a2'])
        self.assertNotIn('beta', text)
        self.assertNotIn('gamma', text)

    def test_stays_within_budget_and_stops_at_first_overflow(self):
        small = self.add('a1', 'p1', 'x' * 30)
        self.add('a2', 'p1', 'y' * 3000)
        later = self.add('a3', 'p1', 'z' * 30)
        budget = estimate_tokens(artifact_block(small)) + estimate_tokens(artifact_block(later))
        text, manifest = build_context(self.store, {'project_id': 'p1'}, budget)
        self.assertEqual(self.ids(manifest), ['a1'])
        self.assertLessEqual(manifest['total_tokens'], budget)
        self.assertNotIn('y', text)

    def test_manifest_fields_and_sha256(self):
        doc = self.add('a1', 'p1', 'alpha')
        text, manifest = build_context(self.store, {'project_id': 'p1'}, 1000)
        block = artifact_block(doc)
        self.assertEqual(set(manifest), {'sources', 'total_tokens', 'budget'})
        self.assertEqual(manifest['budget'], 1000)
        source = manifest['sources'][0]
        self.assertEqual(set(source), {'kind', 'id', 'sha256', 'tokens'})
        self.assertEqual(source['kind'], 'artifact')
        self.assertEqual(source['id'], 'a1')
        self.assertEqual(source['sha256'], hashlib.sha256(block.encode('utf-8')).hexdigest())
        self.assertEqual(text, block)

    def test_tokens_match_chat_context_estimate(self):
        first = self.add('a1', 'p1', 'Größe ✓ ' * 20)
        second = self.add('a2', 'p1', 'plain text ' * 40)
        text, manifest = build_context(self.store, {'project_id': 'p1'}, 100000)
        expected = [estimate_tokens(artifact_block(first)), estimate_tokens(artifact_block(second))]
        self.assertEqual([source['tokens'] for source in manifest['sources']], expected)
        self.assertEqual(manifest['total_tokens'], sum(expected))
        self.assertLessEqual(estimate_tokens(text), manifest['total_tokens'])

    def test_empty_budget_includes_nothing(self):
        self.add('a1', 'p1', 'alpha')
        for budget in (0, None, -5):
            text, manifest = build_context(self.store, {'project_id': 'p1'}, budget)
            self.assertEqual(text, '')
            self.assertEqual(manifest, {'sources': [], 'total_tokens': 0, 'budget': 0})

    def test_task_without_project_id_gets_no_artifacts(self):
        self.add('a1', 'p1', 'alpha')
        self.add('c1', None, 'gamma')
        for task in ({}, {'project_id': ''}, {'project_id': None}, None):
            text, manifest = build_context(self.store, task, 10000)
            self.assertEqual(text, '')
            self.assertEqual(manifest['sources'], [])
            self.assertEqual(manifest['total_tokens'], 0)

    def test_order_is_store_order(self):
        for ident in ('c', 'a', 'b'):
            self.add(ident, 'p1', ident * 5)
        text, manifest = build_context(self.store, {'project_id': 'p1'}, 10000)
        self.assertEqual(self.ids(manifest), ['c', 'a', 'b'])

    def test_not_wired_into_engine(self):
        engine = Path(__file__).resolve().parent.parent / 'notiontool' / 'engine.py'
        self.assertNotIn('context_budget', engine.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
