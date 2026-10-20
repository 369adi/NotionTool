"""Automatic DIRECT/TEAM routing for operator delegations (team_route + operator_main.delegate).

team_graph.expand belongs to a parallel builder, so every test runs against a stub module.
"""
import copy
import sys
import tempfile
import types
import unittest
from unittest import mock

from notiontool import team_route
from notiontool.engine import Engine
from notiontool.operator_main import delegate, register_operator
from notiontool.store import Store
from notiontool.team_route import capacity_hint, decide, kind_of, match_created

ACC = 'acc1'
OP = ACC + ':op-thread'
MAIN = ACC + ':main-thread'
CO1, CO2 = ACC + ':co1', ACC + ':co2'
GRAPH_IDS = ('team-design', 'team-design-crit')


class Adapter:
    pass


def tasks(*idents, deps=None, **extra):
    deps = deps or {}
    return [dict(id=i, title='Task ' + i, prompt='Do ' + i, acceptance='Report ' + i, depends_on=deps.get(i, []),
                 **extra) for i in idents]


def base(score, reasons=('stub',)):
    return mock.patch('notiontool.effort_policy.route_for_request',
                      return_value=dict(route='team' if score >= 0.55 else 'direct', raised=False, score=score,
                                        reasons=list(reasons), reason=''))


