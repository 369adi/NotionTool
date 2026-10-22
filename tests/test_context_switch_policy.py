"""Measured Coworker switch rule (chat_context.switch_decision) and its wiring in rotate()."""
import math
import re
import tempfile
import threading
import unittest
from pathlib import Path

from notiontool import desktop_settings as ds
from notiontool.chat_context import (ChatContext, SWITCH_DEFAULTS, queued_ahead, switch_decision,
                                     switch_message)
from notiontool.store import Store

ROOT = Path(__file__).resolve().parent.parent
OLD = '3fbd3dc4-6a0c-80aa-b071-00a9e12fce0b'
NEW = '3fbd3dc4-6a0c-80bb-8001-00a9aaaaaaaa'
MEASURED = dict(coworker_switch_policy='measured')
THRESHOLD = dict(coworker_switch_policy='threshold')
# Defaults (docs/CONTEXT_SWITCH_BENCH.md 'Defaults 2026-10-22'): T0 12k, C 76 s, b 3.514 s/task/100k, e 0.
T_STAR = {1: 2174778, 3: 732926, 5: 444556}


def usage(tokens, turns=10, limit=1200000, turn_limit=600):
    return dict(estimated_tokens=tokens, turns=turns, token_limit=limit, turn_limit=turn_limit)


class Decision(unittest.TestCase):
    def test_measured_is_the_default_for_coworkers(self):
        self.assertEqual(SWITCH_DEFAULTS['coworker_switch_policy'], 'measured')
        self.assertEqual(switch_decision({}, usage(0))['policy'], 'measured')

    def test_defaults_rest_on_step_gap_measurement(self):
        self.assertEqual((SWITCH_DEFAULTS['switch_cost_s'], SWITCH_DEFAULTS['switch_slope_ms_per_100k'],
                          SWITCH_DEFAULTS['switch_error_permille_per_100k'], SWITCH_DEFAULTS['switch_min_tokens']),
                         (76, 3514, 0, 400000))

    def test_threshold_keeps_old_behaviour(self):
        self.assertFalse(switch_decision(THRESHOLD, usage(1199999))['switch'])
        d = switch_decision(THRESHOLD, usage(1200000))
        self.assertEqual((d['switch'], d['reason'], d['policy']), (True, 'token_limit', 'threshold'))
        self.assertEqual(switch_decision(THRESHOLD, usage(1000, turns=600))['reason'], 'turn_limit')
        self.assertFalse(switch_decision(THRESHOLD, usage(900000))['switch'])  # measured would switch here

    def test_t_star_for_1_3_5_tasks(self):
        for n, t_star in T_STAR.items():
            expected = math.ceil(12000 + 76 * 100000 / (n * 3.514))
            self.assertEqual(t_star, expected)
            d = switch_decision(dict(MEASURED, switch_remaining_tasks=n), usage(0))
            self.assertEqual(d['t_star_tokens'], t_star)
            self.assertEqual(d['switch_at_tokens'], min(1200000, max(400000, t_star)))

    def test_measured_switches_only_above_the_point(self):
        below = switch_decision(MEASURED, usage(732000))
        above = switch_decision(MEASURED, usage(734000))
        self.assertFalse(below['switch'])
        self.assertEqual((above['switch'], above['reason'], above['policy']), (True, 'measured', 'measured'))
        self.assertGreater(above['extra_s'], above['switch_cost_s'])
        self.assertLessEqual(below['extra_s'], below['switch_cost_s'])

    def test_floor_blocks_switch_for_speed(self):
        # 20 tasks ahead put T* near 120k, but never below the 400k floor.
        d = switch_decision(MEASURED, usage(399999), queued=19)
        self.assertLess(d['t_star_tokens'], 400000)
        self.assertFalse(d['switch'])
        self.assertEqual(d['switch_at_tokens'], 400000)
        self.assertTrue(switch_decision(MEASURED, usage(400000), queued=19)['switch'])

    def test_hard_limits_always_win(self):
        no_gain = dict(MEASURED, switch_slope_ms_per_100k=0, switch_error_permille_per_100k=0)
        self.assertIsNone(switch_decision(no_gain, usage(900000))['t_star_tokens'])
        self.assertFalse(switch_decision(no_gain, usage(900000))['switch'])
        self.assertEqual(switch_decision(no_gain, usage(1200000))['reason'], 'token_limit')
        self.assertEqual(switch_decision(no_gain, usage(1000, turns=600))['reason'], 'turn_limit')
        # One task ahead: T* 2.17M lies above the 1.2M hard limit, so the limit is the switch point.
        d = switch_decision(MEASURED, usage(0), queued=0)
        self.assertEqual(d['switch_at_tokens'], 1200000)
        self.assertEqual(switch_decision(MEASURED, usage(1200000), queued=0)['reason'], 'token_limit')

    def test_main_never_uses_measured(self):
        d = switch_decision(MEASURED, usage(900000), 'main')
        self.assertEqual((d['policy'], d['switch']), ('threshold', False))

    def test_tasks_ahead_from_queue_with_setting_fallback(self):
        d = switch_decision(MEASURED, usage(0), queued=4)
        self.assertEqual((d['remaining_tasks'], d['remaining_source'], d['queued_tasks']), (5, 'queue', 4))
        self.assertEqual(d['t_star_tokens'], T_STAR[5])
        d = switch_decision(dict(MEASURED, switch_remaining_tasks=7), usage(0), queued=0)
        self.assertEqual((d['remaining_tasks'], d['remaining_source']), (1, 'queue'))
        d = switch_decision(dict(MEASURED, switch_remaining_tasks=7), usage(0))
        self.assertEqual((d['remaining_tasks'], d['remaining_source'], d['queued_tasks']), (7, 'setting', None))
        self.assertEqual(switch_decision(MEASURED, usage(0), queued=500)['remaining_tasks'], 50)

    def test_queued_ahead_counts_worker_and_run(self):
        w, task = {'id': 'w1'}, {'id': 't1', 'run_id': 'r1'}
        tasks = [task,
                 dict(id='a', state='queued', worker_id='w1', run_id='r9'),   # own queue
                 dict(id='b', state='retry', worker_id='w1'),                 # own retry
                 dict(id='c', state='ready', run_id='r1'),                    # same run, unassigned
                 dict(id='d', state='queued', run_id='r1', worker_id='w2'),   # same run, other worker
                 dict(id='e', state='done', worker_id='w1'),                  # finished
                 dict(id='f', state='running', worker_id='w1'),               # not queued
                 dict(id='g', state='queued', run_id='r2')]                   # other run
        self.assertEqual(queued_ahead(tasks, w, task), 3)
        self.assertEqual(queued_ahead([], w, task), 0)
        self.assertIsNone(queued_ahead(None, w, task))

    def test_message_has_the_numbers(self):
        d = switch_decision(MEASURED, usage(740000), queued=2)
        text = switch_message('measured', d)
        for part in ('740000 tokens', 'estimated extra', 'next 3 tasks (queue)', 'switch cost 76 s',
                     'switch point 732926', 'hard limit 1200000'):
            self.assertIn(part, text)


