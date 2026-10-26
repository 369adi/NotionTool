"""RBX bug (2026-10-25): the age guard covers ANY Notion chat, also own chats NotionTool did not create.

The age always comes from the Notion thread record's created_time (adapter.thread_records), never from the
NotionTool chat/worker record. 21 h: send ok + warning. 22 h: automatic switch, the fresh chat keeps the
name, the old one is renamed '<title> (expired)'. 23 h: /api/send and the own-chat send path return 400 and
nothing reaches Notion.
"""
import json
import time
import unittest
from unittest import mock

from notiontool import foreign_chat_guard as guard
from notiontool import notion_shell

try:
    from test_i50_backend import HOUR, FakeAdapter, FakeStore, call, make_app
except ImportError:  # run from the repo root
    from tests.test_i50_backend import HOUR, FakeAdapter, FakeStore, call, make_app

RBX = '2a7dac2a-b9fd-80a1-9c3e-00a9aabbccdd'
MAIN = '5b1dac2a-b9fd-80a1-9c3e-00a9ddeeff00'


class NotionFake(FakeAdapter):
    """Fake Notion: the thread record's created_time decides the age; renames are recorded."""

    def __init__(self, created_s, chats=(), title='RBX'):
        FakeAdapter.__init__(self, listed=True)
        self.created_s = created_s
        self.created = {}  # thread key -> created seconds (default created_s)
        self.lookups = []
        self.chats = list(chats)  # the account's Notion AI chat list (client.threads)
        self.titles = {}  # thread key -> current Notion title
        self.base_title = title
        self.drop_titles = {}  # title -> renames Notion silently ignores
        self.fail_lookups = 0
        self.list_error = False

    def client(self, account_id):
        client = FakeAdapter.client(self, account_id)

        def threads():
            if self.list_error:
                raise OSError('chat list unavailable')
            return [{'id': t} for t in self.chats]
        client.threads = threads
        return client

    def rename_thread(self, client, tid, title):
        FakeAdapter.rename_thread(self, client, tid, title)
        if self.drop_titles.get(title):
            self.drop_titles[title] -= 1
            return
        self.titles[guard.thread_key(tid)] = title

    def thread_records(self, client, ids):
        self.lookups.append(list(ids))
        if self.fail_lookups:
            self.fail_lookups -= 1
            raise OSError('Notion unreachable')
        return {i: {'created_time': int(self.created.get(guard.thread_key(i), self.created_s) * 1000),
                    'data': {'title': self.titles.get(guard.thread_key(i), self.base_title)}} for i in ids}


def own_store(now, worker_created=None):
    worker = dict(id='w_rbx', account_id='a1', purpose='private', state='ready', thread_id=RBX, label='RBX',
                  created_at=now if worker_created is None else worker_created)
    return FakeStore(workers=[worker], accounts=[dict(id='a1', state='ready')])


class AgeSourceTests(unittest.TestCase):
    def test_age_comes_from_the_notion_created_time(self):
        now = time.time()
        store = own_store(now, worker_created=now)  # the NotionTool record looks brand new
        notion = NotionFake(now - 23.5 * HOUR)
        info = guard.age_of(store, notion, RBX, now, 'a1')
        self.assertEqual(info['source'], 'notion')
        self.assertAlmostEqual(info['age_h'], 23.5, delta=0.01)
        self.assertEqual(info['level'], 'locked')
        self.assertTrue(notion.lookups)

    def test_old_record_does_not_win_over_a_young_notion_thread(self):
        now = time.time()
        store = own_store(now, worker_created=now - 30 * HOUR)
        info = guard.age_of(store, NotionFake(now - 1 * HOUR), RBX, now, 'a1')
        self.assertEqual((info['source'], info['level']), ('notion', 'ok'))

    def test_rbx_real_created_time_is_locked(self):
        created_ms = 1792851361007  # RBX, 2026-10-24 ~16:16 Berlin
        store = own_store(0)
        info = guard.age_of(store, NotionFake(created_ms / 1000.0), RBX, created_ms / 1000.0 + 25 * HOUR, 'a1')
        self.assertEqual(info['level'], 'locked')

    def test_switch_point_never_after_22_h(self):
        self.assertEqual(guard.switch_hours({'coworker_max_age_hours': 40}), 22)
        self.assertEqual(guard.switch_hours({'coworker_max_age_hours': 10}), 10)


