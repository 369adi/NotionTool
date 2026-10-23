"""I50 desktop send block: a send from the desktop message box (Notion's own composer, intercepted in
NotionShell._pin_request) into a chat 23 h or older is refused before it reaches Notion."""
import json
import unittest
from unittest import mock

from notiontool import desktop_shell, notion_shell

OLD = 'aaaabbbb-cccc-dddd-eeee-ffff00001111'
YOUNG = '1111222233334444555566667777aaaa'
OVER = 'cccc0000cccc0000cccc0000cccc0000'
AGES = {'api_ok': True, 'warn_h': 20, 'limit_h': 23, 'chats': [
    {'worker_id': 'w1', 'thread_id': 'AAAABBBBCCCCDDDDEEEEFFFF00001111', 'age_h': 23.0, 'blocked': True},
    {'worker_id': 'w2', 'thread_id': YOUNG, 'age_h': 2.0, 'blocked': False},
    {'worker_id': 'w3', 'thread_id': OVER, 'age_h': 30.0, 'blocked': False},
    {'worker_id': 'w4', 'thread_id': 'dddd', 'age_h': None, 'blocked': False}]}


class FakeSession:
    def __init__(self):
        self.sent = []

    def send(self, method, params=None, timeout=None):
        self.sent.append((method, params))
        return {}


def make_shell(source):
    shell = notion_shell.NotionShell.__new__(notion_shell.NotionShell)
    shell.session = FakeSession()
    shell.account_id = 'a1'
    shell.chat_ages_source = source
    return shell


def send(shell, thread, method='POST'):
    body = json.dumps({'threadId': thread, 'spaceId': 's1', 'transcript': [{'type': 'user', 'value': [['hi']]}]})
    with mock.patch.object(notion_shell, 'pin_log') as log:
        shell._pin_request({'requestId': 'r1', 'request': {
            'method': method, 'url': '/api/v3/runInferenceTranscript', 'postData': body}})
    return [m for m, _ in shell.session.sent], log


class DesktopSendBlockTests(unittest.TestCase):
    def test_send_into_23_h_chat_is_refused_before_notion(self):
        shell = make_shell(lambda: AGES)
        methods, log = send(shell, OLD)
        self.assertIn('Fetch.failRequest', methods)
        self.assertNotIn('Fetch.continueRequest', methods)
        failed = [p for m, p in shell.session.sent if m == 'Fetch.failRequest'][0]
        self.assertEqual(failed, {'requestId': 'r1', 'errorReason': 'BlockedByClient'})
        toast = [p for m, p in shell.session.sent if m == 'Runtime.evaluate'][0]['expression']
        self.assertIn('chat_too_old', toast)
        self.assertIn('Context Switch', toast)
        self.assertEqual(log.call_args[0][0]['event'], 'send_blocked_too_old')

    def test_age_over_limit_blocks_even_without_the_flag(self):
        methods, _ = send(make_shell(lambda: AGES), OVER)
        self.assertEqual(methods[0], 'Fetch.failRequest')
        self.assertNotIn('Fetch.continueRequest', methods)

    def test_young_unknown_or_unlisted_chats_go_through(self):
        for thread in (YOUNG, 'dddd', 'ffffeeee'):
            methods, _ = send(make_shell(lambda: AGES), thread)
            self.assertEqual(methods, ['Fetch.continueRequest'], thread)

    def test_no_or_failing_age_source_never_blocks(self):
        def broken():
            raise RuntimeError('backend down')
        for source in (None, broken, lambda: {'api_ok': False, 'chats': []}):
            methods, _ = send(make_shell(source), OLD)
            self.assertEqual(methods, ['Fetch.continueRequest'])

    def test_get_requests_are_not_checked(self):
        source = mock.Mock(return_value=AGES)
        methods, _ = send(make_shell(source), OLD, method='GET')
        self.assertEqual(methods, ['Fetch.continueRequest'])
        source.assert_not_called()

    def test_helpers(self):
        self.assertEqual(notion_shell.request_thread_id(json.dumps({'threadId': OLD})), OLD.replace('-', ''))
        self.assertEqual(notion_shell.request_thread_id('not json'), '')
        self.assertIn('limit 23 h', notion_shell.send_block_reason(AGES, OLD.replace('-', '')))
        self.assertEqual(notion_shell.send_block_reason(AGES, YOUNG), '')
        self.assertEqual(notion_shell.send_block_reason(None, YOUNG), '')

    def test_desktop_shell_wires_the_chat_ages_action(self):
        calls = []

        def host(method, path, body=None):
            calls.append(path)
            return dict(AGES)

        class Server:
            class store:
                @staticmethod
                def get(kind, ident, default=None):
                    return default

                @staticmethod
                def all(kind):
                    return []
            port = 8790
            token = 'tok'

        original = desktop_shell.notion_shell.default_handlers
        desktop_shell.notion_shell.default_handlers = lambda s: {}
        try:
            handlers = desktop_shell.desktop_handlers(Server(), host_call=host)
        finally:
            desktop_shell.notion_shell.default_handlers = original
        shell = desktop_shell.DesktopShell.__new__(desktop_shell.DesktopShell)
        desktop_shell.DesktopShell.__init__(shell, None, 'a1', handlers, executor=mock.Mock())
        self.assertTrue(callable(shell.chat_ages_source))
        shell.session = FakeSession()
        methods, _ = send(shell, OLD)
        self.assertIn('/api/chat/ages', calls)
        self.assertIn('Fetch.failRequest', methods)
        self.assertNotIn('Fetch.continueRequest', methods)


if __name__ == '__main__':
    unittest.main()
