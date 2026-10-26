"""Switch context button -> switch request into the Main's own chat (owner 2026-10-25 23:06)."""
import unittest

from notiontool import main_wake, operator_main, switch_request

THREAD = '00000000-0000-4000-8000-0000000000a2'


class Store:
    def __init__(self, purpose='operator', switches=()):
        self.data = {
            'chats': {'c1': dict(id='c1', main_worker='acc:' + THREAD, state='active', operator=True)},
            'workers': {'acc:' + THREAD: dict(id='acc:' + THREAD, purpose=purpose, thread_id=THREAD, account_id='acc')},
            'main_switches': {s['id']: dict(s) for s in switches},
        }
        self.events = []

    def get(self, kind, ident, default=None):
        return self.data.get(kind, {}).get(ident, default)

    def all(self, kind):
        return [dict(r) for r in self.data.get(kind, {}).values()]

    def put(self, kind, record):
        self.data.setdefault(kind, {})[record['id']] = dict(record)
        return record

    def event(self, kind, ref='', message='', **fields):
        self.events.append((kind, ref, message, fields))


class Client:
    def __init__(self, status='idle'):
        self.status = status

    def transcript(self, thread, limit=5):
        return {'session': {'status': self.status}}


class Adapter:
    def __init__(self, status='idle'):
        self.c = Client(status)

    def client(self, account):
        return self.c


class SwitchRequestTests(unittest.TestCase):
    def setUp(self):
        self.original = operator_main.switch_chat_id
        operator_main.switch_chat_id = lambda store, body: 'c1'

    def tearDown(self):
        operator_main.switch_chat_id = self.original

    def deliver(self, store, status='idle', now=1000.0, send=None):
        sent = []
        send = send or (lambda client, thread, text: sent.append((thread, text)))
        ids = switch_request.deliver(store, Adapter(status), main_wake.MainWake.chat_busy, send, now)
        return ids, sent

    def test_request_queues_one_pending_record(self):
        store = Store()
        out = switch_request.request(store, {'chat_id': 'c1'}, now=1000.0)
        self.assertFalse(out['duplicate'])
        self.assertEqual(out['state'], 'pending')
        self.assertEqual(out['thread_id'], THREAD)
        self.assertEqual(out['account_id'], 'acc')
        self.assertTrue(out['id'].startswith('btn-'))
        self.assertEqual(len(store.all(switch_request.KIND)), 1)
        self.assertEqual(store.events[0][0], 'main_switch_requested')

    def test_second_press_is_a_duplicate(self):
        store = Store()
        first = switch_request.request(store, {'chat_id': 'c1'}, now=1000.0)
        second = switch_request.request(store, {'chat_id': 'c1'}, now=1010.0)
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(len(store.all(switch_request.KIND)), 1)

    def test_running_switch_is_a_duplicate(self):
        store = Store(switches=[dict(id='s1', state='prompt_sent')])
        out = switch_request.request(store, {'chat_id': 'c1'}, now=1000.0)
        self.assertTrue(out['duplicate'])
        self.assertEqual(store.all(switch_request.KIND), [])

    def test_coworker_chat_is_refused(self):
        with self.assertRaises(ValueError):
            switch_request.request(Store(purpose='worker'), {'chat_id': 'c1'}, now=1000.0)

    def test_idle_main_gets_the_message_exactly_once(self):
        store = Store()
        rid = switch_request.request(store, {'chat_id': 'c1'}, now=1000.0)['id']
        ids, sent = self.deliver(store, now=1005.0)
        self.assertEqual(ids, [rid])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], THREAD)
        self.assertEqual(store.get(switch_request.KIND, rid)['state'], 'sent')
        ids, sent = self.deliver(store, now=1020.0)
        self.assertEqual((ids, sent), ([], []))

    def test_message_tells_the_main_to_switch_itself(self):
        store = Store()
        rid = switch_request.request(store, {'chat_id': 'c1'}, now=1000.0)['id']
        _, sent = self.deliver(store)
        text = sent[0][1]
        self.assertTrue(text.startswith(switch_request.HEAD))
        for part in ('work/HANDOFF_MAIN_V6.md', '/api/main/switch/start', '"brief_text"', '"trigger": "user"',
                     '/api/main/switch/advance', '"switched"', '/api/account/refresh', 'never resend',
                     THREAD, rid, '"account_id": "acc"', 'without asking the user'):
            self.assertIn(part, text)

    def test_busy_main_waits(self):
        store = Store()
        rid = switch_request.request(store, {'chat_id': 'c1'}, now=1000.0)['id']
        ids, sent = self.deliver(store, status='running', now=1005.0)
        self.assertEqual((ids, sent), ([], []))
        self.assertEqual(store.get(switch_request.KIND, rid)['state'], 'pending')
        ids, sent = self.deliver(store, status='idle', now=1100.0)
        self.assertEqual(ids, [rid])

    def test_request_expires_after_30_minutes(self):
        store = Store()
        rid = switch_request.request(store, {'chat_id': 'c1'}, now=1000.0)['id']
        ids, sent = self.deliver(store, now=1000.0 + switch_request.EXPIRE_S + 1)
        self.assertEqual((ids, sent), ([], []))
        self.assertEqual(store.get(switch_request.KIND, rid)['state'], 'expired')

    def test_send_error_is_recorded_and_never_retried(self):
        store = Store()
        rid = switch_request.request(store, {'chat_id': 'c1'}, now=1000.0)['id']

        def boom(client, thread, text):
            raise RuntimeError('network down')
        ids, _ = self.deliver(store, send=boom)
        self.assertEqual(ids, [])
        record = store.get(switch_request.KIND, rid)
        self.assertEqual(record['state'], 'failed')
        self.assertIn('network down', record['error'])
        ids, sent = self.deliver(store, now=1100.0)
        self.assertEqual((ids, sent), ([], []))

    def test_switch_started_meanwhile_supersedes_the_request(self):
        store = Store()
        rid = switch_request.request(store, {'chat_id': 'c1'}, now=1000.0)['id']
        store.put('main_switches', dict(id='s2', state='brief_ready'))
        ids, sent = self.deliver(store)
        self.assertEqual((ids, sent), ([], []))
        self.assertEqual(store.get(switch_request.KIND, rid)['state'], 'superseded')

    def test_main_wake_delivers_with_its_own_sender(self):
        # MainWake._send keeps the user's model and effort (user_config); the tick runs it even when wakes are off.
        self.assertTrue(callable(getattr(main_wake.MainWake, 'switch_request_tick', None)))


if __name__ == '__main__':
    unittest.main()