class OwnChatLevelsTests(unittest.TestCase):
    def test_21_h_send_ok_with_warning_and_no_switch(self):
        now = time.time()
        store = own_store(now)
        notion = NotionFake(now - 21 * HOUR)
        result = guard.send_check(store, notion, RBX, now, 'a1')
        self.assertTrue(result['allowed'])
        self.assertEqual(result['level'], 'warn')
        self.assertIn('Auto-switch', result['warning'])
        guard.sweep(store, notion, now)
        self.assertEqual(store.get('workers', 'w_rbx')['thread_id'], RBX)
        self.assertEqual(notion.renamed, [])

    def test_21_h_through_the_route(self):
        now = time.time()
        store = own_store(now)
        app = make_app(store)
        app.adapter = NotionFake(now - 21 * HOUR)
        app.app_chat_inline = True
        status, data = call(app, 'POST', '/api/chat/send_check', {'thread_id': RBX, 'send': True, 'account_id': 'a1'})
        self.assertEqual(status, 200)
        self.assertTrue(data['allowed'])
        self.assertTrue(data['warning'])

    def test_22_h_automatic_switch_keeps_the_name_and_expires_the_old_chat(self):
        now = time.time()
        store = own_store(now)
        notion = NotionFake(now - 22.2 * HOUR)
        levels = guard.sweep(store, notion, now)  # one turn_watch cycle
        self.assertEqual(levels[guard.thread_key(RBX)], 'switching')
        worker = store.get('workers', 'w_rbx')
        self.assertEqual(guard.thread_key(worker['thread_id']), FakeAdapter.NEW)
        self.assertEqual(worker['label'], 'RBX')
        self.assertEqual(worker['purpose'], 'private')
        self.assertIn('Continuation checkpoint', worker['handoff'])
        self.assertIn((FakeAdapter.NEW, 'RBX'), notion.renamed)
        self.assertIn((RBX, 'RBX (expired)'), notion.renamed)
        self.assertFalse([t for t in notion.renamed if t[0] == RBX and t[1] != 'RBX (expired)'])
        self.assertTrue(store.get(guard.KIND, guard.thread_key(RBX))['expired'])
        self.assertIn('own_chat_context_switch', store.events)

    def test_busy_own_chat_waits_for_the_turn(self):
        now = time.time()
        store = own_store(now)
        store.put('tasks', dict(id='t1', worker_id='w_rbx', state='running'))
        notion = NotionFake(now - 22.5 * HOUR)
        guard.sweep(store, notion, now)
        self.assertEqual(store.get('workers', 'w_rbx')['thread_id'], RBX)


class HardBlockTests(unittest.TestCase):
    def test_api_send_23_h_by_notion_time_is_400_and_never_reaches_the_engine(self):
        now = time.time()
        store = FakeStore(chats=[dict(id='c_main', main_worker='w_main', created=now, title='Main 7')],
                          workers=[dict(id='w_main', account_id='a1', purpose='main', state='ready',
                                        thread_id=MAIN, created_at=now)],
                          accounts=[dict(id='a1', state='ready')])
        app = make_app(store)
        app.adapter = NotionFake(now - 23.1 * HOUR)
        status, data = call(app, 'POST', '/api/send', {'chat_id': 'c_main', 'text': 'hello'})
        self.assertEqual(status, 400)
        self.assertIn('chat_too_old', data['error'])
        self.assertIn(guard.BLOCK_MESSAGE, data['error'])
        app.engine.submit.assert_not_called()

    def test_own_chat_send_path_23_h_is_400_and_starts_the_switch(self):
        now = time.time()
        store = own_store(now)
        app = make_app(store)
        notion = NotionFake(now - 25 * HOUR)
        app.adapter = notion
        app.app_chat_inline = True
        status, data = call(app, 'POST', '/api/chat/send_check', {'thread_id': RBX, 'send': True, 'account_id': 'a1'})
        self.assertEqual(status, 400)
        self.assertIn(guard.BLOCK_MESSAGE, data['error'])
        app.engine.submit.assert_not_called()
        self.assertIn((RBX, 'RBX (expired)'), notion.renamed)
        self.assertEqual(store.get('workers', 'w_rbx')['label'], 'RBX')

    def test_check_without_send_reports_locked(self):
        now = time.time()
        app = make_app(own_store(now))
        app.adapter = NotionFake(now - 24 * HOUR)
        status, data = call(app, 'POST', '/api/chat/send_check', {'thread_id': RBX, 'send': False, 'account_id': 'a1'})
        self.assertEqual(status, 200)
        self.assertFalse(data['allowed'])
        self.assertEqual(data['level'], 'locked')

    def test_embedded_composer_never_passes_the_text_to_notion(self):
        class Session:
            def __init__(self):
                self.sent = []

            def send(self, method, params=None, timeout=None):
                self.sent.append((method, params))
                return {}

        shell = notion_shell.NotionShell.__new__(notion_shell.NotionShell)
        shell.session = Session()
        shell.account_id = 'a1'
        shell.chat_ages_source = lambda: {'api_ok': True, 'warn_h': 20, 'limit_h': 23, 'chats': []}  # RBX unlisted
        shell.thread_age_source = lambda thread: dict(allowed=False, level='locked', message=guard.BLOCK_MESSAGE)
        body = json.dumps({'threadId': RBX, 'spaceId': '00000000-0000-4000-8000-0000000000a1',
                           'transcript': [{'type': 'user', 'value': [['hi']]}]})
        with mock.patch.object(notion_shell, 'pin_log'):
            shell._pin_request({'requestId': 'r1', 'request': {
                'method': 'POST', 'url': '/api/v3/runInferenceTranscript', 'postData': body}})
        methods = [m for m, _ in shell.session.sent]
        self.assertIn('Fetch.failRequest', methods)
        self.assertNotIn('Fetch.continueRequest', methods)
        toast = [p for m, p in shell.session.sent if m == 'Runtime.evaluate'][0]['expression']
        self.assertIn('This chat is older than 23 h', toast)


