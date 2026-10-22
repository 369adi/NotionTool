import pathlib
import re
import unittest
from unittest import mock

from notiontool import desktop_shell, step_guard

ROOT = pathlib.Path(__file__).resolve().parents[1]
OVERLAY = (ROOT / 'notiontool' / 'web' / 'desktop' / 'overlay.js').read_text(encoding='utf-8')
SERVER = (ROOT / 'notiontool' / 'server.py').read_text(encoding='utf-8')


class StepsHourTest(unittest.TestCase):
    def test_left_is_limit_minus_used(self):
        fake = dict(level='ok', wait=0, ai_steps_60m=120, steps_15m=40,
                    limits=dict(step_guard.DEFAULTS))
        with mock.patch.object(step_guard, 'state', return_value=fake):
            out = desktop_shell._steps_hour(object(), 'acc', 0)
        self.assertEqual(out['used'], 120)
        self.assertEqual(out['limit'], step_guard.DEFAULTS['inf_hard_60m'])
        self.assertEqual(out['left'], step_guard.DEFAULTS['inf_hard_60m'] - 120)
        self.assertEqual(out['limit_15m'], step_guard.DEFAULTS['step_hard_15m'])

    def test_never_negative_and_fail_safe(self):
        fake = dict(level='pause', wait=60, ai_steps_60m=999, limits=dict(step_guard.DEFAULTS))
        with mock.patch.object(step_guard, 'state', return_value=fake):
            self.assertEqual(desktop_shell._steps_hour(object(), 'acc', 0)['left'], 0)
        with mock.patch.object(step_guard, 'state', side_effect=RuntimeError('x')):
            self.assertIsNone(desktop_shell._steps_hour(object(), 'acc', 0))


class OverlayTest(unittest.TestCase):
    def test_main_rows_get_visible_switch_button(self):
        self.assertIn('function mainSwitchButton(info)', OVERLAY)
        self.assertIn("'data-nt-desk': 'main-switch'", OVERLAY)
        self.assertIn("runMenuItem('switch'", OVERLAY)

    def test_steps_chip_in_coworker_head(self):
        self.assertIn('function stepsChip(state)', OVERLAY)
        self.assertIn("' AI left'", OVERLAY)
        self.assertIn('nt-desk-ailimit', OVERLAY)  # not .nt-desk-steps: theme.css styles that as Notion's step pill
        self.assertNotIn("'nt-desk-chip nt-desk-steps'", OVERLAY)
        self.assertIn('stepsChip(state)', OVERLAY)


class ResetCoworkersTest(unittest.TestCase):
    def test_reset_also_deletes_renamed_pool_chats(self):
        self.assertEqual(SERVER.count("re.match(r'^Coworker \\d+$'"), 2)
        pat = r'^Coworker \d+$'
        self.assertTrue(re.match(pat, 'Coworker 17'))
        self.assertFalse(re.match(pat, 'Roblox 1'))
        self.assertFalse(re.match(pat, 'Coworker notes'))


class UpkeepTargetTest(unittest.TestCase):
    def test_refill_target_follows_team_size_without_coworker_target(self):
        from notiontool import coworker_upkeep as cu
        from tests import test_coworker_upkeep as tcu

        class Store5(tcu.Store):
            def settings(self):
                return {'harness_account_id': tcu.ACC, 'per_account': 5}

        store = Store5([tcu.w('c%d' % i) for i in range(4)])
        adapter = tcu.Adapter()
        out = cu.CoworkerUpkeep(store, adapter, tcu.Engine(), clock=lambda: 1000.0).once()
        self.assertEqual((out['alive'], out['create']), (4, 1))  # 4 alive -> exactly 1 more, never up to 10
        full = Store5([tcu.w('c%d' % i) for i in range(5)])
        out = cu.CoworkerUpkeep(full, tcu.Adapter(), tcu.Engine(), clock=lambda: 1000.0).once()
        self.assertEqual(out['create'], 0)


class TrimTest(unittest.TestCase):
    def test_trim_plan_only_idle_extras(self):
        from notiontool import coworker_upkeep as cu
        from tests import test_coworker_upkeep as tcu
        ws = [tcu.w('c%d' % i, welcome_at=i) for i in range(7)]
        ws.append(tcu.w('own', welcome=False, enabled=False))  # the user's own chat never counts or goes
        ws[6]['lease'] = 'x'  # leased: kept
        tasks = [dict(id='t', worker_id='c5', state='queued')]  # has a task: kept
        ids = cu.trim_plan(ws, tasks, tcu.ACC, 5, keep_threads=['c4'])  # ledger role: kept
        self.assertEqual(ids, ['c3', 'c2'])
        self.assertEqual(cu.trim_plan(ws[:5], [], tcu.ACC, 5), [])

    def test_once_deletes_extras(self):
        from notiontool import coworker_upkeep as cu
        from tests import test_coworker_upkeep as tcu

        class Store5(tcu.Store):
            def settings(self):
                return {'harness_account_id': tcu.ACC, 'per_account': 5}

            def delete(self, kind, ident):
                self.data[kind].pop(ident, None)

        class Adapter5(tcu.Adapter):
            deleted = []

            def client(self, account_id):
                return 'client'

            def delete_thread(self, client, thread_id):
                self.deleted.append(thread_id)

        store = Store5([tcu.w('c%d' % i, welcome_at=i) for i in range(7)])
        adapter = Adapter5()
        out = cu.CoworkerUpkeep(store, adapter, tcu.Engine(), clock=lambda: 1000.0).once()
        self.assertEqual(sorted(out['trimmed']), ['c5', 'c6'])
        self.assertEqual(len(store.data['workers']), 5)
        self.assertEqual(out['create'], 0)


if __name__ == '__main__':
    unittest.main()
