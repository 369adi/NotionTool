import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notiontool.parallel_plan import (budget_limit, choose_coworkers, expected_wall_time, independent_parts,  # noqa: E402
                                      levels, young_coworkers)


def indep(n, files=None):
    return [dict(id='t%d' % i, files=(files(i) if files else ['f%d.py' % i]), est_s=100) for i in range(n)]


def young(n):
    return [dict(id='w%d' % i, age_h=1) for i in range(n)]


class TestParallelPlan(unittest.TestCase):
    def test_serial_chain_gets_one(self):
        tasks = [dict(id='a', est_s=100), dict(id='b', depends_on=['a'], est_s=100),
                 dict(id='c', depends_on=['b'], est_s=100)]
        count, reason = choose_coworkers(tasks, young(5), {})
        self.assertEqual(count, 1)
        self.assertIn('DAG width 1', reason)
        self.assertEqual(levels(tasks), [['a'], ['b'], ['c']])

    def test_seven_independent_parts_with_seven_free_get_seven(self):
        count, reason = choose_coworkers(indep(7), young(7), {})
        self.assertEqual(count, 7, reason)

    def test_never_more_than_independent_parts(self):
        count, _ = choose_coworkers(indep(3), young(10), {})
        self.assertEqual(count, 3)

    def test_overlapping_files_give_fewer(self):
        tasks = indep(7, files=lambda i: ['shared.py'] if i < 3 else ['f%d.py' % i])
        self.assertEqual(independent_parts(tasks), (7, 5))
        count, reason = choose_coworkers(tasks, young(7), {})
        self.assertTrue(count < 7 and count <= 5, reason)
        # the 3 shared-file tasks are serial, so fewer coworkers are already as fast as 5
        self.assertEqual(expected_wall_time(tasks, count, {}), expected_wall_time(tasks, 5, {}))

    def test_file_paths_compare_case_and_slash_insensitive(self):
        tasks = [dict(id='a', files=['notiontool\\Engine.py']), dict(id='b', files=['notiontool/engine.py'])]
        self.assertEqual(independent_parts(tasks), (2, 1))
        self.assertEqual(choose_coworkers(tasks, young(4), {})[0], 1)

    def test_old_coworkers_excluded(self):
        free = [dict(age_h=1), dict(age_h=2), dict(age_h=30), dict(age_h=40), dict(age_h=23), dict(age_h=5)]
        count, reason = choose_coworkers(indep(7), free, {})
        self.assertEqual(count, 3)
        self.assertIn('3 coworker(s) >= 23 h excluded', reason)

    def test_age_from_created_at_and_unknown_age_counts_as_young(self):
        # name kept from the review request; unknown age now counts as too old by default
        now = 2_000_000_000
        free = [dict(created_at=now - 3600), dict(created_at=now - 24 * 3600), dict(id='no-age'), 30]
        self.assertEqual(len(young_coworkers(free, dict(now=now))), 1)
        self.assertEqual(len(young_coworkers(free, dict(now=now, unknown_age_young=True))), 2)
        count, reason = choose_coworkers(indep(2), free, dict(now=now))
        self.assertEqual(count, 1)
        self.assertIn('1 unknown age excluded', reason)
        self.assertIn('2 coworker(s) >= 23 h excluded', reason)

    def test_no_young_coworker_gives_zero(self):
        count, reason = choose_coworkers(indep(2), [dict(age_h=25)], {})
        self.assertEqual(count, 0)
        self.assertIn('no free coworker', reason)
        count, reason = choose_coworkers(indep(2), [dict(id='x')], {})
        self.assertEqual(count, 0)
        self.assertIn('1 unknown age excluded', reason)
        self.assertEqual(choose_coworkers([], young(2), {})[0], 0)

    def test_network_budget_limit(self):
        stats = dict(req_per_coworker_min=40)  # 180 * 0.8 / 40 = 3.6 -> 3
        self.assertEqual(budget_limit(stats), 3)
        count, reason = choose_coworkers(indep(7), young(7), stats)
        self.assertEqual(count, 3)
        self.assertIn('network budget', reason)
        # per-turn rate: 60 requests per 60 s turn = 60/min -> 2 coworkers
        self.assertEqual(budget_limit(dict(req_per_turn=60, turn_s=60)), 2)
        self.assertIsNone(budget_limit(dict(req_per_coworker_min=0)))

    def test_budget_on_by_default(self):
        # measured default: 180 req/min * 0.8 / top parallel_limit 10 -> 14.4 req/min per coworker -> 10
        self.assertEqual(budget_limit({}), 10)
        self.assertEqual(budget_limit(None), 10)
        count, reason = choose_coworkers(indep(12), young(12), {})
        self.assertLessEqual(count, 10)
        self.assertIn('network budget 180 req/min 10', reason)
        # the live NetworkBudget.parallel_limit from the engine hook also caps the count
        count, reason = choose_coworkers(indep(7), young(7), dict(parallel_limit=4))
        self.assertEqual(count, 4)
        self.assertIn('parallel limit 4', reason)

    def test_expected_time_decreases_then_flattens(self):
        tasks = indep(7)
        times = [expected_wall_time(tasks, n, {}) for n in range(1, 11)]
        self.assertTrue(all(a >= b for a, b in zip(times, times[1:])))
        self.assertTrue(times[0] > times[1] > times[3] > times[6])
        self.assertTrue(times[6] == times[7] == times[9])  # beyond 7 parts nothing changes
        self.assertEqual(times[0], 7 * 106)  # est_s 100 + overhead 6
        self.assertEqual(times[6], 106)

    def test_low_gain_stops_adding(self):
        # one long task and six short ones: past 2 coworkers the long task dominates
        tasks = [dict(id='long', est_s=1000)] + [dict(id='s%d' % i, est_s=10) for i in range(6)]
        count, reason = choose_coworkers(tasks, young(7), {})
        self.assertEqual(count, 2, reason)
        self.assertIn('gain <', reason)

    def test_fixed_overhead_and_dependencies_in_time(self):
        tasks = [dict(id='a', est_s=100), dict(id='b', est_s=100), dict(id='c', depends_on=['a', 'b'], est_s=50)]
        stats = dict(overhead_s=0, fixed_s=30)
        self.assertEqual(expected_wall_time(tasks, 2, stats), 180)
        self.assertEqual(expected_wall_time(tasks, 1, stats), 280)
        # unknown task time uses the 163 s bench default
        self.assertEqual(expected_wall_time([dict(id='x')], 1, dict(overhead_s=0)), 163)