class Session:
    def __init__(self):
        self.sent = []

    def send(self, method, params=None, timeout=None):
        self.sent.append((method, params))
        return {}


def foreign_store():
    """An account with NO NotionTool record of the chat: the user's own chat RBX before the fix."""
    return FakeStore(accounts=[dict(id='a1', state='ready')])


def worker_of(store, thread_id):
    key = guard.thread_key(thread_id)
    return next((w for w in store.all('workers') if guard.thread_key(w.get('thread_id')) == key), None)


class ForeignDiscoveryTests(unittest.TestCase):
    def test_sweep_adopts_an_unknown_21_h_chat_without_switching(self):
        now = time.time()
        store, notion = foreign_store(), NotionFake(now - 21 * HOUR, chats=[RBX])
        levels = guard.sweep(store, notion, now)
        worker = worker_of(store, RBX)
        self.assertEqual((worker['label'], worker['purpose']), ('RBX', 'private'))
        self.assertEqual(levels[guard.thread_key(RBX)], 'warn')
        self.assertEqual(notion.renamed, [])

    def test_sweep_adopts_an_unknown_22_2_h_chat_and_switches_it(self):
        now = time.time()
        store, notion = foreign_store(), NotionFake(now - 22.2 * HOUR, chats=[RBX])
        notion.created[FakeAdapter.NEW] = now
        levels = guard.sweep(store, notion, now)
        self.assertEqual(levels[guard.thread_key(RBX)], 'switching')
        worker = worker_of(store, FakeAdapter.NEW)
        self.assertEqual(worker['label'], 'RBX')
        self.assertIn((FakeAdapter.NEW, 'RBX'), notion.renamed)
        self.assertIn((RBX, 'RBX (expired)'), notion.renamed)
        self.assertTrue(store.get(guard.KIND, guard.thread_key(RBX))['rename_ok'])

    def test_too_old_or_expired_list_entries_are_not_adopted(self):
        now = time.time()
        store = foreign_store()
        guard.sweep(store, NotionFake(now - 30 * HOUR, chats=[RBX]), now)
        self.assertIsNone(worker_of(store, RBX))
        notion = NotionFake(now - 22.2 * HOUR, chats=[MAIN], title='Main 3 (expired)')
        guard.sweep(store, notion, now)
        self.assertIsNone(worker_of(store, MAIN))
        self.assertEqual(notion.renamed, [])

    def test_a_failed_chat_list_is_logged(self):
        now = time.time()
        store, notion = foreign_store(), NotionFake(now - 21 * HOUR, chats=[RBX])
        notion.list_error = True
        guard.sweep(store, notion, now)
        self.assertIn('foreign_chat_list_failed', store.events)

    def test_send_check_adopts_an_unknown_chat(self):
        now = time.time()
        store = foreign_store()
        result = guard.send_check(store, NotionFake(now - 21 * HOUR), RBX, now, 'a1')
        self.assertTrue(result['allowed'])
        self.assertEqual(worker_of(store, RBX)['label'], 'RBX')


