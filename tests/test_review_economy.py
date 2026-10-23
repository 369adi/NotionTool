"""Review economy: review_model_policy (auto|opus) and review_followup_scope (fix_only|full)."""
import json
import os
import tempfile
import unittest

from notiontool.engine import Engine
from notiontool.operator_main import delegate, register_operator
from notiontool.operator_roles import (MAX_FIX_ROUNDS, REVIEW_CORE_FILES, followup_review_prompt, review_model,
                                       review_prompt)
from notiontool.store import Store

ACC = 'acc1'
OP = ACC + ':op-thread'
CO1, CO2, CO3 = ACC + ':co1', ACC + ':co2', ACC + ':co3'
DONE = json.dumps(dict(status='done', summary='Built it.\nRESULT: DONE'))
PASS = json.dumps(dict(passed=True, summary='APPROVED', fixes=[]))
FAIL = json.dumps(dict(passed=False, summary='Changes needed', fixes=[
    dict(id='F1', title='Fix rounding', prompt='Round half up in calc.txt', acceptance='rounded', files=['calc.txt'])]))


class Adapter:  # fake client: no Notion call is ever made, the test completes the tasks
    pass


def spec(files=('calc.txt',), acceptance='tests/test_x.py passes', ident='T1'):
    return dict(id=ident, title='Part ' + ident, prompt='Do ' + ident, acceptance=acceptance,
                depends_on=[], files=list(files), risk='medium')


class ReviewEconomyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        for name, lines in (('calc.txt', 3), ('b.txt', 3), ('c.txt', 3), ('big.txt', 500),
                            ('notiontool/engine.py', 3), ('notiontool/helper.py', 3)):
            path = os.path.join(self.temp.name, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as fh:
                fh.write('x\n' * lines)
        self.store = s = Store(os.path.join(self.temp.name, 'state'))
        s.put('accounts', dict(id=ACC, state='ready', bridge_connected=True, team_threads={}))
        s.put('workers', dict(id=OP, account_id=ACC, thread_id='op-thread', purpose='worker', enabled=False,
                              state='ready', lease='', label='Operator', join_state='joined'))
        for ident in (CO1, CO2, CO3):
            s.put('workers', dict(id=ident, account_id=ACC, thread_id=ident.split(':')[1], purpose='worker',
                                  enabled=True, state='ready', lease='', label='Welcome', join_state='joined'))
        self.engine = Engine(s, Adapter())
        self.chat = register_operator(s, OP, project_path=self.temp.name)['chat_id']

    def tearDown(self):
        self.engine.close()
        close = getattr(self.store, 'close', None)
        if close:
            close()
        self.temp.cleanup()

    # helpers -------------------------------------------------------------------------------------
    def settings(self, **values):
        self.store.put('settings', dict(self.store.get('settings', 'app', {}) or {}, id='app', **values))

    def run_(self, out):
        return self.store.get('runs', out['run_id'])

    def tasks(self, out):
        return self.engine.tasks(out['run_id'])

    def live(self, out):
        return [t for t in self.tasks(out) if t['state'] == 'queued']

    def finish(self, task, worker, result):
        task.update(state='done', worker_id=worker, result=result)
        self.store.put('tasks', task)

    def advance(self, out):
        self.engine._advance(self.run_(out))
        return self.run_(out)

    def first_review(self, files=('calc.txt',), acceptance='tests/test_x.py passes'):
        out = delegate(self.engine, self.chat, 'Refactor', [spec(files, acceptance)])
        (build,) = self.live(out)
        self.finish(build, CO1, DONE)
        self.advance(out)
        (review,) = self.live(out)
        self.assertEqual(review['role'], 'reviewer')
        return out, build, review

    def second_review(self):
        out, build, review = self.first_review()
        self.finish(review, CO2, FAIL)
        self.advance(out)
        (fixer,) = self.live(out)
        self.assertEqual(fixer['fix_of'], build['id'])
        self.finish(fixer, CO1, DONE)
        self.advance(out)
        (again,) = self.live(out)
        self.assertEqual(again['role'], 'reviewer')
        self.assertEqual(again['review_round'], 2)
        return out, build, fixer, again

    # review_model_policy -------------------------------------------------------------------------
    def test_auto_is_default_and_small_non_core_chain_gets_sonnet(self):
        self.assertNotIn('review_model_policy', self.store.get('settings', 'app', {}) or {})
        _, _, review = self.first_review()
        self.assertEqual(review['model'], 'sonnet')
        self.assertIn('review_model_policy auto', review['model_reason'])
        self.assertIn('sonnet review', review['model_reason'])
        self.assertIn('calc.txt', review['model_reason'])

    def test_auto_two_small_files_still_sonnet(self):
        _, _, review = self.first_review(files=('calc.txt', 'b.txt'))
        self.assertEqual(review['model'], 'sonnet')

    def test_auto_core_file_gets_opus(self):
        _, _, review = self.first_review(files=('notiontool/engine.py',))
        self.assertEqual(review['model'], 'opus')
        self.assertIn('core file changed (notiontool/engine.py)', review['model_reason'])

    def test_core_file_rule_matches_backslashes_and_absolute_paths(self):
        run = dict(id='r', project_path=self.temp.name)
        for name in ('notiontool\\engine.py', './notiontool/engine.py',
                     os.path.join(self.temp.name, 'notiontool', 'engine.py')):
            model, reason = review_model(self.engine, run, [dict(files=[name], acceptance='')], {})
            self.assertEqual(model, 'opus', name)
            self.assertIn('core file', reason)
        model, _ = review_model(self.engine, run, [dict(files=['notiontool/helper.py'], acceptance='')], {})
        self.assertEqual(model, 'sonnet')

    def test_default_core_files_list(self):
        self.assertEqual(set(REVIEW_CORE_FILES), {
            'notiontool/engine.py', 'notiontool/notion.py', 'notiontool/token_budget.py', 'notiontool/bridge.py',
            'notiontool/edge_transport.py', 'notiontool/server.py', 'notiontool/network_budget.py'})

    def test_review_core_files_setting_overrides_the_default(self):
        self.settings(review_core_files=['calc.txt'])
        _, _, review = self.first_review()
        self.assertEqual(review['model'], 'opus')
        self.assertIn('core file changed (calc.txt)', review['model_reason'])
        run = dict(id='r', project_path=self.temp.name)
        model, _ = review_model(self.engine, run, [dict(files=['notiontool/engine.py'], acceptance='')],
                                dict(review_core_files=[]))
        self.assertEqual(model, 'sonnet')  # an explicit empty list means: no core files

    def test_auto_more_than_two_files_gets_opus(self):
        _, _, review = self.first_review(files=('calc.txt', 'b.txt', 'c.txt'))
        self.assertEqual(review['model'], 'opus')
        self.assertIn('3 changed files', review['model_reason'])

    def test_auto_file_larger_than_acceptance_suggests_gets_opus(self):
        _, _, review = self.first_review(files=('big.txt',))
        self.assertEqual(review['model'], 'opus')
        self.assertIn('big.txt has 500 lines', review['model_reason'])
        run = dict(id='r', project_path=self.temp.name)
        model, _ = review_model(self.engine, run, [dict(files=['big.txt'], acceptance='big.txt stays at most 600 lines')], {})
        self.assertEqual(model, 'sonnet')
        model, _ = review_model(self.engine, run, [dict(files=['calc.txt'], acceptance='calc.txt at most 2 lines')], {})
        self.assertEqual(model, 'opus')
        model, _ = review_model(self.engine, run, [dict(files=['big.txt'], acceptance='')], dict(review_small_file_lines=1000))
        self.assertEqual(model, 'sonnet')

    def test_auto_unknown_size_or_no_files_gets_opus(self):
        run = dict(id='r', project_path=self.temp.name)
        self.assertEqual(review_model(self.engine, run, [dict(files=['missing.txt'], acceptance='')], {})[0], 'opus')
        self.assertEqual(review_model(self.engine, run, [dict(files=[], acceptance='')], {})[0], 'opus')

    def test_opus_policy_restores_old_behavior(self):
        self.settings(review_model_policy='opus')
        _, _, review = self.first_review()
        old = self.engine.add_task(self.run_(dict(run_id=review['run_id'])), 'reviewer', 'p', 'Review: x')
        self.assertEqual(review['model'], old['model'])
        self.assertEqual(review['model'], 'opus')
        self.assertEqual(review['model_reason'], old['model_reason'])
        self.assertNotIn('review_model_policy', review['model_reason'])
        self.assertEqual(review_model(self.engine, {}, [dict(files=['calc.txt'])], dict(review_model_policy='OPUS')),
                         (None, ''))

    def test_settings_read_defensively(self):
        class BrokenStore:
            def settings(self):
                raise RuntimeError('no settings')

        class Stub:
            store = BrokenStore()

            def _project_root(self, run):
                return run['project_path']

        run = dict(id='r', project_path=self.temp.name)
        self.assertEqual(review_model(Stub(), run, [dict(files=['calc.txt'], acceptance='')])[0], 'sonnet')
        self.assertEqual(review_model(Stub(), run, [dict(files=['calc.txt'])], dict(review_model_policy='weird'))[0],
                         'sonnet')  # unknown value = default 'auto'
        self.settings(review_model_policy=None, review_followup_scope=None)
        _, _, review = self.first_review()
        self.assertEqual(review['model'], 'sonnet')

    def test_fix_task_keeps_the_model_of_the_original_task(self):
        out = delegate(self.engine, self.chat, 'Refactor', [spec()])
        (build,) = self.live(out)
        build.update(model='sonnet', model_reason='planned sonnet')
        self.finish(build, CO1, DONE)
        self.advance(out)
        (review,) = self.live(out)
        self.finish(review, CO2, FAIL)
        self.advance(out)
        (fixer,) = self.live(out)
        self.assertEqual((fixer['model'], fixer['model_reason']), ('sonnet', 'planned sonnet'))

    # review_followup_scope -----------------------------------------------------------------------
    def test_round_two_prompt_is_fix_only_by_default(self):
        out, build, fixer, again = self.second_review()
        prompt = again['prompt']
        self.assertEqual(again.get('review_followup'), 'fix_only')
        self.assertIn('Follow-up review (round 2)', prompt)
        self.assertIn('Do not re-review the whole part', prompt)
        self.assertIn('Fix rounding', prompt)  # the previous fix list
        self.assertIn('Round half up in calc.txt', prompt)
        self.assertIn(fixer['title'], prompt)  # the last fixer
        self.assertIn('changed these files: calc.txt', prompt)
        self.assertIn('broke nothing', prompt)
        self.assertIn('tests/test_x.py', prompt)
        self.assertNotIn('Independently review', prompt)
        self.assertEqual(again['review_scope'], [build['id'], fixer['id']])

    def test_round_one_prompt_is_unchanged(self):
        out, build, review = self.first_review()
        self.assertEqual(review['prompt'], review_prompt('Refactor', [self.store.get('tasks', build['id'])]))
        self.assertNotIn('review_followup', review)

    def test_full_scope_restores_old_round_two_prompt(self):
        self.settings(review_followup_scope='full')
        out, build, fixer, again = self.second_review()
        chain = [self.store.get('tasks', build['id']), self.store.get('tasks', fixer['id'])]
        self.assertEqual(again['prompt'], review_prompt('Refactor', chain))
        self.assertIn('Independently review', again['prompt'])
        self.assertNotIn('review_followup', again)

    def test_followup_prompt_falls_back_to_fixer_prompt(self):
        chain = [dict(title='Part T1', acceptance='ok'),
                 dict(title='Fix 1: Part T1', files=['calc.txt'], acceptance='ok',
                      prompt='Do it\n\nThe independent review asked for these fixes; apply all of them to the same '
                             'files and re-run the listed tests:\n- Fix rounding')]
        from notiontool.operator_roles import _previous_fix_list
        self.assertEqual(_previous_fix_list(chain, {}), '- Fix rounding')
        text = followup_review_prompt('goal', chain, '- Fix rounding')
        self.assertIn('- Fix rounding', text)
        self.assertIn('Fix 1: Part T1', text)

    def test_fix_rounds_limit_unchanged_and_main_decides(self):
        self.assertEqual(MAX_FIX_ROUNDS, 1)  # owner 2026-10-23: one fix round (was 2)
        out = delegate(self.engine, self.chat, 'Refactor', [spec()])
        for _ in range(MAX_FIX_ROUNDS + 1):
            (build,) = self.live(out)
            self.finish(build, CO1, DONE)
            self.advance(out)
            (review,) = self.live(out)
            self.finish(review, CO2, FAIL)
            self.advance(out)
        self.assertEqual(len([t for t in self.tasks(out) if t.get('fix_of')]), MAX_FIX_ROUNDS)
        self.assertEqual(self.live(out), [])
        last = [t for t in self.tasks(out) if t['role'] == 'executor'][-1]
        self.assertEqual(self.store.get('tasks', last['id'])['review_state'], 'open')


if __name__ == '__main__':
    unittest.main()
