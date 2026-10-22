"""team_rotation.py: 23h Welcome Coworker rotation with a fake clock, fake store and fake Notion client."""
import copy
import unittest
import uuid

from notiontool import team_rotation
from notiontool.team_rotation import LoopChecks, TeamRotator, age_seconds, busy_reason, epoch, max_age_hours

HOUR = 3600
T0 = 1_800_000_000.0
OLD_TID = str(uuid.UUID(int=1))
NEW_TID = str(uuid.UUID(int=2))


class FakeStore:
    def __init__(self, settings=None):
        self.data, self.events, self.extra = {}, [], dict(settings or {})

    def get(self, kind, ident, default=None):
        doc = self.data.get(kind, {}).get(ident)
        return copy.deepcopy(doc) if doc is not None else default

    def all(self, kind):
        return [copy.deepcopy(d) for d in self.data.get(kind, {}).values()]

    def put(self, kind, doc):
        self.data.setdefault(kind, {})[doc['id']] = copy.deepcopy(doc)
        return doc

    def event(self, kind, run_id='', **fields):
        self.events.append(dict(fields, kind=kind))

    def settings(self):
        return dict(dict(per_account=2, max_workers=40), **self.extra)

    def kinds(self):
        return [e['kind'] for e in self.events]


class FakeClient:
    def __init__(self, threads):
        self.info = {'space_id': 'space'}
        self.listed = threads

    def model(self):
        return 'opus'

    def threads(self):
        return [{'id': t} for t in self.listed]


class FakeAdapter:
    """No network: every Notion call is recorded; creation lists the thread only when listed=True."""

    def __init__(self, store, listed=True, records=None, budget=None):
        self.store, self.listed, self.records = store, listed, records or {}
        self.threads = [OLD_TID, str(uuid.UUID(int=9))]
        self.created, self.refreshed, self.record_calls = [], 0, 0
        self.network_budget = budget
        self.client_obj = FakeClient(self.threads)

    def client(self, account_id):
        return self.client_obj

    def thread_records(self, client, ids):
        self.record_calls += 1
        return {i: self.records[i] for i in ids if i in self.records}

    @staticmethod
    def thread_key(thread):
        return str(uuid.UUID(thread['id']))

    def new_id(self, space_id, tail):
        return NEW_TID.replace('-', '')

    def create_welcome(self, client, tid, model):
        self.created.append(tid)
        if self.listed:
            self.threads.append(str(uuid.UUID(tid)))
        return self.listed, True

    def refresh(self, account_id):
        self.refreshed += 1
        for tid in self.threads:
            ident = account_id + ':' + tid
            if not self.store.get('workers', ident):
                self.store.put('workers', dict(id=ident, account_id=account_id, thread_id=tid, purpose='worker',
                                               enabled=False, state='ready', lease='', label='Untitled chat'))

    def apply_chat_pool(self, account_id, client):
        return {}


class Budget:
    def __init__(self, **status):
        self.value = status

    def status(self):
        return self.value


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def setup(created_at=T0, settings=None, **adapter_kw):
    store = FakeStore(settings)
    store.put('accounts', dict(id='a1', state='ready', slot=1, team_threads={
        'main': {'id': 'main-t', 'pending': False}, 'worker1': {'id': OLD_TID, 'pending': False}}))
    worker = dict(id='a1:' + OLD_TID, account_id='a1', thread_id=OLD_TID, purpose='worker', enabled=True,
                  state='ready', lease='', join_state='joined', label='Coworker 1')
    if created_at is not None:
        worker['created_at'] = created_at
    store.put('workers', worker)
    adapter = FakeAdapter(store, **adapter_kw)
    jobs = []

    def start_job(title, fn, ident):
        jobs.append(fn)  # runs later, like the Application's background job
        return {'job_id': 'j%d' % len(jobs)}
    clock = Clock(T0 + 24 * HOUR)
    return store, adapter, TeamRotator(store, adapter, start_job, clock=clock), jobs, clock


def run(rotator, jobs):
    """One check, then every started background job (its exception surfaces like a failed job)."""
    started = rotator.check()
    while jobs:
        jobs.pop(0)()
    return started