class UnknownAgeTests(unittest.TestCase):
    def test_unknown_age_blocks_and_the_lookup_is_retried(self):
        now = time.time()
        store, notion = foreign_store(), NotionFake(now - 21 * HOUR)
        notion.fail_lookups = 1
        first = guard.send_check(store, notion, RBX, now, 'a1')
        self.assertFalse(first['allowed'])
        self.assertEqual((first['level'], first['message']), ('unknown', guard.UNKNOWN_MESSAGE))
        again = guard.send_check(store, notion, RBX, now + guard.RETRY_MIN_S + 1, 'a1')
        self.assertTrue(again['allowed'])
        self.assertEqual(again['level'], 'warn')
        self.assertEqual(worker_of(store, RBX)['label'], 'RBX')

    def test_route_refuses_a_send_with_unknown_age(self):
        now = time.time()
        app = make_app(foreign_store())
        notion = NotionFake(now - 21 * HOUR)
        notion.fail_lookups = 10
        app.adapter = notion
        app.app_chat_inline = True
        status, data = call(app, 'POST', '/api/chat/send_check', {'thread_id': RBX, 'send': True, 'account_id': 'a1'})
        self.assertEqual(status, 400)
        self.assertIn('chat_age_unknown', data['error'])
        self.assertIn(guard.UNKNOWN_MESSAGE, data['error'])
        app.engine.submit.assert_not_called()

    def test_background_senders_stay_lenient_on_unknown_age(self):
        now = time.time()
        notion = NotionFake(now - 21 * HOUR)
        notion.fail_lookups = 10
        guard.require_young(own_store(now), notion, RBX, now, 'a1')  # must not raise

    def test_embedded_composer_blocks_when_the_check_fails(self):
        shell = notion_shell.NotionShell.__new__(notion_shell.NotionShell)
        shell.session, shell.account_id = Session(), 'a1'
        shell.chat_ages_source = lambda: {'api_ok': True, 'warn_h': 20, 'limit_h': 23, 'chats': []}

        def broken(thread):
            raise OSError('host api down')
        shell.thread_age_source = broken
        body = json.dumps({'threadId': RBX, 'transcript': [{'type': 'user', 'value': [['hi']]}]})
        with mock.patch.object(notion_shell, 'pin_log'):
            shell._pin_request({'requestId': 'r1', 'request': {
                'method': 'POST', 'url': '/api/v3/runInferenceTranscript', 'postData': body}})
        methods = [m for m, _ in shell.session.sent]
        self.assertIn('Fetch.failRequest', methods)
        self.assertNotIn('Fetch.continueRequest', methods)
        toast = [p for m, p in shell.session.sent if m == 'Runtime.evaluate'][0]['expression']
        self.assertIn('Chat age unknown', toast)


class RenameRecheckTests(unittest.TestCase):
    def test_a_rename_that_did_not_stick_is_logged_and_retried(self):
        now = time.time()
        store, notion = own_store(now), NotionFake(now - 22.2 * HOUR)
        notion.created[FakeAdapter.NEW] = now
        notion.drop_titles = {'RBX (expired)': 1}  # Notion ignores the first rename
        guard.sweep(store, notion, now)
        key = guard.thread_key(RBX)
        self.assertFalse(store.get(guard.KIND, key).get('rename_ok'))
        self.assertIn('expired_rename_failed', store.events)
        guard.sweep(store, notion, now + 60)  # throttled: no rename storm
        self.assertEqual(notion.renamed.count((RBX, 'RBX (expired)')), 1)
        guard.sweep(store, notion, now + guard.RENAME_RETRY_S + 1)
        self.assertTrue(store.get(guard.KIND, key)['rename_ok'])
        self.assertEqual(notion.titles[key], 'RBX (expired)')
        self.assertIn('expired_rename_fixed', store.events)


