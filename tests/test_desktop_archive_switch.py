"""Sidebar Archive (old chats after a context switch) and the Switch context button (2026-10-22)."""
import unittest

from notiontool import desktop_shell


class FakeStore:
    def __init__(self, workers, switches=()):
        self.workers = {w['id']: w for w in workers}
        self.switches = list(switches)

    def get(self, kind, ident, default=None):
        return self.workers.get(ident, default) if kind == 'workers' else default

    def all(self, kind):
        return list(self.workers.values()) if kind == 'workers' else self.switches if kind == 'main_switches' else []


class FakeServer:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def action(self, path, body):
        self.calls.append((path, dict(body)))
        return {'ok': True}


THREAD_A = '3fea1b13e54d805cbc4700a9af92c91f'
THREAD_B = '3fea1b13e54d805cbc4700a9af92c920'
THREAD_C = '3fea1b13e54d805cbc4700a9af92c921'


class ArchiveTests(unittest.TestCase):
    def test_archived_chats_newest_first_and_account_filter(self):
        workers = [
            dict(id='w1', purpose='worker', label='Cw4', account_id='a1', created=10, retired_threads=[THREAD_A, THREAD_B]),
            dict(id='w2', purpose='worker', label='Cw5', account_id='a2', created=20, retired_threads=[THREAD_C]),
        ]
        rows = desktop_shell.archived_chats(workers, 'a1')
        self.assertEqual([r['thread_id'] for r in rows], [THREAD_B, THREAD_A])
        self.assertEqual(rows[0]['name'], 'Cw4 - old 2')
        self.assertTrue(all(r['url'] for r in rows))
        self.assertNotIn('order', rows[0])
        self.assertEqual(len(desktop_shell.archived_chats(workers, '')), 3)

    def test_limit(self):
        threads = ['%032x' % i for i in range(60)]
        rows = desktop_shell.archived_chats([dict(id='w', purpose='worker', retired_threads=threads)], '')
        self.assertEqual(len(rows), desktop_shell.ARCHIVE_MAX)


class SwitchButtonTests(unittest.TestCase):
    def handlers(self, store):
        server = FakeServer(store)
        original = desktop_shell.notion_shell.default_handlers
        desktop_shell.notion_shell.default_handlers = lambda s: {}
        try:
            return server, desktop_shell.desktop_handlers(server)
        finally:
            desktop_shell.notion_shell.default_handlers = original

    def test_action_is_allowed(self):
        self.assertIn('chat_context_switch', desktop_shell.DESK_ACTIONS)

    def test_coworker_goes_to_compress_and_sends_nothing_else(self):
        server, h = self.handlers(FakeStore([dict(id='w1', purpose='worker')]))
        result = h['chat_context_switch']({'worker_id': 'w1'})
        self.assertEqual(result['kind'], 'coworker')
        self.assertEqual(server.calls, [('/api/chat/compress', {'worker_id': 'w1', 'reason': 'Context switch button'})])

    def test_main_sends_switch_request_with_chat_id(self):
        # Owner 2026-10-25 23:06: the button asks the Main itself to switch (no coworker brief task).
        server, h = self.handlers(FakeStore([]))
        result = h['chat_context_switch']({'chat_id': 'c1'})
        self.assertEqual(result['kind'], 'main')
        self.assertEqual(server.calls, [('/api/main/switch/request', {'chat_id': 'c1'})])

    def test_main_worker_id_goes_to_switch_request(self):
        server, h = self.handlers(FakeStore([dict(id='acc:t1', purpose='operator')]))
        result = h['chat_context_switch']({'worker_id': 'acc:t1'})
        self.assertEqual(result['kind'], 'main')
        self.assertEqual(server.calls, [('/api/main/switch/request', {'worker_id': 'acc:t1'})])

    def test_running_main_switch_is_not_started_twice(self):
        server, h = self.handlers(FakeStore([], [dict(id='s1', state='brief_requested')]))
        result = h['chat_context_switch']({'chat_id': 'c1'})
        self.assertTrue(result['duplicate'])
        self.assertEqual(server.calls, [])


if __name__ == '__main__':
    unittest.main()
