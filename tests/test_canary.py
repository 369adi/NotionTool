"""Checking logic of the live backtest (notiontool/canary.py), with a fake harness only.

No Notion client, no network, no real coworker: the fake plays the coworkers by writing the sandbox
files itself. The live part is run by the Main (docs/BACKTEST.md).
"""
import json
import tempfile
import unittest
from pathlib import Path

from notiontool import canary
from notiontool.operator_main import OPERATOR

ROOT = Path(__file__).resolve().parent.parent


def task(title, worker, start, end, result='RESULT: DONE', state='done', **extra):
    return dict(dict(id=title, title=title, worker_id=worker, started=start, finished=end, result=result, state=state), **extra)


class FakeHarness:
    """Fake engine access: records every delegate/cancel call and plays the coworkers."""
    canary_harness = True

    def __init__(self, base, coworkers=3, finish=True, roles_ok=True, capacity=10, extra_main=False, agent=False,
                 status=None):
        self.base, self.finish, self.roles_ok = Path(base), finish, roles_ok
        self.capacity, self.extra_main, self.agent = capacity, extra_main, agent
        self.delegated, self.cancelled = [], []
        self.runs, self.task_rows, self.agents = {}, {}, []
        self.event_rows = [dict(seq=1, kind='boot')]
        self.workers = [dict(id='op', purpose=OPERATOR, account_id='a1', state='ready'),
                        dict(id='m1', purpose='main', account_id='a1', state='ready')]
        self.workers += [dict(id='w%d' % i, purpose='worker', enabled=True, account_id='a1') for i in range(1, coworkers + 1)]
        self.workers.append(dict(id='other', purpose='worker', enabled=True, account_id='a2'))
        self.statuses = list(status or [])

    # -- harness interface used by canary.run_suite --
    def project_path(self):
        return str(self.base)

    def all(self, kind):
        return {'workers': self.workers, 'agents': self.agents,
                'tasks': [t for rows in self.task_rows.values() for t in rows]}.get(kind, [])

    def events(self, after=0):
        return [e for e in self.event_rows if e['seq'] > after]

    def last_event(self):
        return self.event_rows[-1]['seq']

    def settings(self):
        return {}

    def status(self):
        return self.statuses.pop(0) if self.statuses else None

    def run(self, run_id):
        return self.runs.get(run_id, {})

    def tasks(self, run_id):
        return self.task_rows.get(run_id, [])

    def cancel(self, run_id):
        self.cancelled.append(run_id)
        self.runs[run_id]['state'] = 'cancelled'

    def delegate(self, goal, tasks, summary, request_id):
        run_id = 'r%d' % (len(self.delegated) + 1)
        self.delegated.append(dict(goal=goal, tasks=tasks, request_id=request_id))
        self.runs[run_id] = dict(id=run_id, state='executing', capacity=self.capacity, fixes=0)
        if not self.finish:
            self.task_rows[run_id] = [dict(t, state='running', worker_id='w1', started=1.0) for t in tasks]
            return dict(run_id=run_id)
        if tasks[0]['id'] == 'design':
            self.task_rows[run_id] = self._play_roles(tasks)
            self.runs[run_id].update(state='done', fixes=1 if self.roles_ok else 0)
        else:
            self.task_rows[run_id] = self._play_parallel(tasks)
            self.runs[run_id]['state'] = 'done'
        if self.extra_main:
            self.workers.append(dict(id='m2', purpose='main', account_id='a1', state='ready'))
        if self.agent:
            self.agents.append(dict(id='ag1'))
        return dict(run_id=run_id)

    def _write(self, rel, body):
        path = self.base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')

    def _play_roles(self, tasks):
        design, build, review = tasks
        calc, test = build['files']
        self._write(design['files'][0], 'plan\n')
        good = ('def clamp(x, low, high):\n    if low > high:\n        raise ValueError("low > high")\n'
                '    return max(low, min(high, x))\n')
        self._write(calc, good if self.roles_ok else 'def clamp(x, low, high):\n    return max(low, min(high, x))\n')
        self._write(test, 'import unittest\nfrom calc import clamp\n\nclass T(unittest.TestCase):\n'
                          '    def test_in(self):\n        self.assertEqual(clamp(2, 0, 3), 2)\n')
        rows = [task(design['title'], 'w1', 10, 20), task(build['title'], 'w2', 30, 40),
                task(review['title'], 'w3', 50, 60, 'REVIEW: FAIL: point 3\nRESULT: DONE')]
        if self.roles_ok:
            rows += [task('Fix: ' + build['title'], 'w2', 70, 80),
                     task(review['title'] + ' (again)', 'w3', 90, 100, 'REVIEW: PASS\nRESULT: DONE')]
        return rows

    def _play_parallel(self, tasks):
        rows = []
        for i, spec in enumerate(tasks):
            marker = spec['prompt'].split('exactly this one line: ')[1].split(' ')[0]
            self._write(spec['files'][0], marker + '\n')
            rows.append(task(spec['title'], 'w%d' % (i + 1), 100 + i, 200, files=spec['files']))
        return rows


