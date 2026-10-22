import unittest

from notiontool.account_failover import rehome_runs


class FakeStore:
    def __init__(self, **tables):
        self.t = {k: {r['id']: dict(r) for r in v} for k, v in tables.items()}
        self.events = []

    def all(self, table):
        return [dict(r) for r in self.t.get(table, {}).values()]

    def get(self, table, ident, default=None):
        r = self.t.get(table, {}).get(ident)
        return dict(r) if r else default

    def put(self, table, row):
        self.t.setdefault(table, {})[row['id']] = dict(row)

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, run_id, fields))

    def settings(self):
        return {'harness_account_id': 'good'}


def store(dead_state='ai_disabled'):
    return FakeStore(
        accounts=[{'id': 'dead', 'state': dead_state}, {'id': 'good', 'state': 'ready'}],
        workers=[{'id': 'm_dead', 'account_id': 'dead', 'purpose': 'main', 'state': 'ready', 'enabled': True},
                 {'id': 'c_dead', 'account_id': 'dead', 'purpose': 'worker', 'state': 'ready', 'enabled': True},
                 {'id': 'm_good', 'account_id': 'good', 'purpose': 'main', 'state': 'ready', 'enabled': True}],
        runs=[{'id': 'r1', 'state': 'blocked', 'main_worker': 'm_dead', 'account_id': 'dead', 'plan': [1]},
              {'id': 'r2', 'state': 'done', 'main_worker': 'm_dead'}],
        tasks=[{'id': 't1', 'run_id': 'r1', 'state': 'blocked', 'worker_id': 'c_dead', 'exclude_workers': ['c_dead']},
               {'id': 't2', 'run_id': 'r1', 'state': 'done', 'worker_id': 'c_dead'}])


class AccountFailoverTests(unittest.TestCase):
    def test_run_moves_to_working_main_and_tasks_requeue(self):
        s = store()
        self.assertEqual(rehome_runs(s, now=1), ['r1'])
        run = s.get('runs', 'r1')
        self.assertEqual((run['main_worker'], run['account_id'], run['state']), ('m_good', 'good', 'executing'))
        t1 = s.get('tasks', 't1')
        self.assertEqual((t1['state'], t1['worker_id'], t1['exclude_workers']), ('queued', None, []))
        self.assertEqual(s.get('tasks', 't2')['state'], 'done')
        self.assertEqual(s.get('runs', 'r2')['main_worker'], 'm_dead')
        self.assertEqual(s.events[0][0], 'run_rehomed')

    def test_removed_account_worker_missing(self):
        s = store()
        del s.t['workers']['m_dead']
        del s.t['accounts']['dead']
        self.assertEqual(rehome_runs(s), ['r1'])

    def test_healthy_runs_untouched(self):
        s = store(dead_state='ready')
        self.assertEqual(rehome_runs(s), [])

    def test_no_working_account_changes_nothing(self):
        s = store()
        s.t['accounts']['good']['state'] = 'ai_disabled'
        self.assertEqual(rehome_runs(s), [])
        self.assertEqual(s.get('runs', 'r1')['main_worker'], 'm_dead')


if __name__ == '__main__':
    unittest.main()
