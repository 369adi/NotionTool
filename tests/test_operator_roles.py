import json
import os
import tempfile
import unittest

from notiontool.engine import Engine
from notiontool.operator_main import delegate, register_operator
from notiontool.operator_roles import MAX_FIX_ROUNDS, choose_quality, run_stage, stage_of, step
from notiontool.store import Store

ACC = 'acc1'
OP = ACC + ':op-thread'
CO1, CO2, CO3 = ACC + ':co1', ACC + ':co2', ACC + ':co3'
DONE = json.dumps(dict(status='done', summary='Built it.\nRESULT: DONE'))
PASS = json.dumps(dict(passed=True, summary='APPROVED', fixes=[]))
FAIL = json.dumps(dict(passed=False, summary='Changes needed', fixes=[
    dict(id='F1', title='Fix spacing', prompt='Use the 8px grid in web/app.css', acceptance='grid used', files=['web/app.css'])]))


class Adapter:  # fake client: no Notion call is ever made, the test completes the tasks
    pass


def spec(ident='T1', files=(), risk='', title='Part'):
    t = dict(id=ident, title=title + ' ' + ident, prompt='Do ' + ident, acceptance='tests/test_x.py passes',
             depends_on=[], files=list(files))
    if risk:
        t['risk'] = risk
    return t


