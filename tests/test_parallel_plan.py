import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notiontool.parallel_plan import (DELEGATION, PLAN_PART_CAP, PLAN_PART_KIND, budget_limit,  # noqa: E402
                                      choose_coworkers, delegation_decision, est_factor, est_factor_gain,
                                      expected_wall_time, finish_plan_part, finish_task_part, independent_parts,
                                      levels, plan_part_records, plan_report, prune_plan_parts, ratio_report,
                                      record_plan, without_factor, young_coworkers)


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

    def test_age_from_created_at_and_unknown_age_excluded(self):
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


def done_parts(role, pairs):
    out = []
    for n, (est, real) in enumerate(pairs):
        rec = plan_part_records('p%s%d' % (role, n), [dict(id='a', est_s=est)], now=n, role=role)[0]
        out.append(finish_plan_part(rec, real))
    return out


class TestEstVersusReal(unittest.TestCase):
    def test_records_per_part(self):
        parts = [dict(id='a', est_s=60, role='coder'), dict(est_s=None), 'junk', dict(id='c', est_s=-5)]
        recs = plan_part_records('plan1', parts, now=7.0, role='fallback')
        self.assertEqual([r['id'] for r in recs], ['plan1:a', 'plan1:1', 'plan1:c'])
        self.assertEqual([r['role'] for r in recs], ['coder', 'fallback', 'fallback'])
        self.assertEqual([r['est_s'] for r in recs], [60.0, None, None])
        self.assertTrue(all(r['plan_id'] == 'plan1' and r['real_s'] is None and r['created'] == 7.0 for r in recs))
        self.assertEqual(plan_part_records('x', None), [])
        self.assertEqual(PLAN_PART_KIND, 'plan_part')

    def test_finish_stores_real_seconds_without_mutating(self):
        rec = plan_part_records('p', [dict(id='a', est_s=10)])[0]
        self.assertEqual(finish_plan_part(rec, 25)['real_s'], 25.0)
        self.assertIsNone(rec['real_s'])
        for bad in (None, -1, True, 'x'):
            self.assertIsNone(finish_plan_part(rec, bad)['real_s'])

    def test_report_median_and_p90_per_role(self):
        recs = done_parts('coder', [(10, 10 * r) for r in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)])
        recs += done_parts('review', [(100, 50), (100, 150)])
        recs += done_parts('review', [(0, 5), (None, 5), (10, None)])  # unusable: ignored
        rep = ratio_report(recs)
        self.assertEqual(rep['coder'], dict(n=10, median=5.5, p90=9.0))
        self.assertEqual(rep['review'], dict(n=2, median=1.0, p90=1.5))
        self.assertEqual(rep['all']['n'], 12)
        self.assertEqual(ratio_report([]), {})

    def test_factor_needs_enough_data_and_is_median(self):
        recs = done_parts('coder', [(10, 20)] * 19)
        self.assertEqual(est_factor(recs), 1.0)
        self.assertEqual(est_factor(recs + done_parts('coder', [(10, 20)])), 2.0)
        self.assertEqual(est_factor(recs, min_n=5), 2.0)

    def test_gain_shrinks_only_when_estimates_are_biased(self):
        biased = done_parts('coder', [(10, 20 + i % 3) for i in range(30)])  # real is about twice est_s
        gain = est_factor_gain(biased)
        self.assertEqual(gain['n'], 30)
        self.assertTrue(gain['shrinks'])
        self.assertLess(gain['error_factor'], gain['error_plain'])
        exact = done_parts('coder', [(10, 10)] * 30)
        self.assertFalse(est_factor_gain(exact)['shrinks'])
        self.assertEqual(est_factor_gain([]), dict(n=0, error_plain=None, error_factor=None, shrinks=False))

    def test_prune_keeps_the_newest(self):
        recs = done_parts('coder', [(1, 1)] * 5)
        self.assertEqual(prune_plan_parts(recs, cap=5), [])
        self.assertEqual(prune_plan_parts(recs, cap=3), ['pcoder0', 'pcoder1'] and [recs[0]['id'], recs[1]['id']])
        self.assertEqual(len(prune_plan_parts(recs, cap=0)), 5)
        self.assertGreaterEqual(PLAN_PART_CAP, 100)

    def test_delegation_factor_is_off_by_default_and_opt_in(self):
        parts = [dict(id='a', est_s=120), dict(id='b', est_s=120)]
        base = delegation_decision(parts, 10)
        self.assertEqual(delegation_decision(parts, 10, dict(est_factor=1.0)), base)
        self.assertEqual(delegation_decision(parts, 10, dict(est_factor=None)), base)
        doubled = delegation_decision(parts, 10, dict(est_factor=2.0))
        self.assertEqual(doubled['solo_s'], 2 * base['solo_s'])


class FakeStore:
    """Just the objects-table API that parallel_plan uses: get, all (insertion order), put, batch."""

    def __init__(self):
        self.rows = {}

    def get(self, kind, ident, default=None):
        return self.rows.get((kind, str(ident)), default)

    def all(self, kind):
        return [dict(v) for (k, _), v in self.rows.items() if k == kind]

    def put(self, kind, doc):
        self.rows[(kind, str(doc['id']))] = dict(doc)
        return doc

    def batch(self, records, deletes=()):
        for kind, doc in records:
            self.put(kind, doc)
        for kind, ident in deletes:
            self.rows.pop((kind, str(ident)), None)


