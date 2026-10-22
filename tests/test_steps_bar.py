"""AI steps bar: Main step counting (paging back, restart), rolling-window reset times, _steps_hour shape, bar UI."""
import json
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path

from notiontool import desktop_shell, step_guard
from notiontool.turn_watch import WINDOW, TurnWatch

NOW = 1_800_000_000.0
A = 'acc1'
M = dict(id=A + ':main', account_id=A, purpose='operator', thread_id='main-thread')
CW = dict(id=A + ':t1', account_id=A, purpose='worker', thread_id='t1')
JS = Path(desktop_shell.__file__).resolve().parent / 'web' / 'desktop' / 'stepcap.js'


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.docs, self.events, self.puts = {}, [], []

    def get(self, kind, ident, default=None):
        return self.docs.get((kind, ident), default)

    def all(self, kind):
        return [v for (k, _), v in self.docs.items() if k == kind]

    def put(self, kind, doc):
        self.puts.append(kind)
        self.docs[(kind, doc['id'])] = json.loads(json.dumps(doc))

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))

    def settings(self):
        return {}


class Chat:
    """Fake Notion client: transcript(thread, limit) returns only the newest `limit` entries (like notion.py)."""

    def __init__(self):
        self.patches, self.reads = [], []

    def add(self, kind, n=1, **extra):
        for _ in range(n):
            i = len(self.patches) + 1
            self.patches.append(dict(op='put', entity=dict(id='e%d' % i, kind=kind, sequence=i, **extra)))

    def put(self, entity):
        self.patches.append(dict(op='put', entity=entity))

    def transcript(self, thread, limit=100):
        self.reads.append(limit)
        return dict(patches=self.patches[-limit:])


def check(watch, chat):
    """What TurnWatch.check does: read WINDOW entries, then count (with the client for paging back)."""
    return watch.count_steps(M, chat.transcript(M['thread_id'], limit=WINDOW), client=chat)


def st(store, now):
    return step_guard.state(store, now, account=A)


def real_tool(ident, seq, started=None, name='mcp_run_tool'):
    """Tool entry in the shape of a real Main transcript (thread_context of the main chat, 2026-10-22)."""
    entity = dict(id=ident, kind='tool', name=name, presentation_kind='generic', source='committed',
                  sequence=seq, tool_use_id=ident.split(':')[-1], created_at='2026-10-22T16:25:01.816Z',
                  text={'running': [['NotionTool / nt_shell']]},
                  result={'completed_at': '2026-10-22T16:25:20.322Z', 'is_error': False})
    if started:
        entity.update(started_at=started, icon_name='cloudArrowUp',
                      payload={'type': 'mcp', 'server_name': 'NotionTool', 'tool_name': 'nt_shell'})
    return entity


def bar_section():
    text = JS.read_text(encoding='utf-8')
    start, end = text.index('/* nt-steps-bar:start'), text.index('/* nt-steps-bar:end */')
    return text[start:end]