class OperatorRolesTest(unittest.TestCase):
    def setUp(self, coworkers=(CO1, CO2, CO3)):
        self.temp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.temp.name, 'web'))
        for name in ('web/app.css', 'calc.txt'):
            open(os.path.join(self.temp.name, name), 'w').close()
        self.store = s = Store(os.path.join(self.temp.name, 'state'))
        s.put('accounts', dict(id=ACC, state='ready', bridge_connected=True, team_threads={}))
        s.put('workers', dict(id=OP, account_id=ACC, thread_id='op-thread', purpose='worker', enabled=False,
                              state='ready', lease='', label='Operator', join_state='joined'))
        for ident in coworkers:
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

    def delegate(self, goal, tasks, **kw):
        return delegate(self.engine, self.chat, goal, tasks, **kw)

    def run_(self, out):
        return self.store.get('runs', out['run_id'])

    def tasks(self, out):
        return self.engine.tasks(out['run_id'])

    def live(self, out):
        return [t for t in self.tasks(out) if t['state'] == 'queued']

    def pick(self, task, out):
        return self.engine._choose_worker(task, self.run_(out), self.store.settings())

    def finish(self, task, worker, result):
        task.update(state='done', worker_id=worker, result=result)
        self.store.put('tasks', task)

    def advance(self, out):
        self.engine._advance(self.run_(out))
        return self.run_(out)

    def test_quality_auto_selection_and_explicit_field(self):
        self.assertEqual(choose_quality('Make the UI better', [spec()])[0], 'high')
        self.assertEqual(choose_quality('Speed up', [spec(files=['web/app.css'])])[0], 'high')
        self.assertEqual(choose_quality('Refactor', [spec(risk='high')])[0], 'high')
        self.assertEqual(choose_quality('Refactor', [spec(risk='medium')])[0], 'standard')
        self.assertEqual(choose_quality('Refactor', [spec(files=['a.txt', 'b.txt'])])[0], 'standard')
        self.assertEqual(choose_quality('Refactor', [spec(files=['notiontool/x.py'])])[0], 'standard')
        self.assertEqual(choose_quality('Add 2 and 3', [spec()])[0], 'fast')
        self.assertEqual(choose_quality('Make the UI better', [spec()], 'fast'), ('fast', 'requested'))
        with self.assertRaises(ValueError):
            choose_quality('x', [spec()], 'best')
        out = self.delegate('Add numbers', [dict(spec(), quality='standard')])
        self.assertEqual((out['quality'], self.run_(out)['quality']), ('standard', 'standard'))
        self.assertNotIn('quality', self.tasks(out)[0])
        self.assertEqual(self.delegate('Add more', [spec()], quality='high')['quality'], 'high')

    def test_fast_mode_unchanged(self):
        out = self.delegate('Add 2 and 3', [spec()])
        self.assertEqual(out['quality'], 'fast')
        (task,) = self.tasks(out)
        self.assertEqual((task['role'], task['deps']), ('executor', []))
        self.finish(task, CO1, DONE)
        run = self.advance(out)
        self.assertEqual([t['role'] for t in self.tasks(out)], ['executor'])
        self.assertIn(run['state'], ('done', 'awaiting_main', 'needs_main'))
        self.assertIn('1 accepted with RESULT: DONE', run['summary'])

    def test_stage_words(self):
        self.assertEqual(stage_of(dict(role='reviewer')), 'Reviewing')
        self.assertEqual(stage_of(dict(role='executor', fix_of='t1')), 'Fixing')
        self.assertEqual(stage_of(dict(role='architect', stage_role='design')), 'Designing')
        self.assertEqual(run_stage([dict(role='executor', state='queued')]), 'Building')

    def test_high_stage_order_design_build_review_fix_review(self):
        out = self.delegate('Make the UI better', [spec(files=['web/app.css'])])
        self.assertEqual((out['quality'], out['stage']), ('high', 'Designing'))
        design = next(t for t in self.tasks(out) if t.get('stage_role') == 'design')
        build = next(t for t in self.tasks(out) if t['role'] == 'executor')
        self.assertEqual((design['role'], design['specialty']), ('architect', 'designer'))
        self.assertIn('spacing grid', design['prompt'])
        self.assertIn(design['id'], build['deps'])
        self.assertNotEqual(self.pick(design, out)['id'], OP)
        stages = [self.run_(out)['stage']]
        self.finish(design, CO2, json.dumps(dict(status='done', summary='Spec: 8px grid, #1f6feb')))
        self.assertIn('8px grid', self.engine._context([self.store.get('tasks', d) for d in build['deps']]))
        self.advance(out)
        stages.append(self.run_(out)['stage'])
        self.finish(self.store.get('tasks', build['id']), CO1, DONE)
        self.advance(out)
        stages.append(self.run_(out)['stage'])
        (review,) = self.live(out)
        self.assertEqual((review['role'], review['files'], review['review_of']), ('reviewer', [], build['id']))
        self.assertIn('web/app.css', review['prompt'])
        self.assertIn('tests/test_x.py', review['prompt'])
        self.assertNotIn(self.pick(review, out)['id'], (CO1, OP))
        self.finish(review, CO2, FAIL)
        self.advance(out)
        stages.append(self.run_(out)['stage'])
        (fixer,) = self.live(out)
        self.assertEqual((fixer['role'], fixer['files'], fixer['prefer_worker']), ('executor', ['web/app.css'], CO1))
        self.assertIn('8px grid', fixer['prompt'])
        self.assertEqual(self.pick(fixer, out)['id'], CO1)
        self.finish(fixer, CO1, DONE)
        self.advance(out)
        stages.append(self.run_(out)['stage'])
        (again,) = self.live(out)
        self.assertEqual(again['review_scope'], [build['id'], fixer['id']])
        self.finish(again, CO3, PASS)
        run = self.advance(out)
        self.assertEqual(stages, ['Designing', 'Building', 'Reviewing', 'Fixing', 'Reviewing'])
        self.assertNotEqual(run['state'], 'executing')
        self.assertEqual(run['needs_main_reasons'], {})
        self.assertIn('1 accepted with RESULT: DONE', run['summary'])

    def test_fix_loop_limit_marks_needs_main_with_open_points(self):
        out = self.delegate('Refactor', [spec(files=['calc.txt'], risk='medium')])
        self.assertEqual(out['quality'], 'standard')
        self.assertFalse(any(t.get('stage_role') == 'design' for t in self.tasks(out)))
        for _ in range(MAX_FIX_ROUNDS + 1):
            (build,) = self.live(out)
            self.assertEqual(build['role'], 'executor')
            self.finish(build, CO1, DONE)
            self.advance(out)
            (review,) = self.live(out)
            self.assertEqual(review['role'], 'reviewer')
            self.finish(review, CO2, FAIL)
            self.advance(out)
        run = self.run_(out)
        self.assertEqual(len([t for t in self.tasks(out) if t.get('fix_of')]), MAX_FIX_ROUNDS)
        self.assertEqual(self.live(out), [])
        self.assertNotEqual(run['state'], 'executing')
        (reason,) = run['needs_main_reasons'].values()
        self.assertIn('Review open points', reason)
        self.assertIn('Fix spacing', reason)

    def test_read_only_roles_hold_no_locks(self):
        out = self.delegate('Make the UI better', [spec(files=['web/app.css'])])
        design = next(t for t in self.tasks(out) if t.get('stage_role') == 'design')
        self.assertEqual((design['files'], design['read_only']), ([], True))
        build = next(t for t in self.tasks(out) if t['role'] == 'executor')
        build.update(state='running', worker_id=CO1, lease='B')
        self.store.put('tasks', build)
        design.update(state='running', worker_id=CO2, lease='L')
        self.store.put('tasks', design)
        self.assertFalse(self.engine._conflicts(design, self.run_(out)))
        with self.assertRaisesRegex(ValueError, 'read-only'):
            self.engine.claim_file(design['id'], 'L', 'web/app.css')
        self.finish(self.store.get('tasks', design['id']), CO2, json.dumps(dict(status='done', summary='Spec text here')))
        self.finish(self.store.get('tasks', build['id']), CO1, DONE)
        other = self.engine.add_task(self.run_(out), 'executor', 'more', files=['web/app.css'])
        other.update(state='running', worker_id=CO3, lease='O')
        self.store.put('tasks', other)
        step(self.engine, self.run_(out), self.tasks(out))
        review = next(t for t in self.tasks(out) if t['role'] == 'reviewer')
        self.assertEqual(review['files'], [])
        self.assertFalse(self.engine._conflicts(review, self.run_(out)))

    def test_review_needs_a_different_coworker(self):
        self.tearDown()
        self.setUp(coworkers=(CO1,))
        out = self.delegate('Refactor', [spec(files=['calc.txt'], risk='medium')])
        (build,) = self.live(out)
        self.finish(build, CO1, DONE)
        run = self.advance(out)
        self.assertFalse(any(t['role'] == 'reviewer' for t in self.tasks(out)))
        self.assertIn('No second Coworker', list(run['needs_main_reasons'].values())[0])


if __name__ == '__main__':
    unittest.main()
