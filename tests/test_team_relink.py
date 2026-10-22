import unittest

from notiontool.team_rotation import TeamRotator


class Store:
    def __init__(self, account, workers):
        self.objects = {('accounts', account['id']): account}
        for w in workers:
            self.objects[('workers', w['id'])] = w
        self.events = []

    def get(self, kind, ident, default=None):
        return self.objects.get((kind, ident), default)

    def all(self, kind):
        return [v for (k, _), v in self.objects.items() if k == kind]

    def put(self, kind, doc):
        self.objects[(kind, doc['id'])] = doc

    def event(self, kind, *args, **fields):
        self.events.append(kind)


def worker(ident, thread, created, **extra):
    return dict(dict(id=ident, account_id='a', thread_id=thread, purpose='worker', state='ready', enabled=True,
                     created_at=created), **extra)


class RelinkTest(unittest.TestCase):
    def test_unknown_roles_track_loose_coworkers_oldest_first(self):
        account = dict(id='a', team_threads={
            'worker1': dict(id='gone1', pending=False),
            'worker2': dict(id='t2', pending=False),
            'worker3': dict(id='gone3', pending=True),
            'worker10': dict(id='gone10', pending=False)})
        store = Store(account, [worker('w2', 't2', 50), worker('w13', 't13', 10), worker('w14', 't14', 20),
                                worker('w15', 't15', 30)])
        rotator = TeamRotator(store, None, None)
        self.assertEqual(rotator.relink('a'), 2)
        ledger = store.get('accounts', 'a')['team_threads']
        self.assertEqual(ledger['worker1']['id'], 't13')
        self.assertEqual(ledger['worker10']['id'], 't14')
        self.assertEqual(ledger['worker2']['id'], 't2')
        self.assertEqual(ledger['worker3'], dict(id='gone3', pending=True))
        self.assertEqual(store.events.count('team_role_relinked'), 2)

    def test_nothing_to_relink(self):
        account = dict(id='a', team_threads={'worker1': dict(id='t1', pending=False)})
        store = Store(account, [worker('w1', 't1', 5)])
        self.assertEqual(TeamRotator(store, None, None).relink('a'), 0)
        self.assertEqual(store.events, [])


if __name__ == '__main__':
    unittest.main()
