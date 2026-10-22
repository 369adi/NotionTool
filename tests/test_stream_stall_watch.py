"""Notion keeps runInferenceTranscript open after the turn (live 22 Oct 2026, Coworker 4: turn done 05:46:42,
stream still open 06:11). The stream ends when Notion's thread record shows our turn completed. Fakes only."""
import json
import threading
import time
import unittest

from notiontool.notion import NotionAdapter, NotionClient
from tests.test_thread_list_guard import T1, Base

SPACE = 'space-1'


def record(latest_ms, completed_ms, status='completed'):
    # live shape 2026-10-22: {spaceId, value: {value: record}}
    return {'spaceId': SPACE, 'value': {'value': {'id': T1, 'latest_user_or_trigger_time': latest_ms,
            'data': {'last_turn_outcome': {'status': status, 'completed_time': completed_ms}}}, 'role': 'editor'}}


class FanoutTransport:
    """First answer names the cell (fanoutData, empty recordMap), like Notion live."""
    def __init__(self, rec):
        self.rec, self.calls = rec, []

    def request(self, path, body, headers=None, timeout=45):
        self.calls.append(dict(headers or {}))
        if (headers or {}).get('x-notion-cell') != 'prod-space-euc1-0001':
            return 200, json.dumps({'recordMap': {'__version__': 3},
                                    'fanoutData': [{'headers': {'x-notion-cell': 'prod-space-euc1-0001'}, 'request': body}]})
        return 200, json.dumps({'recordMap': {'__version__': 3, 'thread': {T1: self.rec()}}})


class FanoutTests(unittest.TestCase):
    def test_fanout_is_followed_to_the_named_cell(self):
        now = time.time() * 1000
        transport = FanoutTransport(lambda: record(now - 1000, now))
        client = NotionClient(transport, {'space_id': SPACE})
        latest, completed, status = NotionAdapter.turn_outcome(None, client, T1)
        self.assertEqual((latest, completed, status), (now - 1000, now, 'completed'))
        self.assertEqual(transport.calls[1].get('x-notion-cell'), 'prod-space-euc1-0001')

    def test_plain_answer_is_unchanged(self):
        class Plain:
            def request(self, path, body, headers=None, timeout=45):
                return 200, json.dumps({'recordMap': {'thread': {}}, 'x': 1})
        self.assertEqual(NotionClient(Plain(), {'space_id': SPACE}).call('loadUserContent', {}),
                         {'recordMap': {'thread': {}}, 'x': 1})


class StallWatchTests(Base):
    def adapter(self, outcome):
        adapter = NotionAdapter(self.store)
        adapter.turn_outcome = lambda client, tid: outcome()
        adapter.stream_turn_poll_s, adapter.stream_turn_quiet_s = 0.05, 0.1
        return adapter

    def watch(self, adapter, cancel=None, seconds=1.0, chunks=False):
        cancel = cancel or threading.Event()
        stream_cancel, alive, last = threading.Event(), [True], [time.monotonic()]
        thread = threading.Thread(target=adapter._stall_watch, daemon=True,
                                  args=(object(), {'thread_id': T1}, cancel, stream_cancel, alive, last, 'r1',
                                        {'task_id': 't1'}))
        thread.start()
        end = time.monotonic() + seconds
        while time.monotonic() < end and not stream_cancel.is_set():
            if chunks:
                last[0] = time.monotonic()  # bytes still flowing
            time.sleep(0.02)
        alive[0] = False
        thread.join(2)
        return stream_cancel.is_set()

    def test_open_stream_ends_when_notion_finished_our_turn(self):
        start = time.time() * 1000
        self.assertTrue(self.watch(self.adapter(lambda: (start, time.time() * 1000, 'completed'))))

    def test_running_turn_is_never_ended(self):
        start = time.time() * 1000
        self.assertFalse(self.watch(self.adapter(lambda: (start, start - 60000, 'completed'))))

    def test_outcome_of_an_older_turn_does_not_count(self):
        old = time.time() * 1000 - 600000
        self.assertFalse(self.watch(self.adapter(lambda: (old, old + 1000, 'completed'))))

    def test_unreadable_record_keeps_waiting(self):
        def broken():
            raise LookupError('thread record not returned')
        self.assertFalse(self.watch(self.adapter(broken)))

    def test_no_record_reads_while_bytes_flow(self):
        reads = []
        def outcome():
            reads.append(1)
            return (0, time.time() * 1000, 'completed')
        self.assertFalse(self.watch(self.adapter(outcome), chunks=True, seconds=0.5))
        self.assertEqual(reads, [])

    def test_task_cancel_is_passed_to_the_stream(self):
        cancel = threading.Event()
        cancel.set()
        self.assertTrue(self.watch(self.adapter(lambda: (0, 0, '')), cancel=cancel, seconds=0.5))


if __name__ == '__main__':
    unittest.main()