class Helpers(unittest.TestCase):
    def test_setting_default_and_clamp(self):
        self.assertEqual(max_age_hours({}), 22)
        self.assertEqual(max_age_hours({'coworker_max_age_hours': 0}), 1)
        self.assertEqual(max_age_hours({'coworker_max_age_hours': 500}), 72)
        self.assertEqual(max_age_hours({'coworker_max_age_hours': 'x'}), 22)

    def test_epoch_formats(self):
        self.assertEqual(epoch(T0 * 1000), T0)
        self.assertEqual(epoch(T0), T0)
        self.assertEqual(epoch('2027-01-15T08:00:00Z'), epoch('2027-01-15T08:00:00+00:00'))
        self.assertEqual(epoch('2027-01-15T08:00:00'), epoch('2027-01-15T08:00:00Z'))  # naive = UTC
        self.assertEqual(epoch(None), 0.0)
        self.assertEqual(epoch('nonsense'), 0.0)
        self.assertIsNone(age_seconds({}, T0))
        self.assertEqual(age_seconds({'created_at': T0}, T0 + 5), 5)

    def test_busy_reasons(self):
        idle = dict(id='w', state='ready', lease='', join_state='joined')
        self.assertEqual(busy_reason(idle, [], []), '')
        self.assertTrue(busy_reason(dict(idle, lease='L'), [], []))
        for state in ('busy', 'recovering', 'error', 'missing'):
            self.assertTrue(busy_reason(dict(idle, state=state), [], []))
        for state in ('running', 'uncertain', 'cancelling', 'queued', 'retry'):
            self.assertTrue(busy_reason(idle, [dict(worker_id='w', state=state)], []), state)
        self.assertEqual(busy_reason(idle, [dict(worker_id='w', state='done')], []), '')
        self.assertTrue(busy_reason(idle, [dict(worker_id='w', state='blocked', remote_unknown=True)], []))
        self.assertTrue(busy_reason(idle, [], [dict(id='w', state='submitted')]))
        self.assertTrue(busy_reason(dict(idle, handoff='text', handoff_delivered=False), [], []))
        self.assertEqual(busy_reason(dict(idle, handoff='text', handoff_delivered=True), [], []), '')


class Rotation(unittest.TestCase):
    def test_young_chat_is_left_alone(self):
        store, adapter, rotator, jobs, clock = setup()
        clock.now = T0 + 21 * HOUR
        self.assertEqual(run(rotator, jobs), [])
        self.assertEqual(adapter.created, [])

    def test_old_idle_chat_rotates_once(self):
        store, adapter, rotator, jobs, clock = setup()
        self.assertEqual(run(rotator, jobs), ['a1'])
        self.assertEqual(len(adapter.created), 1)
        account = store.get('accounts', 'a1')
        self.assertEqual(account['team_threads']['worker1'], {'id': NEW_TID.replace('-', ''), 'pending': False})
        old = store.get('workers', 'a1:' + OLD_TID)
        self.assertEqual((old['enabled'], old['purpose'], old['retired']), (False, 'retired_worker', True))
        self.assertEqual(old['successor'], NEW_TID)
        self.assertNotIn('rotation_reserved', old)
        new = store.get('workers', 'a1:' + NEW_TID)
        self.assertEqual((new['purpose'], new['join_state'], new['enabled'], new['created_at']),
                         ('worker', 'pending', False, clock.now))
        self.assertEqual(store.kinds(), ['team_rotation_started', 'team_rotation_done'])
        # the new chat is young: nothing more is created, even on later checks
        clock.now += 2 * HOUR
        self.assertEqual(run(rotator, jobs), [])
        self.assertEqual(len(adapter.created), 1)

    def test_setting_changes_threshold(self):
        store, adapter, rotator, jobs, clock = setup(settings={'coworker_max_age_hours': 30})
        self.assertEqual(run(rotator, jobs), [])
        clock.now = T0 + 31 * HOUR
        self.assertEqual(run(rotator, jobs), ['a1'])

    def test_busy_chat_waits_then_rotates_when_idle(self):
        store, adapter, rotator, jobs, clock = setup()
        store.put('tasks', dict(id='t1', worker_id='a1:' + OLD_TID, state='running'))
        worker = store.get('workers', 'a1:' + OLD_TID)
        store.put('workers', dict(worker, state='busy', lease='L'))
        self.assertEqual(run(rotator, jobs), [])
        self.assertIn('a1:' + OLD_TID, rotator.public('a1')['waiting'])
        self.assertEqual(adapter.created, [])
        store.put('tasks', dict(id='t1', worker_id='a1:' + OLD_TID, state='done'))
        store.put('workers', dict(worker, state='ready', lease=''))
        self.assertEqual(run(rotator, jobs), ['a1'])
        self.assertEqual(len(adapter.created), 1)

    def test_unconfirmed_creation_is_never_resent(self):
        store, adapter, rotator, jobs, clock = setup(listed=False)
        with self.assertRaises(ValueError):
            run(rotator, jobs)
        self.assertEqual(len(adapter.created), 1)
        item = store.get('accounts', 'a1')['team_threads']['worker1']
        self.assertTrue(item['pending'])
        old = store.get('workers', 'a1:' + OLD_TID)
        self.assertTrue(old['enabled'])  # the old chat keeps working meanwhile
        self.assertEqual(old['purpose'], 'worker')
        self.assertIn('team_rotation_failed', store.kinds())
        # after the backoff it only reconciles the chat list; still unlisted -> no second creation
        clock.now += team_rotation.BACKOFF_S + 1
        with self.assertRaises(ValueError):
            run(rotator, jobs)
        self.assertEqual(len(adapter.created), 1)
        # Notion lists the chat later: the pending entry is confirmed without creating anything
        adapter.threads.append(NEW_TID)
        clock.now += team_rotation.BACKOFF_S + 1
        self.assertEqual(run(rotator, jobs), ['a1'])
        self.assertEqual(len(adapter.created), 1)
        self.assertFalse(store.get('accounts', 'a1')['team_threads']['worker1']['pending'])
        self.assertEqual(store.get('workers', 'a1:' + OLD_TID)['purpose'], 'retired_worker')

    def test_foreign_pending_entry_blocks_account(self):
        store, adapter, rotator, jobs, clock = setup()
        account = store.get('accounts', 'a1')
        account['team_threads']['worker2'] = {'id': 'x', 'pending': True}
        store.put('accounts', account)
        self.assertEqual(run(rotator, jobs), [])
        self.assertEqual(adapter.created, [])

    def test_network_budget_respected(self):
        for status in ({'paused': True}, {'cooldown_seconds': 30}):
            store, adapter, rotator, jobs, clock = setup(budget=Budget(**status))
            self.assertEqual(run(rotator, jobs), [])
            self.assertEqual(adapter.created, [])

    def test_one_job_per_account(self):
        store, adapter, rotator, jobs, clock = setup()
        rotator.rotating.add('a1')
        self.assertEqual(run(rotator, jobs), [])
        busy = TeamRotator(store, adapter, lambda *a: (_ for _ in ()).throw(ValueError('busy')), clock=clock)
        self.assertEqual(busy.check(), [])
        self.assertEqual(adapter.created, [])
        self.assertNotIn('a1', busy.rotating)

    def test_maybe_check_runs_at_most_every_five_minutes(self):
        store, adapter, rotator, jobs, clock = setup()
        calls = []
        rotator.check = lambda: calls.append(clock.now) or []
        rotator.maybe_check()
        clock.now += 299
        rotator.maybe_check()
        clock.now += 2
        rotator.maybe_check()
        self.assertEqual(len(calls), 2)

    def test_loop_checks_runs_both_and_delegates(self):
        grower = type('G', (), {'maybe_check': lambda self: ['g'], 'public': lambda self, a='': 'grower'})()
        rot = type('R', (), {'maybe_check': lambda self: ['r']})()
        chain = LoopChecks(grower, rot)
        self.assertEqual(chain.maybe_check(), ['g', 'r'])
        self.assertEqual(chain.public(), 'grower')


