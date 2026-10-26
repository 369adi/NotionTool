"""Hard limits for NotionTool tool calls (live 25.10.: nt_job_result 'tool running, no result yet' 20+ min)."""
import subprocess
import sys
import threading
import time
import unittest

from notiontool import long_calls
from notiontool.bridge import bounded_lock
from notiontool.turn_watch import PENDING_TOOL_SECONDS, STALL_SECONDS, interrupted_turn

USER = {'id': 'u1', 'kind': 'user_message', 'content': 'do it', 'sequence': 1, 'created_at': 1000}
TOOL = {'id': 't1', 'kind': 'tool_call', 'tool_use_id': 'x', 'sequence': 2, 'created_at': 1010}
META = {'id': 'm1', 'kind': 'turn_metadata', 'sequence': 3, 'created_at': 1300}


def transcript(status, entities):
    return {'session': {'status': status},
            'patches': [{'op': 'put', 'entity': e} for e in entities]}


class JobHardLimitTests(unittest.TestCase):
    def test_job_past_deadline_returns_error_with_last_output(self):
        jobs, gate = long_calls.Jobs(), threading.Event()

        def hang():
            long_calls.output_sink()('step 1 done\nstep 2 ...')
            gate.wait(10)
            return 'late'
        first = jobs.run('c', 'nt_shell', {'command': 'x'}, hang, limit=0.05, deadline=0.3)
        self.assertTrue(first['still_running'])
        start = time.monotonic()
        with self.assertRaises(long_calls.JobLost) as caught:
            jobs.result('c', first['job_id'], wait_s=50)
        self.assertLess(time.monotonic() - start, 2.0)  # never the full 50 s, never forever
        self.assertIn('step 2', str(caught.exception))
        self.assertIn('did NOT start it again', str(caught.exception))
        gate.set()
        time.sleep(0.1)  # the late finish must not replace the error
        with self.assertRaises(long_calls.JobLost):
            jobs.result('c', first['job_id'], wait_s=0)

    def test_lost_thread_returns_error(self):
        jobs = long_calls.Jobs()
        first = jobs.run('c', 'nt_shell', {}, lambda: time.sleep(0.2) or 'ok', limit=0)
        job = jobs._jobs[first['job_id']]
        job['thread'].join()
        job['done'].clear()  # simulate a thread that died without writing a result
        job['result'] = None
        with self.assertRaises(long_calls.JobLost) as caught:
            jobs.result('c', first['job_id'], wait_s=1)
        self.assertIn('thread was lost', str(caught.exception))

    def test_job_deadline_follows_tool_timeout(self):
        self.assertEqual(long_calls.job_deadline({}), long_calls.JOB_HARD_S)
        self.assertEqual(long_calls.job_deadline({'timeout': 600}), 660)
        self.assertEqual(long_calls.job_deadline({'arguments': {'timeout': 900}}), 960)
        self.assertEqual(long_calls.job_deadline({'timeout': 10 ** 6}), 1860)
        self.assertEqual(long_calls.job_deadline({'timeout': True}), long_calls.JOB_HARD_S)
        self.assertEqual(long_calls.job_deadline({'timeout': 'nan'}), long_calls.JOB_HARD_S)

    def test_communicate_is_bounded_and_keeps_output(self):
        process = subprocess.Popen([sys.executable, '-c', "import time;print('hi',flush=True);time.sleep(30)"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        start = time.monotonic()
        out, err, timed_out = long_calls.communicate(process, 1.5)
        self.assertTrue(timed_out)
        self.assertIn(b'hi', out)
        self.assertLess(time.monotonic() - start, 1.5 + 2 * long_calls.KILL_WAIT_S)

    def test_communicate_normal_exit(self):
        process = subprocess.Popen([sys.executable, '-c', "print('ok')"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err, timed_out = long_calls.communicate(process, 20)
        self.assertFalse(timed_out)
        self.assertEqual(out.strip(), b'ok')
        self.assertEqual(process.returncode, 0)


class BoundedLockTests(unittest.TestCase):
    def test_held_lock_gives_clear_error(self):
        lock = threading.RLock()
        held = threading.Event()
        threading.Thread(target=lambda: (lock.acquire(), held.set(), time.sleep(2), lock.release()), daemon=True).start()
        held.wait(2)
        with self.assertRaises(ValueError) as caught:
            with bounded_lock(lock, 0.2):
                pass
        self.assertIn('nothing was changed', str(caught.exception))

    def test_free_lock_works(self):
        lock = threading.Lock()
        with bounded_lock(lock, 0.2):
            self.assertTrue(lock.locked())
        self.assertFalse(lock.locked())


class HungToolWatchTests(unittest.TestCase):
    def test_pending_tool_with_newer_metadata_is_hung(self):
        # Before: newer metadata kept the turn 'active', so nothing happened until STALL_SECONDS of total silence.
        found = interrupted_turn(transcript('running', [USER, TOOL, META]), now=1010 + PENDING_TOOL_SECONDS + 1)
        self.assertIsNotNone(found)
        self.assertTrue(found['hung_tool'])
        self.assertTrue(found['pending'])
        self.assertEqual(found['key'], 'hung:t1')
        self.assertEqual(found['stamp'], 1010)

    def test_young_pending_tool_is_left_alone(self):
        self.assertIsNone(interrupted_turn(transcript('running', [USER, TOOL, META]), now=1010 + 60))

    def test_quiet_turn_keeps_stall_rule(self):
        found = interrupted_turn(transcript('running', [USER, TOOL]), now=1010 + STALL_SECONDS + 1)
        self.assertFalse(found['hung_tool'])
        self.assertFalse(found['pending'])


# ---- live 25.10. 23:26: session idle + newest step a tool call without result = cut turn ----------------------
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from notiontool import bridge as bridge_mod, notion as notion_mod, turn_watch  # noqa: E402
from notiontool.engine import Engine  # noqa: E402
from notiontool.notion import NotionAdapter, cut_tool  # noqa: E402
from notiontool.store import Store  # noqa: E402
from tests import test_resolve_uncertain as ru, test_turn_watch as tw  # noqa: E402

PROMPT = 'ORIGINAL PROMPT of the coworker task'
OPEN = dict(TOOL, kind='mcp_run_tool', name='NotionTool / nt_read_file', interaction_status='pending',
            started_at='2026-10-25T22:26:40Z')


def adapter_on(store):
    adapter = NotionAdapter.__new__(NotionAdapter)  # only the store-backed helpers are used
    adapter.store = store
    return adapter


class CutToolRuleTests(unittest.TestCase):
    def test_idle_session_with_open_tool_is_cut(self):
        for status in ('idle', 'completed'):
            self.assertEqual(cut_tool(transcript(status, [USER, OPEN]), [OPEN])['id'], 't1')

    def test_running_pending_answered_or_approval_is_not_cut(self):
        self.assertIsNone(cut_tool(transcript('running', [USER, OPEN]), [OPEN]))
        self.assertIsNone(cut_tool(dict(transcript('idle', [USER, OPEN]), session_pending=True), [OPEN]))
        answered = [OPEN, {'id': 'r1', 'kind': 'tool_result', 'tool_use_id': 'x', 'sequence': 3}]
        self.assertIsNone(cut_tool(transcript('idle', [USER] + answered), answered))
        approval = dict(OPEN, requires_approval=True)
        self.assertIsNone(cut_tool(transcript('idle', [USER, approval]), [approval]))
        reply = [OPEN, {'id': 'a1', 'kind': 'assistant_message', 'sequence': 3}]
        self.assertIsNone(cut_tool(transcript('idle', [USER] + reply), reply))

    def test_old_rule_switch(self):
        notion_mod.CUT_PENDING_TOOL = False
        try:
            self.assertIsNone(cut_tool(transcript('idle', [USER, OPEN]), [OPEN]))
        finally:
            notion_mod.CUT_PENDING_TOOL = True

    def test_note_cut_turn_sets_remote_done_once_and_resume_clears_it(self):
        store = tw.FakeStore()
        store.put('tasks', {'id': 'tk', 'run_id': 'r', 'state': 'uncertain'})
        adapter = adapter_on(store)
        adapter.note_cut_turn({'id': 'tk', 'run_id': 'r'}, OPEN)
        first = store.get('tasks', 'tk')
        self.assertTrue(first['remote_done_at'])
        self.assertIs(first['cut_pending_tool'], True)
        adapter.note_cut_turn({'id': 'tk', 'run_id': 'r'}, OPEN)  # the next reconcile read: no second event
        self.assertEqual([k for k, _ in store.events].count('turn_cut_pending_tool'), 1)
        self.assertEqual(store.get('tasks', 'tk')['remote_done_at'], first['remote_done_at'])
        adapter.note_cut_resumed({'id': 'tk', 'run_id': 'r'}, transcript('running', [USER, OPEN]))
        self.assertFalse(store.get('tasks', 'tk')['remote_done_at'])
        self.assertIn('turn_cut_resumed', [k for k, _ in store.events])


class CutTurnResolveTests(unittest.TestCase):
    """T2: after the cut, the Main can abandon or accept; the prompt is sent 0 times."""
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.adapter = ru.NoSendAdapter()
        self.store.put('accounts', dict(id='a', slot=1, email='t@example.test', state='ready', cooldown_until=0))
        self.store.put('chats', dict(id='c', title='Main', main_worker='m', folder_id='f', project_path=self.temp.name))
        self.store.put('runs', dict(id='r1', chat_id='c', state='executing', operator=True, project_path=self.temp.name,
                                    error='', capacity=2, worker_budget=2, fixes=0, main_worker='m',
                                    created=time.time(), updated=time.time()))
        self.engine = Engine(self.store, self.adapter)
        Path(self.temp.name, 'a.py').write_text('x = 1\n', encoding='utf-8')
        self.store.put('workers', dict(id='w1', account_id='a', thread_id='w1', purpose='worker', enabled=True,
                                       state='recovering', lease='L1', label='w1', last_used=0))
        self.store.put('tasks', dict(id='t1', run_id='r1', role='executor', title='Gate', prompt=PROMPT,
                                     state='uncertain', result='', deps=[], files=['a.py'], attempts=1, worker_id='w1',
                                     lease='L1', side_effect_hold=True, reconcile_checks=3, sent_at=time.time() - 900,
                                     reconcile_at=time.time() + 3600, pending_tool='nt_read_file'))

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def test_before_the_cut_abandon_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.engine.resolve_uncertain('t1', 'abandon', 'hung')
        self.assertIn('Notion has not confirmed that the turn finished', str(caught.exception))

    def test_cut_turn_can_be_abandoned_and_accepted_without_resend(self):
        adapter_on(self.store).note_cut_turn(self.store.get('tasks', 't1'), OPEN)
        self.assertEqual(self.engine.resolve_uncertain('t1', 'abandon', 'cut turn')['state'], 'failed')
        self.assertIn('nothing was resent', self.store.get('tasks', 't1')['error'])
        task = dict(self.store.get('tasks', 't1'), state='uncertain', lease='L1', remote_done_at=0, cut_pending_tool=False)
        self.store.put('tasks', task)
        self.store.put('workers', dict(self.store.get('workers', 'w1'), state='recovering', lease='L1'))
        adapter_on(self.store).note_cut_turn(self.store.get('tasks', 't1'), dict(OPEN, id='t2'))
        self.assertEqual(self.engine.resolve_uncertain('t1', 'accept', 'checked')['state'], 'done')
        self.assertEqual(self.adapter.calls, [])  # the original prompt was sent 0 times


class IdleOpenToolWatchTests(unittest.TestCase):
    """T3: the turn watch continues a cut coworker turn once; the original prompt is sent 0 times."""
    def test_interrupted_turn_hits_idle_open_tool(self):
        found = interrupted_turn(transcript('idle', [USER, OPEN]), now=1010 + PENDING_TOOL_SECONDS + 1)
        self.assertIsNotNone(found)
        self.assertTrue(found['pending'])
        self.assertEqual(found['key'], 't1')

    def test_check_sends_one_continue_after_pending_tool_seconds(self):
        store, adapter, now = tw.FakeStore(), tw.FakeAdapter(), [1000.0]
        store.put('accounts', {'id': 'acc', 'state': 'ready', 'team_threads': {}})
        store.put('workers', {'id': 'w-co', 'account_id': 'acc', 'thread_id': 'co-thread', 'state': 'recovering'})
        store.put('tasks', {'id': 'tk', 'run_id': 'r', 'worker_id': 'w-co', 'state': 'uncertain', 'prompt': PROMPT})
        adapter.transcripts['co-thread'] = tw.transcript(
            tw.entity(1, 'user_message', 'u1', created_at=1000.0),
            tw.entity(2, 'mcp_run_tool', 't1', tool_use_id='x', interaction_status='pending', created_at=1000.0))
        watch = TurnWatch(store, adapter, tw.FakeEngine(), clock=lambda: now[0])
        watch.spawn = lambda fn, *args: fn(*args)
        worker = store.get('workers', 'w-co')
        self.assertIsNone(watch.check(worker))
        now[0] += PENDING_TOOL_SECONDS + 1
        self.assertIsNotNone(watch.check(worker))
        now[0] += 60
        self.assertIsNone(watch.check(worker))  # same key: nothing more
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(adapter.sent[0][2], turn_watch.RESUME_TEXT)
        self.assertEqual(sum(1 for s in adapter.sent if PROMPT in s[2]), 0)


class FileToolLockTests(unittest.TestCase):
    """nt_read_file/nt_write_file answer within the lock limit while the engine lock is held; nothing changes."""
    def test_held_engine_lock(self):
        store, engine = tw.FakeStore(), tw.FakeEngine()
        bridge = bridge_mod.Bridge(store, engine)
        target = Path(tempfile.mkdtemp()) / 'f.txt'
        target.write_text('old', encoding='utf-8')
        held, release = threading.Event(), threading.Event()

        def hold():
            with engine.lock:
                held.set()
                release.wait(10)
        threading.Thread(target=hold, daemon=True).start()
        held.wait(2)
        saved = bridge_mod.LOCK_WAIT_S
        bridge_mod.LOCK_WAIT_S = 0.3
        try:
            start = time.monotonic()
            out = bridge.operator_call(None, 'nt_read_file', {'path': str(target)})
            self.assertIn('old', str(out))
            with self.assertRaises(ValueError) as caught:
                bridge.operator_call(None, 'nt_write_file', {'path': str(target), 'content': 'new',
                                                             'assignment_token': 'tok'})
            self.assertLess(time.monotonic() - start, 3.0)
            self.assertIn('nothing was changed', str(caught.exception))
            self.assertEqual(target.read_text(encoding='utf-8'), 'old')
        finally:
            bridge_mod.LOCK_WAIT_S = saved
            release.set()


from notiontool.turn_watch import TurnWatch  # noqa: E402


if __name__ == '__main__':
    unittest.main()