# ---------------------------------------------------------------- backtest (not a unit test)
# python tests/test_parallel_plan.py --backtest [--project DIR] [--db PATH] [--bench PATH] [--free N]
# Env fallbacks: NOTIONTOOL_PROJECT, NOTIONTOOL_DB, NOTIONTOOL_SPEED_BENCH.
# Read-only: speed_bench.py opens the DB with mode=ro; prints numbers only, sends no Notion requests.

def _load_bench(path):
    import importlib.util
    if not path or not os.path.isfile(path):
        raise SystemExit('speed_bench.py not found at %r: pass --bench PATH or set NOTIONTOOL_SPEED_BENCH' % path)
    spec = importlib.util.spec_from_file_location('speed_bench', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plan_tasks(a, raw, plan_roles, known_times=True):
    rows = a['rows']
    ids = {x['id'] for x in rows}
    plans = [x for x in rows if x['role'] in plan_roles and x['finished']]
    out = []
    for x in rows:
        ps = set(d for d in x['deps'] if d in ids)
        for key in ('review_of', 'fix_of'):
            if x.get(key) in ids:
                ps.add(x[key])
        if x['role'] not in plan_roles and x['created']:
            ps |= {p['id'] for p in plans if p['id'] != x['id'] and p['finished'] <= x['created'] + 1}
        ps.discard(x['id'])
        t = raw.get(x['id'], {})
        files = t.get('assigned_files') or t.get('files') or []
        # second pass: task time 0/unknown -> est_s None, so the 163 s turn_s default is used
        est = (x['busy'] or 0.0) if known_times else None
        out.append(dict(id=x['id'], depends_on=sorted(ps), est_s=est, files=[f for f in files if isinstance(f, str)]))
    return out


def _backtest(db, bench, free_n):
    import statistics as S
    if not os.path.isfile(db):
        raise SystemExit('database not found at %r: pass --db PATH, --project DIR or set NOTIONTOOL_DB' % db)
    sb = _load_bench(bench)
    objs, ev_task, ev_run, glob = sb.load(sb.connect(db))
    res = sb.analyse(objs, ev_task, ev_run, glob)
    base = dict(overhead_s=0.0, fixed_s=0.0)
    base_default = dict(fixed_s=0.0)  # unknown times: turn_s 163 + overhead 6 per task
    rows = []
    for a in res['runs']:
        if not a['wall'] or not a['work'] or a['peak'] < 1:
            continue
        tasks = _plan_tasks(a, objs['tasks'], sb.PLAN_ROLES)
        pred = expected_wall_time(tasks, a['peak'], base)
        if pred <= 0:
            continue
        tasks0 = _plan_tasks(a, objs['tasks'], sb.PLAN_ROLES, known_times=False)
        pred0 = expected_wall_time(tasks0, a['peak'], base_default)
        rec, _ = choose_coworkers(tasks, [dict(age_h=1)] * free_n, base)
        rows.append(dict(id=a['id'], peak=a['peak'], wall=a['wall'], pred=pred, pred0=pred0, main=a['main_wait'] or 0.0,
                         rec=rec, pred_rec=expected_wall_time(tasks, rec, base), tasks=len(tasks)))
    if not rows:
        raise SystemExit('no usable runs in %s' % db)
    err = lambda p, w: abs(p - w) / w * 100.0  # noqa: E731

    def line(name, preds):
        e = [err(p, r['wall']) for p, r in zip(preds, rows)]
        tw = sum(abs(p - r['wall']) for p, r in zip(preds, rows)) / sum(r['wall'] for r in rows) * 100.0
        print('%-22s mean_err=%.0f%% median_err=%.0f%% time_weighted_err=%.0f%% total_ratio_wall/pred=%.2f' % (
            name, S.fmean(e), S.median(e), tw, sum(r['wall'] for r in rows) / max(1e-9, sum(preds))))
        return e

    sum_wall, sum_pred = sum(r['wall'] for r in rows), sum(r['pred'] for r in rows)
    print('bench=%s runs=%d free_for_choice=%d' % (bench, len(rows), free_n))
    print('sum_wall_h=%.1f sum_pred_h=%.1f sum_pred_default163_h=%.1f gap_h=%.1f main_wait_h=%.1f' % (
        sum_wall / 3600, sum_pred / 3600, sum(r['pred0'] for r in rows) / 3600, (sum_wall - sum_pred) / 3600,
        sum(r['main'] for r in rows) / 3600))
    raw_e = line('known_task_times', [r['pred'] for r in rows])
    line('default_163s_times', [r['pred0'] for r in rows])
    line('known_plus_main', [r['pred'] + r['main'] for r in rows])
    # fixed_s per peak level, leave-one-out median of (wall - pred) of the other runs at that level
    lvl = lambda r: min(r['peak'], 5)  # noqa: E731
    fixed = []
    for i, r in enumerate(rows):
        others = [o['wall'] - o['pred'] for j, o in enumerate(rows) if j != i and lvl(o) == lvl(r)]
        fixed.append(max(0.0, S.median(others)) if others else 0.0)
    fix_e = line('known_plus_fixed_loo', [r['pred'] + f for r, f in zip(rows, fixed)])
    for p in sorted({lvl(r) for r in rows}):
        idx = [i for i, r in enumerate(rows) if lvl(r) == p]
        print('peak=%s runs=%d known=%.0f%% plus_fixed=%.0f%% fixed_s_median=%.0f' % (
            '5+' if p == 5 else p, len(idx), S.fmean(raw_e[i] for i in idx), S.fmean(fix_e[i] for i in idx),
            S.median(fixed[i] for i in idx)))
    print('why long runs are underestimated: the simulation only counts task busy time; the gap is time outside '
          'tasks (Main waits, queue wait after ready, review/plan hand-offs). Main wait alone is %.0f%% of the gap.' % (
              100.0 * sum(r['main'] for r in rows) / max(1e-9, sum_wall - sum_pred)))
    same = sum(r['rec'] == r['peak'] for r in rows)
    fewer = sum(r['rec'] < r['peak'] for r in rows)
    more = sum(r['rec'] > r['peak'] for r in rows)
    change = sum(r['pred_rec'] - r['pred'] for r in rows)
    print('chosen_vs_actual_peak (free=%d) same=%d fewer=%d more=%d pred_task_time_change_min=%.1f (%.1f%%)' % (
        free_n, same, fewer, more, change / 60, 100.0 * change / sum_pred))
    for r, e in sorted(zip(rows, raw_e), key=lambda z: -z[0]['wall'])[:10]:
        print('run=%s tasks=%d peak=%d wall_min=%.1f pred_min=%.1f err=%.0f%% main_min=%.1f chosen=%d' % (
            str(r['id'])[:9], r['tasks'], r['peak'], r['wall'] / 60, r['pred'] / 60, e, r['main'] / 60, r['rec']))


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--backtest', action='store_true')
    ap.add_argument('--project', default=os.environ.get('NOTIONTOOL_PROJECT')
                    or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument('--db', default=os.environ.get('NOTIONTOOL_DB'))
    ap.add_argument('--bench', default=os.environ.get('NOTIONTOOL_SPEED_BENCH'))
    ap.add_argument('--free', type=int, default=7, help='free young coworkers given to choose_coworkers')
    args, rest = ap.parse_known_args()
    if args.backtest:
        _backtest(args.db or os.path.join(args.project, 'data', 'notiontool.db'),
                  args.bench or os.path.join(args.project, 'work', 'speed_bench.py'), args.free)
    else:
        unittest.main(argv=[sys.argv[0]] + rest)