class Adapter:
    semantic_context = False
    def __init__(s): s.renamed = []
    def new_id(s, space, tail): return NEW
    def create_welcome(s, client, tid, model): return True, True
    def rename_thread(s, c, tid, title): s.renamed.append((tid, title))


class Client:
    info = {'space_id': '8d7d3dc4-6a0c-8164-897f-00036215c141'}
    def transcript(s, tid): return {'session': {'status': 'idle', 'sequence': 5}}


class Ctx(ChatContext):
    tokens = 0
    def usage(self, worker, transcript):
        return usage(self.tokens, limit=int(self.store.settings().get('context_token_limit', 1200000)))


class RotateWiring(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / '.test-data'; root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root); self.store = Store(self.temp.name)
        self.store.put('accounts', dict(id='a', slot=1, model='m'))
        self.w = dict(id='a:' + OLD, account_id='a', thread_id=OLD, purpose='worker', enabled=True, generation=1,
                      label='Welcome to Notion', prev_task_id='t0')
        self.store.put('workers', self.w)
        self.task = {'id': 't1', 'run_id': 'r1', 'prompt': 'next task'}
    def tearDown(self): self.store.close(); self.temp.cleanup()

    def queue(self, n):
        for i in range(n):
            self.store.put('tasks', dict(id='q%d' % i, run_id='r1', state='queued', worker_id=self.w['id'],
                                         prompt='queued %d' % i))

    def run_rotate(self, tokens, **settings):
        self.store.put('settings', dict(id='app', **settings))
        ctx = Ctx(self.store); ctx.tokens = tokens
        return ctx.rotate(Adapter(), Client(), dict(self.w), self.task, {'session': {'status': 'idle'}}, threading.Event())

    def switch_events(self):
        return [e for e in self.store.events() if e['kind'] == 'context_switch']

    def test_measured_switch_writes_event_and_moves_chat(self):
        self.queue(2)  # 2 queued + this task = 3 tasks ahead -> T* 732926
        w = self.run_rotate(740000)  # measured is the default
        self.assertEqual(w['thread_id'], NEW)
        events = self.switch_events()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e['reason'], 'measured')
        self.assertEqual(e['decision']['tokens'], 740000)
        self.assertEqual(e['decision']['switch_cost_s'], 76)
        self.assertEqual(e['decision']['switch_at_tokens'], T_STAR[3])
        self.assertEqual((e['decision']['remaining_tasks'], e['decision']['remaining_source']), (3, 'queue'))
        self.assertGreater(e['decision']['extra_s'], 76)
        self.assertIn('estimated extra', e['message'])
        rotated = [x for x in self.store.events() if x['kind'] == 'context_rotated']
        self.assertEqual(rotated[-1]['usage']['switch']['reason'], 'measured')

    def test_empty_queue_means_one_task_and_keeps_chat(self):
        w = self.run_rotate(740000)  # only this task ahead -> switch point is the 1.2M hard limit
        self.assertEqual(w['thread_id'], OLD)
        self.assertEqual(self.switch_events(), [])
        d = self.store.get('workers', self.w['id'])['context_usage']['switch']
        self.assertEqual((d['remaining_tasks'], d['remaining_source'], d['switch_at_tokens']), (1, 'queue', 1200000))

    def test_measured_below_point_keeps_chat(self):
        self.queue(2)
        w = self.run_rotate(700000)
        self.assertEqual(w['thread_id'], OLD)
        self.assertEqual(self.switch_events(), [])
        self.assertFalse(self.store.get('workers', self.w['id'])['context_usage']['switch']['switch'])

    def test_threshold_ignores_measured_point(self):
        self.queue(2)
        w = self.run_rotate(740000, coworker_switch_policy='threshold')
        self.assertEqual(w['thread_id'], OLD)
        self.assertEqual(self.switch_events(), [])

    def test_threshold_hard_limit_event(self):
        w = self.run_rotate(5000, coworker_switch_policy='threshold', context_token_limit=4000)
        self.assertEqual(w['thread_id'], NEW)
        self.assertEqual(self.switch_events()[0]['reason'], 'token_limit')