class Backfill(unittest.TestCase):
    def test_from_thread_record(self):
        store, adapter, rotator, jobs, clock = setup(created_at=None, records={OLD_TID: {'created_time': (T0 - HOUR) * 1000}})
        rotator.backfill('a1', clock.now)
        worker = store.get('workers', 'a1:' + OLD_TID)
        self.assertEqual((worker['created_at'], worker['created_at_source']), (T0 - HOUR, 'notion'))
        rotator.backfill('a1', clock.now)
        self.assertEqual(adapter.record_calls, 1)  # stamped once, no more network

    def test_first_seen_without_record(self):
        store, adapter, rotator, jobs, clock = setup(created_at=None)
        run(rotator, jobs)
        worker = store.get('workers', 'a1:' + OLD_TID)
        self.assertEqual((worker['created_at'], worker['created_at_source']), (clock.now, 'first_seen'))
        self.assertEqual(adapter.created, [])  # first seen now = age 0, no rotation

    def test_unreadable_records_keep_first_seen_and_retry(self):
        store, adapter, rotator, jobs, clock = setup(created_at=None)
        adapter.thread_records = lambda client, ids: (_ for _ in ()).throw(OSError('offline'))
        rotator.backfill('a1', clock.now)
        worker = store.get('workers', 'a1:' + OLD_TID)
        self.assertNotIn('created_at', worker)
        self.assertEqual(worker['first_seen_at'], clock.now)


class Wiring(unittest.TestCase):
    def test_settings_and_state(self):
        from notiontool import desktop_settings, notion_shell
        entry = [e for e in desktop_settings.HARNESS_SETTINGS if e['key'] == 'coworker_max_age_hours'][0]
        self.assertEqual((entry['min'], entry['max'], entry['default']), (1, 72, 22))
        self.assertIn('coworker_max_age_hours', notion_shell.INT_SETTINGS)
        with open('notiontool/server.py', encoding='utf-8', newline='') as handle:
            source = handle.read()
        self.assertIn("('coworker_max_age_hours',1,72)", source)
        self.assertIn('LoopChecks(self.team_grower, self.team_rotator)', source)
        with open('notiontool/desktop_shell.py', encoding='utf-8') as handle:
            self.assertIn("row['age_s']", handle.read())


if __name__ == '__main__':
    unittest.main()