class StepsBarTest(unittest.TestCase):
    def test_more_than_window_new_entries_are_all_counted(self):
        s, chat = Store(), Chat()
        watch = TurnWatch(s, None, None, clock=lambda: NOW)
        chat.add('user_message')
        chat.add('assistant_message')
        self.assertEqual(check(watch, chat), 0)  # very first look: baseline only
        chat.add('user_message')
        for _ in range(50):
            chat.add('tool_call')
            chat.add('tool_result')
        chat.add('assistant_message')
        self.assertGreater(len(chat.patches) - 2, WINDOW)  # 102 new entries between two checks
        self.assertEqual(check(watch, chat), 51)  # 50 tool calls + 1 answer; results and user messages are no steps
        self.assertGreater(max(chat.reads), WINDOW)  # it paged back
        state = st(s, NOW + 1)
        self.assertEqual(state['steps_60m'], 51)
        self.assertEqual(state['ai_steps_60m'], 51)
        self.assertEqual(check(watch, chat), 0)  # nothing new: nothing counted twice

    def test_without_client_only_the_window_is_seen(self):
        s, chat = Store(), Chat()
        watch = TurnWatch(s, None, None, clock=lambda: NOW)
        chat.add('user_message')
        self.assertEqual(watch.count_steps(M, chat.transcript('x', limit=WINDOW)), 0)
        chat.add('tool_call', 60)
        self.assertEqual(watch.count_steps(M, chat.transcript('x', limit=WINDOW)), WINDOW)

    def test_restart_in_the_middle_keeps_the_baseline(self):
        s, chat = Store(), Chat()
        first = TurnWatch(s, None, None, clock=lambda: NOW)
        chat.add('user_message')
        chat.add('tool_call')
        chat.add('assistant_message')
        self.assertEqual(check(first, chat), 0)
        chat.add('user_message')
        chat.add('tool_call', 2)
        self.assertEqual(check(first, chat), 2)
        # NotionTool restarts; the Main chat goes on while it is down (more than WINDOW entries)
        chat.add('tool_call', 45)
        chat.add('assistant_message')
        second = TurnWatch(s, None, None, clock=lambda: NOW + 10)  # new instance, same store
        self.assertEqual(check(second, chat), 46)  # no new baseline: the steps written while down count
        self.assertEqual(check(second, chat), 0)
        doc = s.get(step_guard.SEEN_KIND, M['id'] + ':' + M['thread_id'])
        self.assertEqual(doc['ids'][-1], 'e%d' % len(chat.patches))
        self.assertEqual(st(s, NOW + 11)['ai_steps_60m'], 48)
        self.assertEqual(st(s, NOW + 11)['steps_60m'], 48)

    def test_parallel_tool_calls_of_one_inference_count_once(self):
        s, chat = Store(), Chat()
        watch = TurnWatch(s, None, None, clock=lambda: NOW)
        chat.add('user_message')
        self.assertEqual(check(watch, chat), 0)
        chat.add('tool_call', 3, inference_id='inf-1')
        chat.add('assistant_message', inference_id='inf-2')
        self.assertEqual(check(watch, chat), 4)
        self.assertEqual(st(s, NOW + 1)['steps_60m'], 4)
        self.assertEqual(st(s, NOW + 1)['ai_steps_60m'], 2)

    def test_real_main_entry_shape_groups_parallel_calls_by_started_at(self):
        # Real Main transcript: kind 'tool', no inference_id / step_id; two parallel MCP calls of one inference
        # share the exact started_at; callFunction entries (toolu_...) carry no started_at.
        s, chat = Store(), Chat()
        watch = TurnWatch(s, None, None, clock=lambda: NOW)
        chat.put(dict(id='u1', kind='user_message', sequence=156))
        self.assertEqual(check(watch, chat), 0)
        chat.put(real_tool('toolu_019gJ1CcAYuK7AsK8gUfBaTj', 156.000008, name='callFunction'))
        chat.put(real_tool('legacy-workflow-effect:401dac2a-b9fd-8118-80bd-00aa0a695af9', 156.00001,
                           started='2026-10-22T16:25:01.795Z'))
        chat.put(real_tool('legacy-workflow-effect:401dac2a-b9fd-812b-aef7-00aabe208ae9', 156.000011,
                           started='2026-10-22T16:25:01.795Z'))
        chat.put(real_tool('legacy-workflow-effect:401dac2a-b9fd-8193-867a-00aa377d771b', 156.000013,
                           started='2026-10-22T16:25:29.580Z'))
        chat.put(dict(id='a1', kind='assistant_message', sequence=157))
        self.assertEqual(check(watch, chat), 5)  # every tool entry and the answer are steps
        state = st(s, NOW + 1)
        self.assertEqual(state['steps_60m'], 5)
        self.assertEqual(state['ai_steps_60m'], 4)  # the two calls with one started_at are one AI step

    def test_seen_list_is_saved_only_when_it_changed(self):
        s, chat = Store(), Chat()
        watch = TurnWatch(s, None, None, clock=lambda: NOW)
        chat.add('user_message')
        check(watch, chat)
        self.assertEqual(s.puts.count(step_guard.SEEN_KIND), 1)  # first look: baseline saved
        check(watch, chat)
        self.assertEqual(s.puts.count(step_guard.SEEN_KIND), 1)  # nothing new: no write
        chat.add('tool_call')
        check(watch, chat)
        self.assertEqual(s.puts.count(step_guard.SEEN_KIND), 2)
        restarted = TurnWatch(s, None, None, clock=lambda: NOW)  # loaded from the store, unchanged
        check(restarted, chat)
        self.assertEqual(s.puts.count(step_guard.SEEN_KIND), 2)

    def test_main_and_coworker_each_counted_once(self):
        s = Store()
        # the stream helper (notion.py) skips Main chats: turn_watch counts them from the transcript
        self.assertEqual(step_guard.count_new(s, M, [dict(type='agent-inference')] * 5, 0, now=NOW), 5)
        self.assertEqual(st(s, NOW + 1)['steps_60m'], 0)
        # coworker steps come only from the stream; turn_watch never counts a coworker transcript
        self.assertEqual(step_guard.count_new(s, CW, [dict(type='agent-inference')] * 5, 0, now=NOW), 5)
        chat = Chat()
        chat.add('tool_call', 5)
        watch = TurnWatch(s, None, None, clock=lambda: NOW)
        self.assertEqual(watch.count_steps(CW, chat.transcript('t1', limit=WINDOW), client=chat), 0)
        self.assertEqual(watch.count_steps(CW, chat.transcript('t1', limit=WINDOW), client=chat), 0)
        self.assertEqual(st(s, NOW + 1)['steps_60m'], 5)
        self.assertEqual(st(s, NOW + 1)['ai_steps_60m'], 5)

    def test_other_streamed_purposes_are_counted_once(self):
        self.assertEqual(step_guard.TRANSCRIPT_PURPOSES, ('operator', 'main'))
        for purpose in ('spare_main', 'external'):
            s = Store()
            worker = dict(id=A + ':' + purpose, account_id=A, purpose=purpose, thread_id=purpose + '-thread')
            steps = [dict(type='agent-inference'), dict(type='tool-call'), dict(type='agent-inference')]
            self.assertEqual(step_guard.count_new(s, worker, steps, 0, now=NOW), 3)
            self.assertEqual(step_guard.count_new(s, worker, steps, 3, now=NOW), 3)  # nothing new
            chat = Chat()
            chat.add('tool_call', 4)
            watch = TurnWatch(s, None, None, clock=lambda: NOW)
            for _ in range(2):  # turn_watch never counts a streamed purpose
                self.assertEqual(watch.count_steps(worker, chat.transcript('x', limit=WINDOW), client=chat), 0)
            state = st(s, NOW + 1)
            self.assertEqual(state['steps_60m'], 3, purpose)
            self.assertEqual(state['ai_steps_60m'], 2, purpose)

    def test_rolling_window_reset_times(self):
        s = Store()
        step_guard.add_steps(s, CW, 10, now=NOW, inference=4)
        step_guard.add_steps(s, CW, 10, now=NOW + 600, inference=3)
        step_guard.add_steps(s, CW, 10, now=NOW + 1200, inference=0)  # no AI step: does not hold the bar
        a = st(s, NOW + 1800)
        self.assertEqual(a['ai_steps_60m'], 7)
        self.assertEqual(a['ai_next_free_at'], NOW + 3600)  # the oldest AI steps drop out
        self.assertEqual(a['ai_resets_at'], NOW + 600 + 3600)  # the newest AI steps drop out: fully free
        self.assertIsNone(a['ai_under_limit_at'])  # under the limit already
        b = st(s, NOW + 3601)
        self.assertEqual(b['ai_steps_60m'], 3)
        self.assertEqual(b['ai_next_free_at'], NOW + 4200)
        self.assertEqual(b['ai_resets_at'], NOW + 4200)
        c = st(s, NOW + 4201)
        self.assertEqual(c['ai_steps_60m'], 0)
        self.assertIsNone(c['ai_next_free_at'])
        self.assertIsNone(c['ai_resets_at'])

    def test_over_the_limit_tells_when_it_is_under_again(self):
        s = Store()
        step_guard.add_steps(s, CW, 300, now=NOW, inference=300)
        step_guard.add_steps(s, CW, 200, now=NOW + 60, inference=200)  # 500 >= 450: pause
        a = st(s, NOW + 120)
        self.assertEqual(a['level'], 'pause')
        self.assertEqual(a['ai_steps_60m'], 500)
        self.assertEqual(a['ai_under_limit_at'], NOW + 3600)
        self.assertEqual(a['ai_next_free_at'], NOW + 3600)
        self.assertEqual(a['ai_resets_at'], NOW + 60 + 3600)

    def test_steps_hour_output_shape(self):
        s = Store()
        empty = desktop_shell._steps_hour(s, A, NOW)
        self.assertLessEqual({'used', 'left', 'limit', 'resets_at', 'next_free_at', 'steps_15m', 'limit_15m'},
                             set(empty))
        self.assertEqual(empty['used'], 0)
        self.assertEqual(empty['left'], 450)
        self.assertEqual(empty['limit'], 450)
        self.assertIsNone(empty['resets_at'])
        self.assertIsNone(empty['next_free_at'])
        step_guard.add_steps(s, CW, 10, now=NOW, inference=4)
        step_guard.add_steps(s, CW, 10, now=NOW + 600, inference=3)
        out = desktop_shell._steps_hour(s, A, NOW + 1800)
        self.assertEqual((out['used'], out['left'], out['limit']), (7, 443, 450))
        self.assertEqual(out['next_free_at'], NOW + 3600)
        self.assertEqual(out['resets_at'], NOW + 4200)
        self.assertEqual((out['steps_15m'], out['limit_15m'], out['window_s']), (0, 1000, 3600))
        json.dumps(out)  # goes to the UI state as JSON

    def test_bar_replaces_the_chip_in_stepcap_js(self):
        code = bar_section()
        self.assertIn('[data-nt-desk="steps-chip"]', code)
        self.assertIn("chip.style.display = 'none'", code)
        for part in ("'steps-bar'", 'AI steps left', 'full at', 'YELLOW_AT = 0.7, RED_AT = 0.9', 'Last 15 min: ',
                     'steps_hour', 'onState'):
            self.assertIn(part, code)
        css = Path(JS.parent / 'progress.css').read_text(encoding='utf-8')
        self.assertIn('nt-desk-ailimit-bar', css)
        self.assertIn('[data-nt-desk="steps-chip"] { display: none', css)  # the chip never flashes

    @unittest.skipIf(not shutil.which('node'), 'node is not installed')
    def test_bar_model_in_node(self):
        reset, nxt = NOW + 4200, NOW + 3600
        cases = [dict(used=0, left=450, limit=450, steps_15m=0, limit_15m=1000),
                 dict(used=314, left=136, limit=450, resets_at=reset, next_free_at=nxt, steps_15m=12, limit_15m=1000),
                 dict(used=315, left=135, limit=450, resets_at=reset, next_free_at=nxt, steps_15m=12, limit_15m=1000),
                 dict(used=405, left=45, limit=450, resets_at=reset, next_free_at=nxt, steps_15m=12, limit_15m=1000)]
        script = ('var window = {};\n' + bar_section() + '\nvar m = window.__ntStepsBarModel;\n'
                  'console.log(JSON.stringify(%s.map(m)));\n' % json.dumps(cases))
        run = subprocess.run(['node', '-'], input=script, capture_output=True, text=True,
                             encoding='utf-8', timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        self.assertEqual([o['level'] for o in out], ['ok', 'ok', 'warn', 'high'])
        self.assertEqual(out[0]['text'], '450 AI steps left')
        self.assertEqual(out[0]['fill'], 100)
        self.assertIn('No AI steps in the last 60 min.', out[0]['title'])

        def hm(t):
            return time.strftime('%H:%M', time.localtime(t))
        self.assertEqual(out[2]['text'], '135 AI steps left \u00b7 full at ' + hm(reset))
        self.assertEqual(out[3]['fill'], 10)
        self.assertEqual(out[3]['color'], '#c4372b')
        self.assertEqual(out[2]['color'], '#d9a400')
        title = out[3]['title']
        for part in ('405 used of 450, 45 left', 'Next step frees up at ' + hm(nxt), 'Fully free again at ' + hm(reset),
                     'Last 15 min: 12 of 1000 steps.'):
            self.assertIn(part, title)


if __name__ == '__main__':
    unittest.main()
