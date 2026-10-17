import io
import json
import re
import threading
import types
import unittest
from unittest import mock

from notiontool import server
from notiontool.server import Application


class FakeStore:
    def __init__(self, events, engine):
        self.rows = [dict(seq=s, ts=float(s), run_id=r, kind=k, n=s) for s, r, k in events]
        self.engine = engine
        self.calls = []

    def events(self, after=0, limit=150):
        self.calls.append((after, limit))
        # One pass is enough: stop the stream loop after the first read.
        self.engine.stop_event.set()
        return [dict(e) for e in self.rows if e['seq'] > after][-limit:]

    def get(self, kind, ident, default=None):
        return default


class FakeEngine:
    def __init__(self):
        self.stop_event = threading.Event()

    def tasks(self, run_id):
        return []


class FakeHandler:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}
        self.close_connection = False

    def send_response(self, code):
        self.status = code

    def send_header(self, name, value):
        self.sent_headers[name] = value

    def end_headers(self):
        pass


EVENTS = [(1, '', 'run_started'), (2, 'r1', 'task_started'), (3, 'r1', 'task_done'),
          (4, '', 'worker_state'), (5, 'r1', 'run_done')]


def run_stream(query=None, headers=None, events=EVENTS):
    engine = FakeEngine()
    store = FakeStore(events, engine)
    app = types.SimpleNamespace(store=store, engine=engine)
    handler = FakeHandler(headers)
    query = {k: [v] for k, v in (query or {}).items()}
    with mock.patch.object(server.time, 'sleep', lambda s: None):
        Application.stream(app, handler, query)
    text = handler.wfile.getvalue().decode('utf-8')
    ids = [int(x) for x in re.findall(r'^id: (\d+)$', text, re.M)]
    return text, ids, store, handler


class StreamResumeTest(unittest.TestCase):
    def test_no_since_sends_everything(self):
        text, ids, store, handler = run_stream()
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.sent_headers['Content-Type'], 'text/event-stream; charset=utf-8')
        self.assertTrue(text.startswith('retry: 2000\n\n'))
        self.assertEqual(ids, [1, 2, 3, 4, 5])
        self.assertEqual(store.calls[0][0], 0)

    def test_since_sends_only_newer_events_in_order(self):
        text, ids, store, _ = run_stream({'since': '2'})
        self.assertEqual(ids, [3, 4, 5])
        self.assertEqual(store.calls[0][0], 2)

    def test_since_beyond_last_seq_sends_nothing_old(self):
        text, ids, store, handler = run_stream({'since': '99'})
        self.assertEqual(handler.status, 200)
        self.assertEqual(ids, [])
        self.assertNotIn('data:', text)
        self.assertEqual(store.calls[0][0], 99)

    def test_invalid_since_is_ignored_safely(self):
        for bad in ('abc', '1.5', '2;3', ' ', '0x10'):
            with self.subTest(since=bad):
                text, ids, store, handler = run_stream({'since': bad})
                self.assertEqual(handler.status, 200)
                self.assertEqual(ids, [1, 2, 3, 4, 5])
                self.assertEqual(store.calls[0][0], 0)

    def test_negative_since_is_clamped_to_zero(self):
        text, ids, store, _ = run_stream({'since': '-5'})
        self.assertEqual(ids, [1, 2, 3, 4, 5])
        self.assertEqual(store.calls[0][0], 0)

    def test_last_event_id_header_wins_over_since(self):
        text, ids, store, _ = run_stream({'since': '1'}, {'Last-Event-ID': '4'})
        self.assertEqual(ids, [5])

    def test_after_still_works(self):
        text, ids, store, _ = run_stream({'after': '3'})
        self.assertEqual(ids, [4, 5])

    def test_since_preferred_over_after(self):
        text, ids, store, _ = run_stream({'since': '4', 'after': '1'})
        self.assertEqual(ids, [5])

    def test_every_event_carries_its_seq_as_id(self):
        text, ids, store, _ = run_stream({'since': '3'})
        blocks = [b for b in text.split('\n\n') if b.startswith('id: ')]
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            lines = block.split('\n')
            seq = int(lines[0][len('id: '):])
            data = json.loads(lines[2][len('data: '):])
            self.assertEqual(data['seq'], seq)
            self.assertEqual(lines[1], 'event: ' + data['kind'])
        self.assertEqual(ids, [4, 5])

    def test_run_filter_combines_with_since(self):
        events = EVENTS + [(6, 'r2', 'task_started'), (7, '', 'worker_state')]
        text, ids, store, _ = run_stream({'since': '2', 'run_id': 'r1'}, events=events)
        self.assertEqual(ids, [3, 4, 5, 7])


if __name__ == '__main__':
    unittest.main()
