import copy
import json
import os
import re
import tempfile
import unittest

from notiontool.engine import Engine
from notiontool.operator_main import delegate, register_operator
from notiontool.protocol import validate_plan
from notiontool.store import Store
from notiontool.team_graph import MAX_TASKS, STAGE_ROLES, expand, graph_size

ID = re.compile(r'^[A-Za-z0-9_-]{1,48}$')
ACC = 'acc1'
OP = ACC + ':op-thread'
CO1, CO2, CO3 = ACC + ':co1', ACC + ':co2', ACC + ':co3'
DONE = json.dumps(dict(status='done', summary='Built it.\nRESULT: DONE'))
PASS = json.dumps(dict(passed=True, summary='APPROVED', fixes=[]))
FAIL = json.dumps(dict(passed=False, summary='Changes needed', fixes=[
    dict(id='F1', title='Fix rounding', prompt='Round in calc.txt', acceptance='rounded', files=['calc.txt'])]))


def spec(ident='T1', title=None, deps=(), files=(), prompt=None, **kw):
    t = dict(id=ident, title=title or 'Part ' + ident, prompt=prompt or 'Do ' + ident,
             acceptance='tests/test_x.py passes', depends_on=list(deps), files=list(files))
    t.update(kw)
    return t


def by_role(tasks, role):
    return [t for t in tasks if t.get('team', {}).get('stage_role') == role]


class ExpandTest(unittest.TestCase):
    def check_plan(self, out):
        ids = [t['id'] for t in out]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(ID.match(i) for i in ids), ids)
        self.assertLessEqual(len(out), MAX_TASKS)
        self.assertTrue(all(t['team']['stage_role'] in STAGE_ROLES for t in out))
        validate_plan({'tasks': out}, max_tasks=MAX_TASKS)

    def test_build_graph_design_critique_builds_integrator(self):
        tasks = [spec('A', files=['a.py']), spec('B', deps=['A'], files=['b.py'])]
        before = copy.deepcopy(tasks)
        out = expand(tasks, {'mode': 'team', 'reasons': ['x']}, 'build')
        self.assertEqual(tasks, before)  # input never mutated
        self.check_plan(out)
        (design,), (crit,), (integ,) = by_role(out, 'design'), by_role(out, 'design_critique'), by_role(out, 'integrate')
        builds = by_role(out, 'build')
        self.assertEqual([b['id'] for b in builds], ['A', 'B'])
        self.assertEqual(design['depends_on'], [])
        self.assertEqual(crit['depends_on'], [design['id']])
        self.assertEqual(builds[0]['depends_on'], [design['id'], crit['id']])  # builds see the design
        self.assertEqual(builds[1]['depends_on'], ['A', design['id'], crit['id']])  # user dependency kept
        self.assertEqual(integ['depends_on'], ['A', 'B', design['id'], crit['id']])  # integrator sees the design
        for t in (design, crit, integ):
            self.assertEqual((t['files'], t['team']['read_only']), ([], True))
        self.assertEqual(builds[0]['team'], {'stage_role': 'build', 'read_only': False})
        self.assertEqual(builds[0]['files'], ['a.py'])
        self.assertIn('architect', design['prompt'])
        self.assertIn(design['id'], crit['prompt'])

    def test_ui_build_gets_visual_design(self):
        (design,) = by_role(expand([spec('A', files=['web/app.css'])], {}, 'build'), 'design')
        self.assertEqual((design['title'], design['role']), ('Design: visual spec', 'designer'))

    def test_analysis_graph_streams_one_critique_per_part(self):
        tasks = [spec('P1'), spec('P2'), spec('P3')]
        out = expand(tasks, {'mode': 'team'}, 'analysis')
        self.check_plan(out)
        parts, crits, (synth,) = by_role(out, 'part'), by_role(out, 'critique'), by_role(out, 'synthesis')
        self.assertEqual([p['id'] for p in parts], ['P1', 'P2', 'P3'])
        self.assertTrue(all(p['depends_on'] == [] and p['team']['read_only'] for p in parts))
        self.assertEqual([c['depends_on'] for c in crits], [['P1'], ['P2'], ['P3']])  # only its own part
        self.assertEqual(set(synth['depends_on']), {'P1', 'P2', 'P3'} | {c['id'] for c in crits})
        self.assertEqual(len(out), graph_size(3, 'analysis'))

    def test_analysis_part_with_files_is_not_read_only(self):
        out = expand([spec('P1'), spec('P2', files=['x.py']), spec('P3', files=['y.py'], read_only=True)], {}, 'analysis')
        self.check_plan(out)
        parts = {p['id']: p['team']['read_only'] for p in by_role(out, 'part')}
        self.assertEqual(parts, {'P1': True, 'P2': False, 'P3': True})
        self.assertTrue(all(t['team']['read_only'] for t in out if t['team']['stage_role'] != 'part'))

    def test_same_titles_and_bad_or_long_ids_stay_unique_and_valid(self):
        long = 'x' * 48
        tasks = [spec('a b!', title='Same'), spec(long, title='Same', deps=['a b!']),
                 spec('team-design', title='Same')]
        for kind in ('build', 'analysis'):
            out = expand(tasks, {}, kind)
            self.check_plan(out)
            ids = [t['id'] for t in out]
            self.assertIn('a-b', ids)
            dependent = next(t for t in out if t['id'] == long)
            self.assertIn('a-b', dependent['depends_on'])  # remapped with the sanitized id
            self.assertEqual(ids.count('team-design'), 1)

    def test_alias_deps_are_tolerated(self):
        out = expand([spec('A'), dict(spec('B'), depends_on=[], after=['A'])], {}, 'build')
        b = next(t for t in out if t['id'] == 'B')
        self.assertIn('A', b['depends_on'])
        self.assertNotIn('after', b)

    def test_cap_fallback_returns_plain_copies(self):
        ok = expand([spec('T%d' % i) for i in range(37)], {}, 'build')
        self.assertEqual(len(ok), 40)
        self.check_plan(ok)
        over = [spec('T%d' % i) for i in range(38)]
        plain = expand(over, {}, 'build')
        self.assertEqual(plain, over)
        self.assertFalse(any('team' in t for t in plain))
        self.assertEqual(len(expand([spec('T%d' % i) for i in range(19)], {}, 'analysis')), 39)
        self.assertFalse(any('team' in t for t in expand([spec('T%d' % i) for i in range(20)], {}, 'analysis')))

    def test_bad_input_and_unknown_kind(self):
        self.assertEqual(expand([], {}, 'build'), [])
        dup = [spec('A'), spec('A')]
        self.assertFalse(any('team' in t for t in expand(dup, {}, 'build')))
        self.assertTrue(by_role(expand([spec('A')], None, 'weird'), 'integrate'))