class SendPathTests(unittest.TestCase):
    # Senders that only write into a chat they create in the same call (no age yet), the context-switch
    # summary turn (it is what moves an old chat away), transports and the fake.
    EXCLUDED = {('notion.py', 'create_welcome'), ('mains.py', 'run_create'), ('chat_context.py', '*'),
                ('edge_transport.py', '*'), ('fake_notion.py', '*')}

    def test_every_runinferencetranscript_sender_is_guarded(self):
        import pathlib
        import re
        root = pathlib.Path(guard.__file__).parent
        missing = []
        for path in sorted(root.glob('*.py')):
            lines = path.read_text(encoding='utf-8').splitlines()
            for i, line in enumerate(lines):
                if 'runInferenceTranscript' not in line or not re.search(r'\.(stream|call)\(', line):
                    continue
                # the enclosing module function or method (nested helpers such as remote_done are skipped)
                start = max(j for j in range(i + 1) if re.match(r'(    )?def ', lines[j]) or j == 0)
                name = (re.findall(r'def (\w+)', lines[start]) or [''])[0]
                if {(path.name, name), (path.name, '*')} & self.EXCLUDED:
                    continue
                if not any(w in '\n'.join(lines[start:i + 1]) for w in ('require_young', 'guard_body')):
                    missing.append('%s:%d %s' % (path.name, i + 1, name))
        self.assertEqual(missing, [])

    def test_main_wake_never_sends_into_a_23_h_chat(self):
        from notiontool import main_wake
        now = time.time()
        notion = NotionFake(now - 23.5 * HOUR)
        notion.body = mock.Mock(return_value={})
        notion.new_id = lambda *args: FakeAdapter.NEW
        wake = main_wake.MainWake(own_store(now), notion, mock.Mock())
        client = mock.Mock(info={'space_id': 'space1'}, account_id='a1')
        with mock.patch('notiontool.notion.user_config', return_value=('m', 'high')):
            with self.assertRaises(ValueError) as ctx:
                wake._send(client, RBX, 'wake up')
        self.assertIn('chat_too_old', str(ctx.exception))
        client.transport.stream.assert_not_called()

    def test_raw_operator_body_is_guarded(self):
        now = time.time()
        notion = NotionFake(now - 25 * HOUR)
        with self.assertRaises(ValueError):
            guard.guard_body(own_store(now), notion, {'threadId': RBX, 'transcript': []}, 'a1')
        guard.guard_body(own_store(now), notion, {'threadId': RBX, 'createThread': True}, 'a1')  # new chat


class ComposerWiringTests(unittest.TestCase):
    def _shell(self, host_call):
        from notiontool import desktop_shell
        server = mock.Mock()
        server.store = own_store(time.time())
        handlers = desktop_shell.desktop_handlers(server, host_call=host_call)
        shell = desktop_shell.DesktopShell(None, 'a1', handlers)
        shell.session = Session()
        return shell

    def _send(self, shell):
        body = json.dumps({'threadId': RBX, 'transcript': [{'type': 'user', 'value': [['hi']]}]})
        with mock.patch.object(notion_shell, 'pin_log'):
            shell._pin_request({'requestId': 'r1', 'request': {
                'method': 'POST', 'url': '/api/v3/runInferenceTranscript', 'postData': body}})
        return [m for m, _ in shell.session.sent]

    def _host(self, status, text):
        from notiontool import desktop_shell
        calls = []

        def host_call(method, path, body=None):
            calls.append(path)
            if path == desktop_shell.CHAT_SEND_CHECK_PATH:
                raise desktop_shell.HostApiError(status, text)
            return {'api_ok': True, 'warn_h': 20, 'limit_h': 23, 'chats': []}
        return host_call, calls

    def test_real_handler_400_chat_too_old_fails_the_request(self):
        from notiontool import desktop_shell
        host_call, calls = self._host(400, 'chat_too_old: ' + guard.BLOCK_MESSAGE)
        shell = self._shell(host_call)
        methods = self._send(shell)
        self.assertIn(desktop_shell.CHAT_SEND_CHECK_PATH, calls)
        self.assertIn('Fetch.failRequest', methods)
        self.assertNotIn('Fetch.continueRequest', methods)

    def test_missing_route_or_broken_check_fails_the_request(self):
        for status in (404, 0, 500):
            host_call, _ = self._host(status, 'not available')
            methods = self._send(self._shell(host_call))
            self.assertIn('Fetch.failRequest', methods, status)
            self.assertNotIn('Fetch.continueRequest', methods, status)


if __name__ == '__main__':
    unittest.main()