class SettingsWiring(unittest.TestCase):
    def test_rule_is_called_in_rotate(self):
        src = (ROOT / 'notiontool' / 'chat_context.py').read_text(encoding='utf-8')
        body = src.split('    def rotate(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn("queued_ahead(self.store.all('tasks'), worker, task)", body)
        self.assertIn("switch_decision(self.store.settings(), usage, worker.get('purpose'), queued)", body)
        self.assertIn("not decision['switch']", body)
        self.assertIn("self.store.event('context_switch'", body)

    def test_store_defaults_match(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                settings = store.settings()
            finally:
                store.close()
        for key, value in SWITCH_DEFAULTS.items():
            self.assertEqual(settings[key], value, key)
        self.assertEqual(settings['coworker_switch_policy'], 'measured')
        self.assertIn('context_token_limit', settings)
        self.assertIn('context_turn_limit', settings)

    def test_desktop_entries_match_server_bounds_and_defaults(self):
        server = (ROOT / 'notiontool' / 'server.py').read_text(encoding='utf-8')
        clamps = {k: (int(lo), int(hi)) for k, lo, hi in re.findall(r"\('(switch_[a-z0-9_]+)',(\d+),(\d+)\)", server)}
        entries = {e['key']: e for e in ds.SWITCH_SETTINGS}
        self.assertEqual(set(entries), set(SWITCH_DEFAULTS))
        for key, entry in entries.items():
            self.assertEqual(entry['default'], SWITCH_DEFAULTS[key], key)
            self.assertFalse(entry['app_local'])
            if entry['type'] == 'int':
                self.assertEqual((entry['min'], entry['max']), clamps[key], key)
                self.assertTrue(entry['min'] <= entry['default'] <= entry['max'], key)
        self.assertEqual(entries['coworker_switch_policy']['options'], ['threshold', 'measured'])
        self.assertEqual(entries['coworker_switch_policy']['default'], 'measured')
        self.assertIn("not in ('threshold','measured')", server)
        schema = ds.settings_schema({'switch_cost_s': 80})
        shown = {e['key']: e['value'] for e in schema['switch']}
        self.assertEqual(set(shown), set(SWITCH_DEFAULTS))
        self.assertEqual(shown['switch_cost_s'], 80)
        self.assertEqual(shown['coworker_switch_policy'], 'measured')
        self.assertFalse(set(SWITCH_DEFAULTS) & {e['key'] for e in schema['harness']})

    def test_crlf_files_stay_crlf(self):
        for name in ('chat_context.py', 'server.py'):
            data = (ROOT / 'notiontool' / name).read_bytes()
            self.assertEqual(data.count(b'\n'), data.count(b'\r\n'), name)


if __name__ == '__main__':
    unittest.main()
