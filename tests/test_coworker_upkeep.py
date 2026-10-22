import threading
import unittest

from notiontool import coworker_upkeep as cu

ACC = 'acc'


def w(ident, state='ready', **kw):
    d = dict(id=ident, account_id=ACC, thread_id=ident, purpose='worker', state=state, enabled=state == 'ready',
             welcome=True, lease='')
    d.update(kw)
    return d


class PlanTests(unittest.TestCase):
    def test_first_sighting_only_marks(self):
        d = cu.plan([w('a', 'missing')], [], ACC, 1, now=1000)
        self.assertEqual(d['mark'], ['a'])
        self.assertEqual(d['retire'], [])

    def test_retire_after_grace_and_requeue_queued_tasks(self):
        workers = [w('a', 'missing', missing_since=1000)]
        tasks = [dict(id='t1', worker_id='a', state='queued'), dict(id='t2', worker_id='a', state='done')]
        d = cu.plan(workers, tasks, ACC, 1, now=1000 + cu.MISSING_GRACE)
        self.assertEqual(d['retire'], ['a'])
        self.assertEqual(d['requeue'], ['t1'])
        self.assertEqual(d['create'], 1)

    def test_busy_or_leased_missing_worker_is_kept(self):
        workers = [w('a', 'missing', missing_since=1), w('b', 'missing', missing_since=1, lease='x')]
        tasks = [dict(id='t', worker_id='a', state='running')]
        d = cu.plan(workers, tasks, ACC, 2, now=10 ** 6)
        self.assertEqual(d['retire'], [])

    def test_users_own_chats_are_never_retired_or_counted(self):
        own = w('own', 'missing', welcome=False, enabled=False)
        d = cu.plan([own, w('c1')], [], ACC, 1, now=10 ** 6)
        self.assertEqual((d['mark'], d['retire'], d['alive'], d['create']), ([], [], 1, 0))

    def test_refill_is_capped_per_cycle(self):
        d = cu.plan([w('c1')], [], ACC, 10, now=1)
        self.assertEqual(d['create'], cu.MAX_NEW_PER_CYCLE)

    def test_other_accounts_and_operator_ignored(self):
        d = cu.plan([w('x', 'missing', account_id='other', missing_since=1), w('m', 'missing', purpose='operator',
                     missing_since=1)], [], ACC, 0, now=10 ** 6)
        self.assertEqual(d['retire'], [])


class Store:
    def __init__(self, workers, tasks=()):
        self.data = {'workers': {x['id']: x for x in workers}, 'tasks': {t['id']: t for t in tasks},
                     'accounts': {ACC: dict(id=ACC, state='ready')}}
        self.events = []

    def settings(self):
        return {'harness_account_id': ACC, 'coworker_target': 2}

    def all(self, kind):
        return [dict(v) for v in self.data[kind].values()]

    def get(self, kind, ident, default=None):
        v = self.data[kind].get(ident)
        return dict(v) if v else default

    def put(self, kind, doc):
        self.data[kind][doc['id']] = dict(doc)

    def event(self, kind, **kw):
        self.events.append(kind)


class Adapter:
    def __init__(self, threads=3):
        self.threads, self.provisioned = threads, []

    def refresh(self, account_id):
        return {'threads': self.threads}

    def provision(self, account_id, count):
        self.provisioned.append(count)
        return {'created': count}


class Engine:
    lock = threading.RLock()
    stop_event = threading.Event()


class OnceTests(unittest.TestCase):
    def test_deleted_coworker_is_retired_and_replaced(self):
        store = Store([w('a', 'missing'), w('b')], [dict(id='t', worker_id='a', state='queued')])
        clock = [1000.0]
        up = cu.CoworkerUpkeep(store, Adapter(), Engine(), clock=lambda: clock[0])
        up.once()  # marks only, but refills the one missing slot
        self.assertEqual(store.data['workers']['a']['state'], 'missing')
        clock[0] += cu.MISSING_GRACE
        up.once()
        self.assertEqual(store.data['workers']['a']['state'], 'retired')
        self.assertEqual(store.data['tasks']['t']['worker_id'], '')
        self.assertIn('coworker_retired', store.events)

    def test_empty_listing_changes_nothing(self):
        store = Store([w('a', 'missing', missing_since=1)])
        adapter = Adapter(threads=0)
        self.assertIsNone(cu.CoworkerUpkeep(store, adapter, Engine(), clock=lambda: 10 ** 6).once())
        self.assertEqual(store.data['workers']['a']['state'], 'missing')
        self.assertEqual(adapter.provisioned, [])

    def test_failed_refill_backs_off(self):
        store = Store([])
        adapter = Adapter()
        adapter.provision = lambda a, c: (_ for _ in ()).throw(RuntimeError('net'))
        up = cu.CoworkerUpkeep(store, adapter, Engine(), clock=lambda: 100.0)
        up.once()
        self.assertIn('coworker_refill_failed', store.events)
        self.assertGreater(up.blocked_until, 100.0)


if __name__ == '__main__':
    unittest.main()