class FakeGraph:
    """Stub of team_graph.expand with builder B's contract (build graph: design, critique, builds, integrate)."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def expand(self, tasks, decision, kind):
        self.calls.append(dict(tasks=copy.deepcopy(tasks), decision=dict(decision), kind=kind))
        if self.fail:
            raise RuntimeError('graph broke')
        out = copy.deepcopy(tasks)
        for task in out:
            task.pop('team', None)
        if decision.get('mode') != 'team' or not decision.get('enforced') or kind not in ('build', 'analysis'):
            return out
        if len(out) + 3 > 40:
            return out
        design = dict(id='team-design', title='Team design', prompt='Design the approach', acceptance='Design doc',
                      depends_on=[], files=[], role='architect', team=dict(stage_role='design', read_only=True))
        crit = dict(id='team-design-crit', title='Team design critique', prompt='Critique the design',
                    acceptance='Findings', depends_on=['team-design'], files=[], role='critic',
                    team=dict(stage_role='design_critique', read_only=True))
        for task in out:
            task['depends_on'] = list(task.get('depends_on') or []) + list(GRAPH_IDS)
            task['team'] = dict(stage_role='build', read_only=False)
        integrate = dict(id='team-integrate', title='Team integrate', prompt='Integrate the builds',
                         acceptance='Integrated', depends_on=[t['id'] for t in out] + ['team-design'], files=[],
                         role='integrator', team=dict(stage_role='integrate', read_only=False))
        return [design, crit] + out + [integrate]


class StubbedGraph(unittest.TestCase):
    def stub(self, graph):
        module = types.ModuleType('notiontool.team_graph')
        module.expand = graph.expand
        patcher = mock.patch.dict(sys.modules, {'notiontool.team_graph': module})
        patcher.start()
        self.addCleanup(patcher.stop)
        return graph


class DecideTest(StubbedGraph):
    def test_typo_fix_is_direct(self):
        work = [dict(id='a', title='Fix typo', prompt='Fix the typo in README.md line 3',
                     acceptance='Typo fixed', files=['README.md'])]
        route = decide('Fix a typo in README.md', work, 'fast', {})
        self.assertEqual(route['mode'], 'direct', route)
        self.assertFalse(route['enforced'])
        self.assertIn('concrete_single -0.15', route['reasons'])
        self.assertTrue(any(r.startswith('base: ') for r in route['reasons']), route['reasons'])

    def test_ui_word_forces_team(self):
        with base(0.0):
            route = decide('Polish it', [dict(id='a', title='Restyle the sidebar buttons', prompt='p',
                                              acceptance='x')], 'fast', {})
        self.assertEqual(route['mode'], 'team')
        self.assertIn('hard:ui', route['reasons'])
        with base(0.0):
            route = decide('Polish it', [dict(id='a', title='t', prompt='p', acceptance='x',
                                              files=['web/app.js'])], 'fast', {})
        self.assertIn('hard:ui', route['reasons'])

    def test_high_risk_forces_team(self):
        with base(0.0):
            route = decide('Goal', tasks('a', risk='high'), 'high', {})
        self.assertEqual((route['mode'], route['enforced'], route['setting']), ('team', False, 'log_only'))
        self.assertIn('hard:risk_high', route['reasons'])

    def test_threshold_boundary(self):
        # Two tasks without deps: only multi_task +0.10 applies.
        with base(0.45):
            self.assertEqual((decide('Goal', tasks('a', 'b'), '', {})['score'],
                              decide('Goal', tasks('a', 'b'), '', {})['mode']), (0.55, 'team'))
        with base(0.44):
            route = decide('Goal', tasks('a', 'b'), '', {})
        self.assertEqual((route['score'], route['mode'], route['threshold']), (0.54, 'direct', 0.55))
        with base(0.44):
            self.assertEqual(decide('Goal', tasks('a', 'b'), '', {'team_route_threshold': 0.5})['mode'], 'team')

    def test_each_adjustment_named(self):
        work = tasks('a', 'b', deps={'b': ['a']})
        work[0]['risk'] = 'medium'
        work[1]['acceptance'] = ''
        with base(0.1):
            route = decide('Goal', work, '', {})
        for reason in ('deps_chain +0.15', 'multi_task +0.10', 'risk_medium +0.10', 'no_acceptance +0.10'):
            self.assertIn(reason, route['reasons'])
        self.assertEqual(route['score'], 0.55)
        with base(0.5):
            single = decide('Goal', tasks('a'), '', {})
        self.assertIn('concrete_single -0.15', single['reasons'])
        self.assertEqual((single['score'], single['mode']), (0.35, 'direct'))
        with base(0.5):
            alias = decide('Goal', [dict(tasks('a')[0]), dict(tasks('b')[0], after=['a'])], '', {})
        self.assertIn('deps_chain +0.15', alias['reasons'])

    def test_decide_never_raises(self):
        for goal, work in ((None, ['not a dict']), ('Goal', None), ('Goal', []), (None, 'x')):
            route = decide(goal, work, '', {})
            self.assertEqual((route['mode'], route['enforced']), ('direct', False))
            self.assertTrue(route['reasons'][0].startswith('route_error: '), route)
        with mock.patch('notiontool.effort_policy.route_for_request', side_effect=RuntimeError('boom')):
            route = decide('Goal', tasks('a'), '', {'team_route_mode': 'enforce'})
        self.assertEqual(route['reasons'], ['route_error: boom'])
        self.assertEqual(decide(None, tasks('a'), '', None)['setting'], 'log_only')
        self.assertEqual(decide('Goal', tasks('a'), '', {'team_route_mode': 'bogus'})['setting'], 'log_only')

    def test_overrides_and_explicit_fast(self):
        with base(0.9):
            self.assertEqual(decide('Goal', tasks('a', 'b'), '', {}, route='direct')['mode'], 'direct')
            fast = decide('Goal', tasks('a', 'b'), 'fast', {'team_route_mode': 'enforce'}, explicit=True)
            chosen_fast = decide('Goal', tasks('a', 'b'), 'fast', {'team_route_mode': 'enforce'})
            forced = decide('Goal', tasks('a', 'b'), 'fast', {}, route='team', explicit=True)
        self.assertEqual((fast['mode'], fast['enforced']), ('direct', False))
        self.assertIn('explicit_fast', fast['reasons'])
        self.assertEqual((chosen_fast['mode'], chosen_fast['enforced']), ('team', True))
        self.assertEqual((forced['mode'], forced['enforced'], forced['route']), ('team', True, 'team'))
        self.assertIn('forced:team', forced['reasons'])
        with base(0.0):
            low = decide('Goal', tasks('a'), '', {}, route='team')
        self.assertEqual((low['mode'], low['enforced']), ('team', True))

    def test_kind_of(self):
        self.assertEqual(kind_of('Read-only audit of server.py', tasks('a')), 'analysis')
        self.assertEqual(kind_of('Read-only audit', tasks('a', files=['x.py'])), 'build')
        self.assertEqual(kind_of('Fix things', tasks('a', 'b', task_class='analysis')), 'analysis')
        self.assertEqual(kind_of('Fix things', tasks('a')), 'build')

    def test_capacity_hint_warns(self):
        big = tasks('a', 'b')
        big[0]['files'] = ['x.py', 'y.py', 'z.py']
        hint = capacity_hint(3, 1, big)
        self.assertEqual(hint, 'Task a looks big and 2 coworker(s) are idle: consider splitting it into '
                               'independent parts.')
        long_prompt = tasks('c')
        long_prompt[0]['prompt'] = 'x' * 1501
        self.assertIn('Task c looks big', capacity_hint(2, 1, long_prompt))
        many = tasks('d')
        many[0]['acceptance'] = '1\n2\n3\n4'
        self.assertIn('Task d looks big', capacity_hint(2, 1, many))

    def test_capacity_hint_silent(self):
        big = tasks('a')
        big[0]['files'] = ['x.py', 'y.py', 'z.py']
        self.assertIsNone(capacity_hint(1, 1, big))  # nobody idle
        self.assertIsNone(capacity_hint(2, 3, big))
        self.assertIsNone(capacity_hint(4, 1, tasks('a', 'b')))  # idle, but nothing is big
        self.assertIsNone(capacity_hint('x', 1, big))
        self.assertIsNone(capacity_hint(3, 1, big, files=4))

    def test_match_created_is_not_positional(self):
        specs = [dict(id='a', title='Same', prompt='one', acceptance='x', files=[], depends_on=[]),
                 dict(id='b', title='Same', prompt='two', acceptance='x', files=[], depends_on=['a'])]
        created = [dict(id='E2', title='Same', prompt='two', acceptance='x', files=[], deps=['E1']),
                   dict(id='E1', title='Same', prompt='one', acceptance='x', files=[], deps=[])]
        out = match_created(specs, created)
        self.assertEqual((out['a']['id'], out['b']['id']), ('E1', 'E2'))


class DelegateRouteTest(StubbedGraph):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s = Store(self.temp.name)
        s.put('accounts', dict(id=ACC, state='ready', team_threads={'main': {'id': 'main-thread', 'pending': False}}))
        s.put('workers', dict(id=OP, account_id=ACC, thread_id='op-thread', purpose='worker', enabled=False,
                              state='ready', lease='', label='Welcome to Notion', welcome=True, join_state='joined'))
        s.put('workers', dict(id=MAIN, account_id=ACC, thread_id='main-thread', purpose='main', enabled=True,
                              state='ready', lease='', label='Welcome to Notion'))
        for ident in (CO1, CO2):
            s.put('workers', dict(id=ident, account_id=ACC, thread_id=ident.split(':')[1], purpose='worker',
                                  enabled=True, state='ready', lease='', label='Welcome to Notion', join_state='joined'))
        s.put('chats', dict(id='c4', title='Main 4', main_worker=MAIN, state='active'))
        self.engine = Engine(s, Adapter())
        self.chat_id = register_operator(s, OP, project_path=self.temp.name)['chat_id']
        self.graph = self.stub(FakeGraph())

    def tearDown(self):
        self.engine.close()
        close = getattr(self.store, 'close', None)
        if close:
            close()
        self.temp.cleanup()

    def setting(self, **values):
        settings = self.store.settings()
        settings.update(values)
        self.store.put('settings', settings)

    def run_of(self, result):
        return self.store.get('runs', result['run_id'])

    def shape(self, result):
        run = self.run_of(result)
        own = sorted(self.engine.tasks(run['id']), key=lambda t: (t['title'], t['prompt']))
        index = {t['id']: t['title'] for t in own}
        rows = [(t['role'], t['title'], t['prompt'], t.get('files'), t.get('acceptance'),
                 sorted(index[d] for d in t.get('deps') or []), t.get('stage_role'), t.get('read_only'),
                 t.get('summary'), t.get('requested_effort')) for t in own]
        return dict(tasks=rows, quality=run['quality'], quality_reason=run['quality_reason'],
                    allocation=run['allocation'], stage=run['stage'], worker_budget=run['worker_budget'],
                    state=run['state'], warnings=result['warnings'])

    def test_default_is_log_only(self):
        cases = [('Goal', lambda: tasks('a', 'b', 'c', deps={'c': ['a', 'b']})),
                 ('Risky change', lambda: tasks('a', 'b', risk='high')),
                 ('Fix one file', lambda: tasks('a', files=['one.py']))]
        for goal, make in cases:
            self.setting(team_route_mode='off')
            before = delegate(self.engine, self.chat_id, goal, make())
            self.setting(team_route_mode='log_only')
            self.graph.calls.clear()
            after = delegate(self.engine, self.chat_id, goal, make())
            self.assertEqual(self.shape(after), self.shape(before), goal)
            self.assertIsNone(before['route'])
            self.assertEqual(after['route']['setting'], 'log_only')
            self.assertFalse(after['route']['enforced'])
            self.assertTrue(all(c['decision']['enforced'] for c in self.graph.calls))  # only the graph_size probe
        self.setting(team_route_mode='')  # unset -> log-only default
        self.assertEqual(delegate(self.engine, self.chat_id, 'Goal', tasks('z'))['route']['setting'], 'log_only')

    def test_route_saved_next_to_allocation(self):
        with base(0.9):
            result = delegate(self.engine, self.chat_id, 'Goal', tasks('a', 'b'))
        run = self.run_of(result)
        self.assertEqual(run['allocation']['mode'], 'operator')
        route = run['route']
        for key in ('mode', 'score', 'reasons', 'enforced', 'setting', 'threshold', 'kind', 'route', 'tasks_in',
                    'graph_size'):
            self.assertIn(key, route)
        self.assertEqual((route['mode'], route['enforced'], route['tasks_in'], route['graph_size']),
                         ('team', False, 2, 5))
        self.assertEqual(len(self.engine.tasks(run['id'])), 2)  # log-only: the graph is only measured

    def test_delegate_response_fields(self):
        result = delegate(self.engine, self.chat_id, 'Goal', tasks('a'))
        run = self.run_of(result)
        self.assertEqual(result['route'], run['route'])
        self.assertEqual(result['capacity'], run['capacity_hint'])
        self.assertEqual(set(result['capacity']), {'free', 'startable', 'hint'})
        self.assertEqual((result['capacity']['free'], result['capacity']['startable']), (2, 1))
        for key in ('ok', 'run_id', 'state', 'chat_id', 'tasks', 'quality', 'stage', 'duplicate', 'warnings'):
            self.assertIn(key, result)

    def test_enforce_expands_graph(self):
        self.setting(team_route_mode='enforce')
        with base(0.9):
            result = delegate(self.engine, self.chat_id, 'Goal', tasks('a', 'b'))
        run = self.run_of(result)
        own = self.engine.tasks(run['id'])
        self.assertEqual(len(own), 5)
        by_role = {}
        for task in own:
            by_role.setdefault(task.get('stage_role'), []).append(task)
        self.assertEqual(sorted((k, len(v)) for k, v in by_role.items()),
                         [('build', 2), ('design', 1), ('design_critique', 1), ('integrate', 1)])
        design = by_role['design'][0]
        self.assertEqual((design['read_only'], design['files']), (True, []))
        for build in by_role['build']:
            self.assertIn(design['id'], build['deps'])
            self.assertIn(by_role['design_critique'][0]['id'], build['deps'])
            self.assertIs(build['read_only'], False)
        self.assertEqual((run['route']['enforced'], run['route']['graph_size']), (True, 5))
        self.assertEqual(run['quality'], 'standard')  # raised so the review step runs
        self.assertIn('raised from fast to standard', run['quality_reason'])
        self.assertEqual(run['stage'], 'Designing')
        self.assertEqual(self.graph.calls[-1]['kind'], 'build')
        self.assertNotIn('team', ''.join(result['warnings']))  # team/route are known keys

    def test_alias_dep_kept_with_enforce(self):
        self.setting(team_route_mode='enforce')
        work = tasks('a', 'b')
        work[1].pop('depends_on')
        work[1]['after'] = ['a']
        with base(0.9):
            result = delegate(self.engine, self.chat_id, 'Goal', work)
        self.assertEqual(self.graph.calls[-1]['tasks'][1]['depends_on'], ['a'])  # expand saw the real dep
        own = {t['title']: t for t in self.engine.tasks(result['run_id'])}
        self.assertIn(own['Task a']['id'], own['Task b']['deps'])
        self.assertIn(own['Team design']['id'], own['Task b']['deps'])

    def test_same_title_builds_mapped(self):
        for mode in ('log_only', 'enforce'):
            self.setting(team_route_mode=mode)
            work = [dict(id='a', title='Same', prompt='Do one', acceptance='x', summary='First part', effort='low'),
                    dict(id='b', title='Same', prompt='Do two', acceptance='x', summary='Second part', effort='high')]
            with base(0.9):
                result = delegate(self.engine, self.chat_id, 'Goal', work)
            own = [t for t in self.engine.tasks(result['run_id']) if t['title'] == 'Same']
            got = sorted((t['prompt'].split('\n')[0], t.get('summary'), t.get('requested_effort')) for t in own)
            self.assertEqual(got, [('Do one', 'First part', 'low'), ('Do two', 'Second part', 'high')], mode)

    def test_over_cap_falls_back(self):
        self.setting(team_route_mode='enforce')
        work = tasks(*['t%d' % i for i in range(38)])
        work[0]['risk'] = 'high'
        result = delegate(self.engine, self.chat_id, 'Goal', work)
        run = self.run_of(result)
        self.assertEqual((run['route']['mode'], run['route']['enforced']), ('direct', False))
        self.assertIn('graph_over_cap', run['route']['reasons'])
        executors = [t for t in self.engine.tasks(run['id']) if t['role'] == 'executor']
        self.assertEqual(len(executors), 38)
        self.assertFalse(any(t.get('stage_role') == 'build' for t in executors))

    def test_expand_errors_never_block(self):
        self.stub(FakeGraph(fail=True))
        with base(0.9):
            logged = delegate(self.engine, self.chat_id, 'Goal', tasks('a', 'b'))
        self.assertIsNone(logged['route']['graph_size'])
        self.assertIn('graph_size_error: graph broke', logged['route']['reasons'])
        self.assertEqual(len(self.engine.tasks(logged['run_id'])), 2)
        self.setting(team_route_mode='enforce')
        with base(0.9):
            enforced = delegate(self.engine, self.chat_id, 'Goal 2', tasks('a', 'b'))
        self.assertEqual((enforced['route']['mode'], enforced['route']['enforced']), ('direct', False))
        self.assertIn('graph_error: graph broke', enforced['route']['reasons'])
        with mock.patch.dict(sys.modules, {'notiontool.team_graph': None}), base(0.9):
            missing = delegate(self.engine, self.chat_id, 'Goal 3', tasks('a', 'b'))
        self.assertTrue(any(r.startswith('graph_error: ') for r in missing['route']['reasons']), missing['route'])
        self.assertEqual(self.run_of(missing)['state'], 'executing')

    def test_route_direct_skips_expand(self):
        self.setting(team_route_mode='enforce')
        with base(0.9):
            result = delegate(self.engine, self.chat_id, 'Goal', tasks('a', 'b', risk='high'), route='direct')
        self.assertEqual(self.graph.calls, [])
        self.assertEqual((result['route']['mode'], result['route']['route']), ('direct', 'direct'))
        self.assertIn('forced:direct', result['route']['reasons'])
        self.assertEqual(len([t for t in self.engine.tasks(result['run_id']) if t['role'] == 'executor']), 2)

    def test_route_team_forces_in_log_only(self):
        work = tasks('a')
        work[0]['route'] = 'team'  # accepted as a task field, like quality
        with base(0.0):
            result = delegate(self.engine, self.chat_id, 'Goal', work)
        self.assertEqual((result['route']['mode'], result['route']['enforced']), ('team', True))
        self.assertEqual(len(self.engine.tasks(result['run_id'])), 4)
        self.assertEqual(result['warnings'], ['Setting advertise_project_tools is off: Coworkers get no project tools'])
        with self.assertRaisesRegex(ValueError, 'route must be'):
            delegate(self.engine, self.chat_id, 'Goal', tasks('b'), route='sideways')

    def test_explicit_fast_stays_direct(self):
        self.setting(team_route_mode='enforce')
        with base(0.9):
            result = delegate(self.engine, self.chat_id, 'Goal', tasks('a', 'b'), quality='fast')
        run = self.run_of(result)
        self.assertEqual((run['route']['mode'], run['quality']), ('direct', 'fast'))
        self.assertIn('explicit_fast', run['route']['reasons'])
        self.assertEqual(self.graph.calls, [])

    def test_team_skips_legacy_design(self):
        self.setting(team_route_mode='enforce')
        result = delegate(self.engine, self.chat_id, 'Goal', tasks('a', 'b', risk='high'))
        run = self.run_of(result)
        self.assertEqual(run['quality'], 'high')
        designs = [t for t in self.engine.tasks(run['id']) if t.get('stage_role') == 'design']
        self.assertEqual([t['title'] for t in designs], ['Team design'])
        self.assertFalse(any(t['role'] == 'architect' for t in self.engine.tasks(run['id'])))

    def test_log_only_high_keeps_one_legacy_design(self):
        result = delegate(self.engine, self.chat_id, 'Goal', tasks('a', 'b', risk='high'))
        run = self.run_of(result)
        designs = [t for t in self.engine.tasks(run['id']) if t.get('stage_role') == 'design']
        self.assertEqual(len(designs), 1)
        self.assertEqual((designs[0]['role'], designs[0]['title']), ('architect', 'Design: approach'))
        self.assertEqual(run['stage'], 'Designing')
        self.assertEqual(run['route']['graph_size'], 5)  # measured, not applied

    def test_duplicate_keeps_route(self):
        with base(0.9):
            first = delegate(self.engine, self.chat_id, 'Goal', tasks('a', 'b'), request_id='req-1')
        with base(0.0):
            again = delegate(self.engine, self.chat_id, 'Goal', tasks('a', 'b'), request_id='req-1')
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['route'], first['route'])
        self.assertEqual(again['capacity'], first['capacity'])

    def test_capacity_hint_in_response_not_warnings(self):
        work = tasks('a')
        work[0]['files'] = ['one.txt', 'two.txt', 'three.txt']
        result = delegate(self.engine, self.chat_id, 'Goal', work)
        self.assertEqual(result['capacity']['hint'], 'Task a looks big and 1 coworker(s) are idle: consider '
                                                     'splitting it into independent parts.')
        self.assertNotIn(result['capacity']['hint'], result['warnings'])
        self.setting(capacity_hint_warn=True)
        again = delegate(self.engine, self.chat_id, 'Goal 2', tasks('b', files=['1.txt', '2.txt', '3.txt']))
        self.assertIn(again['capacity']['hint'], again['warnings'])
        self.setting(capacity_hint_files=5)
        quiet = delegate(self.engine, self.chat_id, 'Goal 3', tasks('c', files=['1.txt', '2.txt', '3.txt']))
        self.assertIsNone(quiet['capacity']['hint'])


if __name__ == '__main__':
    unittest.main()