class TestPlanPartStore(unittest.TestCase):
    def test_cap_is_2000(self):
        self.assertEqual(PLAN_PART_CAP, 2000)

    def test_record_plan_saves_est_and_role_without_real_seconds(self):
        store = FakeStore()
        ids = record_plan(store, 'pl1', [dict(id='a', est_s=60, role='coder'), dict(id='b', est_s=30)], now=5.0)
        self.assertEqual(ids, ['pl1:a', 'pl1:b'])
        rec = store.get(PLAN_PART_KIND, 'pl1:a')
        self.assertEqual((rec['plan_id'], rec['part_id'], rec['role'], rec['est_s'], rec['real_s']),
                         ('pl1', 'a', 'coder', 60.0, None))
        self.assertEqual(store.get(PLAN_PART_KIND, 'pl1:b')['role'], 'unknown')
        self.assertEqual(record_plan(store, 'pl2', None), [])

    def test_finish_task_part_stores_real_seconds_once(self):
        store = FakeStore()
        record_plan(store, 'pl1', [dict(id='a', est_s=60)], now=1.0)
        task = dict(plan_part='pl1:a', state='done', started=100.0, finished=190.0, specialty='reviewer')
        done = finish_task_part(store, task)
        self.assertEqual(done['real_s'], 90.0)
        self.assertEqual(store.get(PLAN_PART_KIND, 'pl1:a')['real_s'], 90.0)
        self.assertEqual(store.get(PLAN_PART_KIND, 'pl1:a')['role'], 'reviewer')  # unknown role takes the specialty
        self.assertIsNone(finish_task_part(store, dict(task, finished=999.0)))  # a second call changes nothing
        self.assertEqual(store.get(PLAN_PART_KIND, 'pl1:a')['real_s'], 90.0)

    def test_finish_task_part_ignores_failed_unlinked_and_broken_tasks(self):
        store = FakeStore()
        record_plan(store, 'pl1', [dict(id='a', est_s=60)], now=1.0)
        base = dict(plan_part='pl1:a', state='done', started=10.0, finished=20.0)
        for bad in (dict(base, state='blocked'), dict(base, plan_part=''), dict(base, plan_part='nope:x'),
                    dict(base, started=None, created=None), dict(base, finished=5.0)):
            self.assertIsNone(finish_task_part(store, bad))
        self.assertIsNone(finish_task_part(store, None))  # never raises
        self.assertIsNone(store.get(PLAN_PART_KIND, 'pl1:a')['real_s'])

    def test_report_on_fixed_records(self):
        store = FakeStore()
        for rec in done_parts('coder', [(10, 10 * r) for r in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)]):
            store.put(PLAN_PART_KIND, rec)
        for rec in done_parts('review', [(100, 50), (100, 150)]):
            store.put(PLAN_PART_KIND, rec)
        store.put(PLAN_PART_KIND, plan_part_records('open', [dict(id='a', est_s=5)], now=1.0, role='coder')[0])  # unfinished
        rep = plan_report(store)
        self.assertEqual(rep['records'], 13)
        self.assertEqual(rep['measured'], 12)
        self.assertEqual(rep['roles']['coder'], dict(n=10, median=5.5, p90=9.0))
        self.assertEqual(rep['roles']['review'], dict(n=2, median=1.0, p90=1.5))
        self.assertEqual(rep['applied_factor'], 1.0)
        self.assertNotIn('gain', rep)
        self.assertIn('gain', plan_report(store, gain=True))
        self.assertEqual(plan_report(FakeStore())['roles'], {})

    def test_record_plan_prunes_the_oldest_beyond_the_cap(self):
        store = FakeStore()
        for n in range(PLAN_PART_CAP - 1):  # 1999 old records, created = 0 .. 1998
            store.put(PLAN_PART_KIND, plan_part_records('old%d' % n, [dict(id='a', est_s=1)], now=float(n))[0])
        ids = record_plan(store, 'new', [dict(id='a', est_s=1), dict(id='b', est_s=1), dict(id='c', est_s=1)],
                          now=5000.0)
        left = [r['id'] for r in store.all(PLAN_PART_KIND)]
        self.assertEqual(len(left), PLAN_PART_CAP)
        self.assertNotIn('old0:a', left)  # the two oldest went
        self.assertNotIn('old1:a', left)
        self.assertIn('old2:a', left)
        self.assertTrue(all(i in left for i in ids))  # the new records stay

    def test_record_plan_cap_argument(self):
        store = FakeStore()
        for n in range(5):
            record_plan(store, 'p%d' % n, [dict(id='a', est_s=1)], now=float(n), cap=3)
        self.assertEqual([r['id'] for r in store.all(PLAN_PART_KIND)], ['p2:a', 'p3:a', 'p4:a'])

    def test_factor_is_measured_never_applied(self):
        parts = [dict(id='a', est_s=120), dict(id='b', est_s=120)]
        base = delegation_decision(parts, 10)
        self.assertEqual(DELEGATION['est_factor'], 1.0)
        self.assertEqual(without_factor(dict(est_factor=3.0, cw_fixed_s=50)), dict(cw_fixed_s=50))
        self.assertIsNone(without_factor(None))
        self.assertEqual(delegation_decision(parts, 10, without_factor(dict(est_factor=3.0))), base)


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