class Clock:
    def __init__(self, step=10.0):
        self.now, self.step = 1000.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def by_name(report):
    return {c['name']: c for c in report['checks']}


class PeakTests(unittest.TestCase):
    def test_peak_overlap(self):
        self.assertEqual(canary.peak_overlap([(1, 5), (2, 6), (3, 4)]), 3)
        self.assertEqual(canary.peak_overlap([(1, 2), (2, 3), (3, 4)]), 1)  # end before start at the same time
        self.assertEqual(canary.peak_overlap([(1, 5), (2, 0), (0, 3)]), 1)  # unfinished / never started ignored
        self.assertEqual(canary.peak_overlap([]), 0)

    def test_counters_found_anywhere(self):
        self.assertEqual(canary.counters_of({'desk': {'stats': {'counters': {'pushes': 2}}}}), {'pushes': 2})
        self.assertIsNone(canary.counters_of({'ok': True}))


class RolesCheckTests(unittest.TestCase):
    def good(self):
        return [task('Canary design: x', 'w1', 1, 2), task('Canary build: x', 'w2', 3, 4),
                task('Canary review: x', 'w3', 5, 6, 'REVIEW: FAIL: point 3\nRESULT: DONE'),
                task('Fix: Canary build: x', 'w2', 7, 8), task('Canary review: x', 'w3', 9, 10, 'REVIEW: PASS\nRESULT: DONE')]

    def test_all_pass(self):
        checks = canary.check_roles(dict(state='done', fixes=1), self.good(), sandbox_test=True)
        self.assertTrue(all(c['passed'] for c in checks), checks)
        self.assertEqual(checks[2]['numbers']['fix_rounds'], 1)

    def test_same_worker_reviews_its_own_work(self):
        rows = self.good()
        for row in rows:
            row['worker_id'] = 'w2'
        c = {x['name']: x for x in canary.check_roles(dict(state='done', fixes=1), rows, True)}
        self.assertFalse(c['Reviewer is another coworker']['passed'])
        self.assertEqual(c['Reviewer is another coworker']['numbers']['both'], 1)

    def test_wrong_order_and_no_fix_round(self):
        rows = [task('Canary build: x', 'w2', 1, 2), task('Canary design: x', 'w1', 3, 4),
                task('Canary review: x', 'w3', 5, 6, 'REVIEW: PASS\nRESULT: DONE')]
        c = {x['name']: x for x in canary.check_roles(dict(state='done', fixes=0), rows, True)}
        self.assertFalse(c['Stages in order']['passed'])
        self.assertIn('Building -> Designing', c['Stages in order']['detail'])
        self.assertFalse(c['Exactly one fix round']['passed'])
        self.assertEqual(c['Exactly one fix round']['numbers']['fix_rounds'], 0)

    def test_two_fix_rounds_fail(self):
        rows = self.good() + [task('Canary review: x', 'w3', 11, 12, 'REVIEW: FAIL: again')]
        c = {x['name']: x for x in canary.check_roles(dict(state='done', fixes=2), rows, True)}
        self.assertFalse(c['Exactly one fix round']['passed'])
        self.assertFalse(c['Final result approved']['passed'])  # last review failed

    def test_not_approved_without_own_code_test(self):
        c = {x['name']: x for x in canary.check_roles(dict(state='done', fixes=1), self.good(), False)}
        self.assertFalse(c['Final result approved']['passed'])
        self.assertIn('failed', c['Final result approved']['detail'])

    def test_missing_review_stage(self):
        rows = self.good()[:2]
        c = {x['name']: x for x in canary.check_roles(dict(state='done'), rows, True)}
        self.assertFalse(c['Stages in order']['passed'])
        self.assertEqual(c['Stages in order']['numbers']['stages_seen'], 2)


