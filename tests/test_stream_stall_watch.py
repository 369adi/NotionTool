"""A frozen Edge stream is ended only when the chat turn is over (live 22 Oct 05:42, Coworker 4). Fakes only."""
import threading
import time
import unittest

from notiontool.notion import NotionAdapter
from tests.test_thread_list_guard import T1, Base


class Client:
    def __init__(self, status='completed', pending=False, fail=False):
        self.status, self.pending, self.fail, self.calls = status, pending, fail, 0

    def transcript(self, thread, limit=100):
        self.calls += 1
        if self.fail:
            raise OSError('tab gone')
        return {'session': {'status': self.status}, 'session_pending': self.pending}


class StallWatchTests(Base):
    def adapter(self, busy=False):
        adapter = NotionAdapter(self.store)
        adapter.thread_busy = lambda client, tid: busy
        adapter.stream_stall_check_s, adapter.stream_stall_recheck_s, adapter.stream_stall_poll_s = 0.2, 0.1, 0.05
        return adapter

    def watch(self, adapter, client, cancel=None, seconds=1.0):
        cancel = cancel or threading.Event()
        stream_cancel, alive, last = threading.Event(), [True], [time.monotonic()]
        thread = threading.Thread(target=adapter._stall_watch, daemon=True,
                                  args=(client, {'thread_id': T1}, cancel, stream_cancel, alive, last, 'r1',
                                        {'task_id': 't1'}))
        thread.start()
        stream_cancel.wait(seconds)
        alive[0] = False
        thread.join(2)
        return stream_cancel.is_set()

    def kinds(self):
        return [e['kind'] for e in self.store.events(limit=50)] if hasattr(self.store, 'events') else None

    def test_frozen_stream_with_finished_turn_is_ended(self):
        client = Client('completed')
        self.assertTrue(self.watch(self.adapter(), client))
        self.assertGreaterEqual(client.calls, 1)

    def test_running_turn_is_never_ended(self):
        self.assertFalse(self.watch(self.adapter(), Client('running')))

    def test_busy_thread_is_never_ended(self):
        self.assertFalse(self.watch(self.adapter(busy=True), Client('completed')))

    def test_pending_session_is_never_ended(self):
        self.assertFalse(self.watch(self.adapter(), Client('completed', pending=True)))

    def test_unreadable_chat_keeps_waiting(self):
        self.assertFalse(self.watch(self.adapter(), Client(fail=True)))

    def test_task_cancel_is_passed_to_the_stream(self):
        cancel = threading.Event()
        cancel.set()
        client = Client('running')
        self.assertTrue(self.watch(self.adapter(), client, cancel=cancel, seconds=0.5))
        self.assertEqual(client.calls, 0)

    def test_no_check_before_the_silence_limit(self):
        adapter = self.adapter()
        adapter.stream_stall_check_s = 30
        client = Client('completed')
        self.assertFalse(self.watch(adapter, client, seconds=0.4))
        self.assertEqual(client.calls, 0)


if __name__ == '__main__':
    unittest.main()