class Adapter:
    pass


class TeamStepTest(unittest.TestCase):
    """operator_roles.step on TEAM tasks (top-level stage_role / read_only as Builder A copies them)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        open(os.path.join(self.temp.name, 'calc.txt'), 'w').close()
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

    def run_(self):
        return self.store.get('runs', self.out['run_id'])

    def get(self, ident):
        return self.store.get('tasks', ident)

    def tasks(self):
        return self.engine.tasks(self.out['run_id'])

    def reviewers(self):
        return [t for t in self.tasks() if t['role'] == 'reviewer' and t['state'] == 'queued']

    def finish(self, ident, worker, result):
        task = self.get(ident)
        task.update(state='done', worker_id=worker, result=result)
        self.store.put('tasks', task)

    def advance(self):
        self.engine._advance(self.run_())

    def team_run(self, prompt='Change calc.txt'):
        self.out = delegate(self.engine, self.chat, 'Refactor', [spec('T1', files=['calc.txt'], prompt=prompt, risk='medium')])
        self.assertEqual(self.out['quality'], 'standard')
        (build,) = [t for t in self.tasks() if t['role'] == 'executor']
        design = self.engine.add_task(self.run_(), 'executor', 'Design it. You are read-only.', 'Design: approach')
        spec_result = json.dumps(dict(status='done', summary='Spec: round half up in calc.txt\nRESULT: DONE'))
        design.update(stage_role='design', read_only=True, state='done', worker_id=CO2, result=spec_result)
        self.store.put('tasks', design)
        build['deps'] = list(build['deps']) + [design['id']]
        build['stage_role'] = 'build'
        self.store.put('tasks', build)
        integ = self.engine.add_task(self.run_(), 'executor', 'Integrate. You are read-only.', 'Integrate', deps=[build['id']])
        integ.update(stage_role='integrate', read_only=True)
        self.store.put('tasks', integ)
        return design['id'], build['id'], integ['id']

    def test_review_sees_design_and_integrator_waits_for_reviews_and_fixes(self):
        design, build, integ = self.team_run()
        self.finish(build, CO1, DONE)
        self.advance()
        self.assertEqual(self.get(design)['review_state'], 'skipped')  # read-only stage task, never approved
        (review,) = self.reviewers()
        self.assertEqual(review['deps'], [build, design])
        self.assertEqual(review['review_scope'], [design, build])
        scope = [t for t in self.tasks() if t['state'] == 'done' and t['id'] in review['review_scope']]
        self.assertIn('round half up', self.engine._context(scope))  # the reviewer's context holds the design result
        self.assertIn('follows them', review['prompt'])
        gate = self.get(integ)
        self.assertIn(review['id'], gate['deps'])
        self.assertTrue(gate.get('team_hold'))
        self.assertGreater(gate['next_at'], self.engine.clock())
        self.finish(review['id'], CO2, FAIL)
        self.advance()
        fixer = self.get(self.get(build)['fixed_by'])
        self.assertIn(fixer['id'], self.get(integ)['deps'])
        self.finish(fixer['id'], CO1, DONE)
        self.advance()
        (again,) = self.reviewers()
        self.assertEqual(again['review_scope'], [design, build, fixer['id']])
        self.assertIn(design, again['deps'])
        self.assertIn(again['id'], self.get(integ)['deps'])
        self.finish(again['id'], CO3, PASS)
        self.advance()
        self.assertEqual(self.get(fixer['id'])['review_state'], 'approved')
        gate = self.get(integ)
        self.assertFalse(gate.get('team_hold'))
        self.assertLessEqual(gate['next_at'], self.engine.clock())
        self.assertEqual(gate['state'], 'queued')

    def test_read_only_build_review_is_skipped_not_approved(self):
        self.out = delegate(self.engine, self.chat, 'Audit',
                            [spec('T1', prompt='Read-only audit of calc.txt', risk='medium')])
        (build,) = [t for t in self.tasks() if t['role'] == 'executor']
        self.finish(build['id'], CO1, DONE)
        self.advance()
        task = self.get(build['id'])
        self.assertEqual(task['review_state'], 'skipped')
        self.assertNotEqual(task['review_state'], 'approved')
        self.assertIn('read-only', task['review_skipped'])
        self.assertFalse(any(t['role'] == 'reviewer' for t in self.tasks()))


if __name__ == '__main__':
    unittest.main()
