"""Wiring batch 1 (T2): run confirm endpoint, stop_turn and ThreadMissing. Fakes only, no network."""
import inspect
import json
import unittest

from notiontool import server
from notiontool import notion
from notiontool.notion import NotionAdapter, NotionClient, ThreadMissing
from notiontool.vendor.transport import NotionHttpError


class FakeStore:
    def __init__(self, runs=(), workers=(), accounts=(), settings=None):
        self.data = {'runs': {r['id']: dict(r) for r in runs},
                     'workers': {w['id']: dict(w) for w in workers},
                     'accounts': {a['id']: dict(a) for a in accounts}}
        self._settings = dict(task_timeout=1800)
        self._settings.update(settings or {})
        self.events = []

    def get(self, kind, ident, default=None):
        return self.data.get(kind, {}).get(ident, default)

    def all(self, kind):
        return list(self.data.get(kind, {}).values())

    def settings(self):
        return dict(self._settings)

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, run_id, fields))


class FakeApp:
    confirm_run_request = server.Application.confirm_run_request

    def __init__(self, store, engine):
        self.store = store
        self.engine = engine


class ConfirmingEngine:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def confirm_run(self, run_id):
        self.calls.append(run_id)
        self.store.data['runs'][run_id]['state'] = 'done'
        return {'confirmed': run_id}


class ConfirmEndpointTests(unittest.TestCase):
    def make(self, state='awaiting_main', engine=True):
        store = FakeStore(runs=[{'id': 'r1', 'state': state, 'chat_id': 'c1'}])
        eng = ConfirmingEngine(store) if engine else object()
        return FakeApp(store, eng), store, eng

    def test_confirm_200_calls_engine_and_returns_new_state(self):
        app, store, eng = self.make()
        status, payload = app.confirm_run_request({'id': 'r1'})
        self.assertEqual(status, 200)
        self.assertEqual(eng.calls, ['r1'])
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['state'], 'done')
        self.assertEqual(payload['result'], {'confirmed': 'r1'})

    def test_confirm_404_for_unknown_or_empty_id(self):
        app, store, eng = self.make()
        for body in ({'id': 'nope'}, {'id': ''}, {}):
            with self.subTest(body=body):
                status, payload = app.confirm_run_request(body)
                self.assertEqual(status, 404)
                self.assertIn('not found', payload['error'])
        self.assertEqual(eng.calls, [])

    def test_confirm_409_when_not_awaiting_main(self):
        for state in ('executing', 'done', 'failed'):
            with self.subTest(state=state):
                app, store, eng = self.make(state=state)
                status, payload = app.confirm_run_request({'id': 'r1'})
                self.assertEqual(status, 409)
                self.assertIn(state, payload['error'])
                self.assertEqual(eng.calls, [])
                self.assertEqual(store.get('runs', 'r1')['state'], state)

    def test_confirm_501_without_engine_confirm_run(self):
        app, store, eng = self.make(engine=False)
        status, payload = app.confirm_run_request({'id': 'r1'})
        self.assertEqual(status, 501)
        self.assertIn('confirm_run', payload['error'])
        self.assertEqual(store.get('runs', 'r1')['state'], 'awaiting_main')

    def test_route_is_wired_before_the_generic_post(self):
        source = inspect.getsource(server)
        route = source.index("path=='/api/run/confirm'")
        generic = source.index("return self.response(200,app.action(path,self.read_body()))")
        self.assertLess(route, generic)
        self.assertIn('app.confirm_run_request(self.read_body())', source)

    def test_awaiting_main_is_exposed_in_chat_list_and_snapshot(self):
        source = inspect.getsource(server.Application.chat_list)
        self.assertIn("awaiting_main=[r['id'] for r in runs if r.get('state') == 'awaiting_main']", source)
        source = inspect.getsource(server.Application.snapshot)
        self.assertIn("awaiting_main=[r['id'] for r in runs if r.get('state') == 'awaiting_main']", source)


class FakeTransport:
    def __init__(self, status=200, text='{}', stream_error=None):
        self.status, self.text, self.stream_error = status, text, stream_error
        self.calls, self.streams = [], []

    def request(self, path, body, headers=None, timeout=45):
        self.calls.append((path, body))
        return self.status, self.text

    def stream(self, path, body, headers=None, timeout=60, cancel=None):
        self.streams.append((path, body))
        if self.stream_error is not None:
            raise self.stream_error
        return iter(())


def client(status=200, text='{}', stream_error=None):
    return NotionClient(FakeTransport(status, text, stream_error), {'space_id': 's1'})


