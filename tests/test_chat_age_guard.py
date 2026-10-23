"""I50: GET /api/chat/ages reply format and its 23 h limit, the same limit the /api/send block uses."""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_i50_backend import HOUR, FakeStore, call, make_app  # noqa: E402

from notiontool import server as server_mod  # noqa: E402
from notiontool.coworker_age_guard import HARD_MAX_AGE_HOURS  # noqa: E402

KEYS = {'id', 'worker_id', 'thread_id', 'title', 'purpose', 'created_at', 'age_h', 'blocked'}


class ChatAgeGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = now = time.time()
        self.store = FakeStore(
            chats=[dict(id='c_main', main_worker='w_main', title='Main chat', created=now),
                   dict(id='c_edge', main_worker='', title='Edge', created=now - 23 * HOUR - 1),
                   dict(id='c_below', main_worker='', title='Below', created=now - 23 * HOUR + 120)],
            workers=[dict(id='w_main', thread_id='AAAA-BBBB', purpose='main', label='Main', created_at=now - 21 * HOUR),
                     dict(id='w_cw', thread_id='cccc1111', purpose='worker', label='Cw 1', created_at=now - 30 * HOUR),
                     dict(id='w_cw_new', thread_id='dddd2222', purpose='worker', created_at=(now - 2 * HOUR) * 1000),
                     dict(id='w_spare', thread_id='eeee3333', purpose='spare_main', created_at=now - 40 * HOUR),
                     dict(id='w_nothread', purpose='worker', created_at=now)])
        self.app = make_app(self.store)

    def ages(self):
        status, data = call(self.app, 'GET', '/api/chat/ages')
        self.assertEqual(status, 200)
        json.dumps(data)
        return data

    def test_reply_format(self):
        data = self.ages()
        self.assertEqual(set(data), {'warn_h', 'limit_h', 'chats'})
        self.assertEqual(data['warn_h'], 20)
        self.assertEqual(data['limit_h'], 23)
        self.assertEqual(data['limit_h'], HARD_MAX_AGE_HOURS)  # one limit for the badge and the send block
        self.assertIsInstance(data['chats'], list)
        for row in data['chats']:
            self.assertEqual(set(row), KEYS)

    def test_main_row_uses_the_worker_thread(self):
        row = {r['id']: r for r in self.ages()['chats']}['c_main']
        self.assertEqual((row['worker_id'], row['thread_id'], row['title'], row['purpose']),
                         ('w_main', 'AAAA-BBBB', 'Main chat', 'main'))
        self.assertAlmostEqual(row['age_h'], 21, delta=0.01)
        self.assertFalse(row['blocked'])

    def test_coworker_rows_once_each(self):
        rows = self.ages()['chats']
        self.assertEqual(sorted(r['id'] for r in rows), ['c_below', 'c_edge', 'c_main', 'w_cw', 'w_cw_new'])
        cw = {r['id']: r for r in rows}
        self.assertEqual((cw['w_cw']['purpose'], cw['w_cw']['thread_id'], cw['w_cw']['title']), ('worker', 'cccc1111', 'Cw 1'))
        self.assertTrue(cw['w_cw']['blocked'])
        self.assertAlmostEqual(cw['w_cw_new']['age_h'], 2, delta=0.01)  # Notion ms stamp
        self.assertEqual(cw['w_cw_new']['title'], 'Coworker')

    def test_age_h_is_age_s_over_3600_and_blocked_at_exactly_23_h(self):
        now = 1_000_000.0
        store = FakeStore(chats=[dict(id='a', created=now - 23 * HOUR), dict(id='b', created=now - 23 * HOUR + 1),
                                 dict(id='c', created=now - 5400), dict(id='d')])
        app = make_app(store)
        rows = {r['id']: r for r in server_mod.Application.chat_ages(app, now=now)['chats']}
        self.assertEqual((rows['a']['age_h'], rows['a']['blocked']), (23.0, True))
        self.assertFalse(rows['b']['blocked'])
        self.assertEqual(rows['c']['age_h'], 1.5)
        self.assertEqual((rows['d']['age_h'], rows['d']['created_at'], rows['d']['blocked']), (None, None, False))

    def test_blocked_matches_the_send_block(self):
        rows = {r['id']: r for r in self.ages()['chats']}
        for chat_id in ('c_main', 'c_edge', 'c_below'):
            status, _ = call(self.app, 'POST', '/api/send', {'chat_id': chat_id, 'text': 'hi'})
            self.assertEqual(status == 400, rows[chat_id]['blocked'], chat_id)
        self.assertTrue(rows['c_edge']['blocked'])
        self.assertFalse(rows['c_below']['blocked'])


if __name__ == '__main__':
    unittest.main()
