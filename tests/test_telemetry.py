import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from notiontool import telemetry
from notiontool.engine import Engine, PendingRemote, RateLimited
from notiontool.model_verification import ModelVerificationError, verify_execution
from notiontool.network_budget import NetworkBudget
from notiontool.notion import NotionAdapter, NotionClient, own_turn_steps
from notiontool.protocol import ContractError
from notiontool.store import Store
from notiontool.telemetry import TASK_CHAIN, chain_gaps, classify, http_class, task_events, task_metrics

REMOTE = ('remote_sent', 'first_output', 'remote_done', 'verify_started', 'verify_done')


class TempStore(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()


class EventSchemaTests(TempStore):
    def test_event_carries_schema_identifiers_and_project(self):
        self.store.put('runs', dict(id='r1', folder_id='f1'))
        seq = self.store.event('task_queued', 'r1', task_id='t1', message='x')
        event = self.store.events()[-1]
        self.assertEqual(event['seq'], seq)
        self.assertEqual(event['run_id'], 'r1')
        self.assertEqual((event['v'], event['task_id'], event['worker_id'], event['project_id']),
                         (telemetry.EVENT_SCHEMA, 't1', '', 'f1'))

    def test_retention_keeps_daily_counts_of_removed_events(self):
        self.store.db.execute('PRAGMA synchronous=OFF')
        for n in range(2105):
            self.store.event('probe', n=n)
        with self.store.lock:
            kept = self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='probe'").fetchone()[0]
        rolled = sum(r['total'] for r in telemetry.rollup(self.store) if r['kind'] == 'probe')
        self.assertEqual(kept, 2001)
        self.assertEqual(kept + rolled, 2105)

    def test_chain_gaps_reports_missing_steps(self):
        for kind in TASK_CHAIN:
            self.store.event(kind, task_id='full')
        self.assertEqual(chain_gaps(self.store, 'full'), [])
        for kind in TASK_CHAIN:
            if kind != 'first_output':
                self.store.event(kind, task_id='gap')
        self.assertEqual(chain_gaps(self.store, 'gap'), ['first_output'])


class ErrorClassTests(unittest.TestCase):
    def test_each_failure_has_one_class(self):
        self.assertEqual(classify(None), '')
        self.assertEqual(classify(RateLimited(5)), 'TRANSIENT')
        self.assertEqual(classify(PendingRemote()), 'REMOTE_UNKNOWN')
        self.assertEqual(classify(ModelVerificationError('a', ['b'])), 'MODEL_MISMATCH')
        self.assertEqual(classify(ContractError('bad')), 'TASK_FAILED')
        self.assertEqual(classify(ValueError('Notion inference: HTTP 400')), 'INVALID_REQUEST')
        self.assertEqual(classify(ValueError('Notion inference: HTTP 503')), 'TRANSIENT')
        self.assertEqual(classify(ValueError('Notion login required')), 'ACCOUNT_BLOCKED')
        self.assertEqual(classify(KeyError('x')), 'LOCAL')
        self.assertEqual((http_class(401), http_class(429), http_class(404)), ('ACCOUNT_BLOCKED', 'TRANSIENT', 'INVALID_REQUEST'))


class FakeTransport:
    def __init__(self, models):
        self.models, self.calls = models, 0

    def request(self, path, body, headers=None, timeout=45):
        assert path == 'getAvailableModels'
        self.calls += 1
        return 200, json.dumps({'models': self.models})


def catalog_entry(label, ident):
    return {'modelProvider': 'anthropic', 'modelMessage': label, 'model': ident}


class CatalogCacheTests(unittest.TestCase):
    def test_cached_catalog_is_reused_and_a_miss_reads_once(self):
        transport = FakeTransport([catalog_entry('Opus 5.5', 'opus-id')])
        client = NotionClient(transport, {'space_id': 's'})
        self.assertEqual(client.model('opus'), 'opus-id')
        self.assertEqual(NotionClient(transport, {'space_id': 's'}).model('opus'), 'opus-id')
        self.assertEqual(transport.calls, 1)
        transport.models.append(catalog_entry('Sonnet 5.5', 'sonnet-id'))
        self.assertEqual(client.model('sonnet'), 'sonnet-id')
        self.assertEqual(transport.calls, 2)
        with self.assertRaises(ValueError):
            client.model('haiku')
        self.assertEqual(transport.calls, 3)

    def test_expired_catalog_is_read_again(self):
        transport = FakeTransport([catalog_entry('Opus 5.5', 'opus-id')])
        client = NotionClient(transport, {'space_id': 's'})
        client.model('opus')
        with patch('notiontool.notion.MODEL_CATALOG_TTL', 0):
            client.model('opus')
        self.assertEqual(transport.calls, 2)


def message_records(steps, sizes):
    def records(table, part):
        sizes.append(len(part))
        return {ident: {'step': steps[ident]} for ident in part}
    return records


class OwnTurnTests(unittest.TestCase):
    def thread(self, count, own, until):
        ids = ['m%d' % n for n in range(count)]
        steps = {}
        for n, ident in enumerate(ids):
            if n in (own, until):
                steps[ident] = {'type': 'user', 'id': ident}
            else:
                steps[ident] = {'type': 'agent-inference', 'id': ident, 'model': 'opus-id' if own < n < until else 'other-id'}
        return ids, steps

    def test_reads_only_the_tail_that_contains_the_own_turn(self):
        ids, steps = self.thread(250, 247, 999)
        sizes = []
        found, read = own_turn_steps(message_records(steps, sizes), ids, 'm247')
        self.assertEqual((read, sizes), (20, [20]))
        self.assertEqual([s['id'] for s in found], ids[230:])

    def test_without_the_own_step_everything_is_read_in_bounded_batches(self):
        ids, steps = self.thread(250, -1, 999)
        sizes = []
        found, read = own_turn_steps(message_records(steps, sizes), ids, 'absent')
        self.assertEqual((read, len(found)), (250, 250))
        self.assertTrue(all(size <= 100 for size in sizes))

    def test_verdict_matches_a_full_read(self):
        ids, steps = self.thread(250, 120, 130)
        full = [steps[i] for i in ids]
        tail, read = own_turn_steps(message_records(steps, []), ids, 'm120')
        self.assertLess(read, 250)
        self.assertEqual(verify_execution(tail, 'm120', 'opus-id'), verify_execution(full, 'm120', 'opus-id'))


class ChainAdapter:
    def __init__(self):
        self.failure = None

    def run(self, worker, task, prompt, cancel, update):
        if self.failure:
            raise self.failure
        return json.dumps(dict(status='done', summary='Verified answer', evidence=['check'], context='finding'))

    def recover(self, worker, task, cancel):
        raise PendingRemote()


class EngineChainTests(TempStore):
    def setUp(self):
        super().setUp()
        self.adapter = ChainAdapter()
        self.store.put('accounts', dict(id='a', slot=1, email='test@example.test', state='ready', cooldown_until=0))
        self.worker('main', 'main')
        for n in range(40):
            self.worker('w%d' % n)
        self.store.put('chats', dict(id='c', title='Main', main_worker='main', folder_id='f', project_path=self.temp.name))
        settings = self.store.settings()
        settings.update(max_workers=40, per_account=40)
        self.store.put('settings', settings)
        self.engine = Engine(self.store, self.adapter)

    def worker(self, ident, purpose='worker'):
        self.store.put('workers', dict(id=ident, account_id='a', thread_id=ident, purpose=purpose, enabled=True,
                                       state='ready', lease='', label=ident, last_used=0))

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        super().tearDown()

    def finish(self, state):
        run = self.engine.submit('c', 'Hallo!')
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.engine.tick()
            tasks = self.engine.tasks(run['id'])
            if tasks and tasks[0]['state'] == state:
                return run, tasks[0]
            time.sleep(.015)
        self.fail('Task did not reach ' + state + ': ' + str(self.engine.tasks(run['id'])))

    def test_successful_task_writes_the_local_chain_and_attempt_timing(self):
        run, task = self.finish('done')
        kinds = [e['kind'] for e in task_events(self.store, task['id'])]
        local = [k for k in TASK_CHAIN if k not in REMOTE]
        self.assertEqual([k for k in kinds if k in local], local)
        attempt = task['attempt_log'][-1]
        self.assertEqual((attempt['attempt'], attempt['state'], attempt['error_class']), (1, 'done', ''))
        self.assertGreaterEqual(attempt['duration'], 0)
        row = next(r for r in task_metrics(self.store, run['id']) if r['task_id'] == task['id'])
        self.assertIsNotNone(row['queue_wait'])
        self.assertIsNotNone(row['total'])
        self.assertEqual(row['errors'], [])

    def test_failed_attempt_records_its_error_class(self):
        self.adapter.failure = ValueError('Notion inference: HTTP 400')
        run, task = self.finish('blocked')
        self.assertEqual(task['attempt_log'][-1]['error_class'], 'INVALID_REQUEST')
        finished = [e for e in task_events(self.store, task['id']) if e['kind'] == 'task_attempt_finished']
        self.assertEqual(finished[-1]['error_class'], 'INVALID_REQUEST')
        row = next(r for r in task_metrics(self.store, run['id']) if r['task_id'] == task['id'])
        self.assertEqual(row['errors'], ['INVALID_REQUEST'])


class VerifyEventTests(TempStore):
    def test_verify_reads_only_the_own_turn_and_reports_it(self):
        ids = ['m%d' % n for n in range(150)]
        sizes = []

        def step(ident):
            n = int(ident[1:])
            if n == 140:
                return {'type': 'user', 'id': ident}
            return {'type': 'agent-inference', 'id': ident, 'model': 'opus-id' if n > 140 else 'old-id'}

        class Client:
            info = {'space_id': 'space'}

            def call(self, path, body, timeout=45):
                table = body['requests'][0]['pointer']['table']
                part = [r['pointer']['id'] for r in body['requests']]
                if table == 'thread':
                    return {'recordMap': {'thread': {'th': {'value': {'space_id': 'space', 'alive': True, 'messages': ids}}}}}
                sizes.append(len(part))
                return {'recordMap': {'thread_message': {i: {'value': {'step': step(i)}} for i in part}}}

        adapter = NotionAdapter(self.store)
        adapter.client = lambda *args, **kwargs: Client()
        task = dict(id='t1', run_id='', step_id='m140', assigned_model_id='opus-id', attempts=1)
        self.store.put('tasks', task)
        reported = adapter.verify_response_model(dict(id='w1', account_id='a', thread_id='th'), task)
        self.assertEqual(reported, ['opus-id'])
        self.assertEqual(sizes, [20])
        saved = self.store.get('tasks', 't1')
        self.assertEqual((saved['verify_messages_read'], saved['model_verification']), (20, 'verified'))
        events = [e for e in task_events(self.store, 't1') if e['kind'].startswith('verify_')]
        self.assertEqual([e['kind'] for e in events], ['verify_started', 'verify_done'])
        self.assertEqual((events[-1]['outcome'], events[-1]['messages_read']), ('verified', 20))


class CoordinationEventTests(TempStore):
    def test_rate_limit_records_the_interval_change(self):
        budget = NetworkBudget(self.store, clock=lambda: 1000.0, sleep=lambda seconds: None)
        budget.limited(5)
        event = [e for e in self.store.events() if e['kind'] == 'request_coordination_adjusted'][-1]
        self.assertEqual((event['interval_before'], event['interval']), (0.25, 0.5))
        self.assertGreaterEqual(event['paused_seconds'], 5)


if __name__ == '__main__':
    unittest.main()