class ThreadMissingTests(unittest.TestCase):
    def test_transcript_404_raises_thread_missing_with_thread_id(self):
        with self.assertRaises(ThreadMissing) as caught:
            client(404, 'gone').transcript('t1')
        self.assertEqual(caught.exception.thread_id, 't1')
        self.assertEqual(caught.exception.path, 'getThreadTranscript')

    def test_transcript_500_is_not_thread_missing(self):
        with self.assertRaises(ValueError) as caught:
            client(500, 'boom').transcript('t1')
        self.assertNotIsInstance(caught.exception, ThreadMissing)
        self.assertIn('boom', str(caught.exception))

    def test_404_on_non_thread_path_stays_a_plain_error(self):
        with self.assertRaises(ValueError) as caught:
            client(404, 'nf').call('loadUserContent', {})
        self.assertNotIsInstance(caught.exception, ThreadMissing)

    def test_transcript_200_still_returns_json(self):
        self.assertEqual(client(200, '{"ok": 1}').transcript('t1'), {'ok': 1})


def adapter(store, fake_client):
    a = object.__new__(NotionAdapter)
    a.store = store
    a.client = lambda account_id, anonymous=False: fake_client
    a.template = {'transcript': [{'value': {'model': 'm'}}]}
    a.body = lambda *args, **kwargs: {}
    return a


WORKER = {'id': 'w1', 'thread_id': 't1', 'account_id': 'a1'}
STOP = {'stop_turn_endpoint': 'configuredStop'}


class StopTurnTests(unittest.TestCase):
    def test_stop_turn_calls_the_configured_endpoint(self):
        c = client(200, '{}')
        store = FakeStore(workers=[WORKER], settings=STOP)
        result = adapter(store, c).stop_turn('t1')
        self.assertEqual(result, {'ok': True, 'reason': 'sent'})
        self.assertEqual(c.transport.calls, [('configuredStop', {'spaceId': 's1', 'threadId': 't1'})])
        self.assertEqual(store.events[-1][0], 'stop_turn_sent')

    def test_stop_turn_setting_off_sends_nothing(self):
        c = client()
        result = adapter(FakeStore(workers=[WORKER], settings={'stop_turn': False}), c).stop_turn('t1')
        self.assertEqual(result['reason'], 'disabled')
        self.assertEqual(c.transport.calls, [])

    def test_stop_turn_without_verified_endpoint_sends_nothing(self):
        c = client()
        result = adapter(FakeStore(workers=[WORKER]), c).stop_turn('t1')
        self.assertEqual(result, {'ok': False, 'reason': 'no_verified_endpoint'})
        self.assertEqual(c.transport.calls, [])

    def test_stop_turn_unknown_thread(self):
        c = client()
        result = adapter(FakeStore(workers=[WORKER]), c).stop_turn('other')
        self.assertEqual(result['reason'], 'unknown_thread')
        self.assertEqual(c.transport.calls, [])

    def test_stop_turn_404_is_a_missing_endpoint_not_a_missing_thread(self):
        store = FakeStore(workers=[WORKER], settings=STOP)
        result = adapter(store, client(404, 'Cannot POST')).stop_turn('t1')
        self.assertEqual((result['ok'], result['reason']), (False, 'error'))
        self.assertEqual(store.events[-1][0], 'stop_turn_failed')

    def test_stop_turn_500_is_reported_not_raised(self):
        store = FakeStore(workers=[WORKER], settings=STOP)
        result = adapter(store, client(500, 'boom')).stop_turn('t1')
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'error')
        self.assertEqual(store.events[-1][0], 'stop_turn_failed')


class ContinueTurnTests(unittest.TestCase):
    IDLE = json.dumps({'session': {'status': 'idle'}})

    def run_continue(self, c):
        store = FakeStore(workers=[WORKER], accounts=[{'id': 'a1'}])
        return adapter(store, c).continue_turn(WORKER, 'continue', 'step1')

    def test_continue_turn_ok(self):
        c = client(200, self.IDLE)
        self.assertTrue(self.run_continue(c))
        self.assertEqual(len(c.transport.streams), 1)

    def test_continue_turn_stream_404_raises_thread_missing(self):
        with self.assertRaises(ThreadMissing) as caught:
            self.run_continue(client(200, self.IDLE, NotionHttpError(404)))
        self.assertEqual(caught.exception.thread_id, 't1')

    def test_continue_turn_stream_500_keeps_original_error(self):
        with self.assertRaises(NotionHttpError) as caught:
            self.run_continue(client(200, self.IDLE, NotionHttpError(500)))
        self.assertEqual(caught.exception.status, 500)
        self.assertNotIsInstance(caught.exception, ThreadMissing)

    def test_continue_turn_transcript_404_raises_thread_missing(self):
        with self.assertRaises(ThreadMissing):
            self.run_continue(client(404, 'gone'))

    def test_thread_paths_constant(self):
        self.assertIn('getThreadTranscript', notion.THREAD_PATHS)
        self.assertIn('runInferenceTranscript', notion.THREAD_PATHS)


if __name__ == '__main__':
    unittest.main()
