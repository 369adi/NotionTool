import unittest

from notiontool.chat_live import ChatLiveWatch, live_status

T = '00000000-0000-4000-8000-000000000001'
K = T.replace('-', '')


def rec(started, completed, msgs=10, updated=None, lease=None, steps=None):
    r = dict(id=T, latest_user_or_trigger_time=int(started * 1000), messages=['m%d' % i for i in range(msgs)],
             data=dict(last_turn_outcome=dict(completed_time=int(completed * 1000), step_count=steps)))
    if updated is not None:
        r['updated_time'] = int(updated * 1000)
    if lease is not None:
        r['current_inference_lease_expiration'] = str(int(lease * 1000))
    return r


class LiveStatusTest(unittest.TestCase):
    def test_working(self):
        st = live_status(rec(1000, 900, updated=1290, lease=3000), now=1300)
        self.assertEqual(st['state'], 'working')
        self.assertIn('last step 10 s ago', st['text'])

    def test_waiting_and_stuck(self):
        self.assertEqual(live_status(rec(1000, 900, updated=1000, lease=9000), now=1200)['state'], 'waiting')
        self.assertEqual(live_status(rec(1000, 900, updated=1000, lease=9000), now=1600)['state'], 'stuck')

    def test_lease_ran_out_is_stuck(self):
        st = live_status(rec(1000, 900, updated=1290, lease=1295), now=1300)
        self.assertEqual(st['state'], 'stuck')

    def test_done_then_idle(self):
        st = live_status(rec(1000, 1100, steps=46), now=1200)
        self.assertEqual(st['state'], 'done')
        self.assertIn('46 steps', st['text'])
        self.assertEqual(live_status(rec(1000, 1100), now=5000)['state'], 'idle')

    def test_bad_record(self):
        self.assertIsNone(live_status(None))


class WatchTest(unittest.TestCase):
    def test_counts_steps_and_detects_no_progress(self):
        w = ChatLiveWatch()
        w.update({T: rec(1000, 900, msgs=10, updated=1000, lease=99999)}, now=1010)
        out = w.update({T: rec(1000, 900, msgs=14, updated=1100, lease=99999)}, now=1100)
        self.assertEqual(out[K]['state'], 'working')
        self.assertEqual(out[K]['steps'], 4)
        # Notion keeps renewing updated_time, but no new message: still detected as no progress
        out = w.update({T: rec(1000, 900, msgs=14, updated=1700, lease=99999)}, now=1700)
        self.assertEqual(out[K]['state'], 'stuck')

    def test_new_turn_resets_steps(self):
        w = ChatLiveWatch()
        w.update({T: rec(1000, 900, msgs=10, updated=1000, lease=99999)}, now=1010)
        out = w.update({T: rec(2000, 1900, msgs=50, updated=2000, lease=99999)}, now=2005)
        self.assertEqual(out[K]['steps'], 0)
        self.assertEqual(out[K]['state'], 'working')

    def test_idle_threads_are_left_out(self):
        w = ChatLiveWatch()
        self.assertEqual(w.update({T: rec(1000, 1100)}, now=9000), {})


if __name__ == '__main__':
    unittest.main()
