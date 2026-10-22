import unittest
from notiontool.team_grow import TeamGrower


class FakeStore:
    def __init__(self, workers):
        self.workers = workers
        self.events = []

    def all(self, kind):
        return list(self.workers) if kind == 'workers' else []

    def event(self, kind, **kw):
        self.events.append((kind, kw))


class FakeAdapter:
    def __init__(self):
        self.renames = []

    def client(self, ident):
        return 'client-' + ident

    def rename_thread(self, client, tid, title):
        self.renames.append((client, tid, title))


class RestoreNameTest(unittest.TestCase):
    def grower(self, workers):
        g = TeamGrower.__new__(TeamGrower)
        g.store = FakeStore(workers)
        g.adapter = FakeAdapter()
        return g

    def test_named_coworker_gets_name_back(self):
        g = self.grower([dict(account_id='a', thread_id='t1', harness_title='Coworker 5')])
        self.assertTrue(g._restore_name('a', 't1'))
        self.assertEqual(g.adapter.renames, [('client-a', 't1', 'Coworker 5')])

    def test_unnamed_chat_untouched(self):
        g = self.grower([dict(account_id='a', thread_id='t1')])
        self.assertFalse(g._restore_name('a', 't1'))
        self.assertEqual(g.adapter.renames, [])

    def test_other_account_untouched(self):
        g = self.grower([dict(account_id='b', thread_id='t1', harness_title='Coworker 5')])
        self.assertFalse(g._restore_name('a', 't1'))
        self.assertEqual(g.adapter.renames, [])


if __name__ == '__main__':
    unittest.main()