class ParallelCheckTests(unittest.TestCase):
    def files(self, n):
        return {'p%d.txt' % i: dict(marker='M%d' % i, content='M%d\n' % i) for i in range(n)}

    def test_all_pass(self):
        rows = [task('t%d' % i, 'w%d' % i, 1, 10, files=['p%d.txt' % i]) for i in range(3)]
        c = {x['name']: x for x in canary.check_parallel(dict(capacity=3), rows, 3, self.files(3), 1, 1, 0, 0, 12.0)}
        self.assertTrue(all(x['passed'] for x in c.values()), c)
        self.assertEqual(c['Ran at the same time']['numbers']['peak'], 3)

    def test_serial_run_and_conflicts(self):
        rows = [task('t%d' % i, 'w1', i * 10, i * 10 + 5, files=['p0.txt']) for i in range(3)]
        files = self.files(3)
        files['p1.txt']['content'] = 'M1\nM2\n'
        files['p2.txt']['content'] = None
        c = {x['name']: x for x in canary.check_parallel(dict(capacity=10), rows, 3, files, 1, 2, 0, 1, 40.0)}
        self.assertFalse(c['Ran at the same time']['passed'])
        self.assertEqual(c['Ran at the same time']['numbers']['peak'], 1)
        self.assertEqual(c['No file conflicts']['numbers'], dict(missing=1, mixed=1, shared=2))
        self.assertFalse(c['No second Main']['passed'])
        self.assertFalse(c['No Notion agent created']['passed'])

    def test_capacity_limits_the_goal(self):
        rows = [task('t%d' % i, 'w%d' % i, 0 if i < 2 else 20, 10 if i < 2 else 30, files=['p%d.txt' % i]) for i in range(4)]
        c = {x['name']: x for x in canary.check_parallel(dict(capacity=2), rows, 4, self.files(4), 1, 1, 0, 0, 30.0)}
        self.assertTrue(c['Ran at the same time']['passed'])
        self.assertEqual(c['Ran at the same time']['numbers']['goal'], 2)

    def test_unfinished_tasks_fail(self):
        rows = [task('t0', 'w1', 0, 10, files=['p0.txt']), task('t1', 'w2', 0, 0, '', 'running', files=['p1.txt'])]
        c = {x['name']: x for x in canary.check_parallel({}, rows, 2, self.files(2), 1, 1, 0, 0, 5.0)}
        self.assertFalse(c['All tasks done']['passed'])
        self.assertEqual(c['All tasks done']['numbers']['done'], 1)


class HealthCheckTests(unittest.TestCase):
    def status(self, pushes, beats, timeouts=0, failures=0):
        return {'counters': dict(pushes=pushes, heartbeats=beats, timeouts=timeouts, failures=failures, dropped=0)}

    def test_healthy(self):
        c = canary.check_health(self.status(1, 1), self.status(3, 9), [], set(), 30)
        self.assertTrue(all(x['passed'] for x in c), c)
        self.assertEqual(c[0]['numbers'], dict(pushes=2, heartbeats=8, need=5))

    def test_no_messages_and_offline(self):
        c = canary.check_health(self.status(1, 1), self.status(1, 1, timeouts=2), [], set(), 30)
        self.assertFalse(c[0]['passed'])
        self.assertFalse(c[1]['passed'])
        self.assertEqual(c[1]['numbers']['timeouts'], 2)

    def test_false_hang_only_for_idle_workers(self):
        events = [dict(kind='turn_interrupted', worker_id='w1'), dict(kind='turn_interrupted', worker_id='w2'),
                  dict(kind='state_push', worker_id='w3')]
        c = canary.check_health(self.status(0, 0), self.status(0, 10), events, {'w1'}, 30)
        self.assertFalse(c[2]['passed'])
        self.assertEqual(c[2]['numbers'], dict(hang_reports=2, false_hangs=1))

    def test_status_missing(self):
        c = canary.check_health(None, None, [], set(), 30)
        self.assertEqual([x['passed'] for x in c], [False, False, False])
        self.assertIn('not available', c[0]['detail'])


class RunSuiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sleeps = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_suite(self, fake, suite, **kw):
        return canary.run_suite(fake, suite, sleep=self.sleeps.append, clock=Clock(), **kw)

    def sandbox_left(self):
        root = self.base / 'work' / 'canary'
        return [p.name for p in root.iterdir() if p.is_dir()] if root.exists() else []

    def test_roles_suite_passes_and_cleans_up(self):
        fake = FakeHarness(self.base)
        report = self.run_suite(fake, 'roles')
        self.assertTrue(report['passed'], report['checks'])
        self.assertEqual(len(fake.delegated), 1)
        self.assertEqual(fake.cancelled, [])
        for spec in fake.delegated[0]['tasks']:
            for f in spec['files']:
                self.assertTrue(f.startswith('work/canary/roles-'), f)
        self.assertIn('leave point 3 open', fake.delegated[0]['tasks'][1]['prompt'])
        self.assertEqual(self.sandbox_left(), [])
        self.assertTrue(report['cleaned'])
        md = Path(report['report_path'])
        self.assertEqual(md.parent, self.base / 'work' / 'canary')
        self.assertTrue(md.name.startswith('report-'))
        body = md.read_bytes()
        self.assertNotIn(b'\r\n', body)
        self.assertIn(b'Exactly one fix round | passed', body)
        json.dumps(report)  # JSON-ready

    def test_roles_suite_catches_missing_fix_round(self):
        report = self.run_suite(FakeHarness(self.base, roles_ok=False), 'roles')
        c = by_name(report)
        self.assertFalse(report['passed'])
        self.assertFalse(c['Exactly one fix round']['passed'])
        self.assertFalse(c['Final result approved']['passed'])  # hidden test finds the missing ValueError
        self.assertIn('FAILED', Path(report['report_path']).read_text(encoding='utf-8'))

    def test_parallel_suite_uses_active_account_coworkers(self):
        fake = FakeHarness(self.base, coworkers=4)
        report = self.run_suite(fake, 'parallel')
        c = by_name(report)
        self.assertTrue(report['passed'], report['checks'])
        self.assertEqual(len(fake.delegated[0]['tasks']), 4)  # the other account's coworker is not counted
        self.assertEqual(c['Ran at the same time']['numbers']['peak'], 4)
        files = [f for t in fake.delegated[0]['tasks'] for f in t['files']]
        self.assertEqual(len(files), len(set(files)))
        self.assertEqual(self.sandbox_left(), [])

    def test_parallel_caps_at_ten(self):
        fake = FakeHarness(self.base, coworkers=13)
        self.run_suite(fake, 'parallel')
        self.assertEqual(len(fake.delegated[0]['tasks']), canary.MAX_PARALLEL)

    def test_parallel_reports_second_main_and_agent(self):
        c = by_name(self.run_suite(FakeHarness(self.base, extra_main=True, agent=True), 'parallel'))
        self.assertFalse(c['No second Main']['passed'])
        self.assertEqual(c['No second Main']['numbers'], dict(mains_before=2, mains_after=3))
        self.assertFalse(c['No Notion agent created']['passed'])

    def test_timeout_cancels_once_and_never_resends(self):
        fake = FakeHarness(self.base, finish=False)
        report = self.run_suite(fake, 'parallel', timeout=60)
        c = by_name(report)
        self.assertFalse(c['Finished in time']['passed'])
        self.assertIn('nothing was resent', c['Finished in time']['detail'])
        self.assertEqual(len(fake.delegated), 1)
        self.assertEqual(len(fake.cancelled), 1)
        self.assertEqual(self.sandbox_left(), [])
        self.assertTrue(self.sleeps and all(s == canary.POLL_S for s in self.sleeps))

    def test_health_suite(self):
        status = [{'counters': dict(pushes=1, heartbeats=0)}, {'counters': dict(pushes=2, heartbeats=9)}]
        fake = FakeHarness(self.base, status=status)
        report = self.run_suite(fake, 'health', health_window=30)
        self.assertTrue(report['passed'], report['checks'])
        self.assertEqual(fake.delegated, [])  # health sends nothing
        self.assertEqual(self.sleeps, [30])

    def test_unknown_suite(self):
        with self.assertRaises(ValueError):
            canary.run_suite(FakeHarness(self.base), 'nope')

    def test_errors_become_a_failed_check(self):
        fake = FakeHarness(self.base)
        fake.delegate = lambda *a: (_ for _ in ()).throw(ValueError('Register this chat as operator Main first'))
        report = self.run_suite(fake, 'roles')
        self.assertFalse(report['passed'])
        self.assertIn('operator Main', report['checks'][0]['detail'])
        self.assertEqual(self.sandbox_left(), [])

    def test_guard_refuses_paths_outside_sandbox(self):
        root = self.base / 'work' / 'canary'
        with self.assertRaises(ValueError):
            canary._guard(self.base / 'notiontool' / 'engine.py', root)
        with self.assertRaises(ValueError):
            canary._guard(root / '..' / 'x.py', root)
        self.assertEqual(canary._guard(root / 'a' / 'b.txt', root), (root / 'a' / 'b.txt').resolve())


class CliTests(unittest.TestCase):
    def test_needs_port_and_key(self):
        import contextlib, io, os
        old = {k: os.environ.pop(k, None) for k in ('NOTIONTOOL_PORT', 'NOTIONTOOL_API_KEY')}
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(canary.main(['--suite', 'health']), 2)
            self.assertIn('--port', err.getvalue())
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v

    def test_lf_line_endings(self):
        for rel in ('notiontool/canary.py', 'tests/test_canary.py', 'docs/BACKTEST.md'):
            path = ROOT / rel
            if path.exists():
                self.assertNotIn(b'\r\n', path.read_bytes(), rel)


if __name__ == '__main__':
    unittest.main()
