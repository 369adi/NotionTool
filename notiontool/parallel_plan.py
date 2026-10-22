"""Pure coworker-count planner: how many coworkers should a task list get (e.g. 2 vs 7)?

Stdlib only, no I/O, no engine imports. Not wired into the engine yet.

    choose_coworkers(tasks, free_coworkers, stats) -> (count, reason)
    expected_wall_time(tasks, count, stats) -> seconds

tasks: planner-contract dicts: id, optional depends_on (list of ids), files (list of paths),
       est_s (expected busy seconds; missing/None -> stats['turn_s']).
free_coworkers: dicts with age_h, or created_at (epoch s, needs stats['now']), or plain numbers
       (age in hours). A coworker whose age is unknown counts as too old unless
       stats['unknown_age_young'] is True.
stats: optional overrides of DEFAULTS. Defaults come from work/audit/speed-bench-20261020.md.

count = min(free young coworkers (I32: < 23 h), widest DAG level, file-disjoint parts of that level,
            network budget 180 req/min / requests per busy coworker per minute, live parallel_limit),
then reduced while the extra coworkers buy less than min_gain each in expected wall time.

Age limit: I32, read from coworker_age_guard.HARD_MAX_AGE_HOURS (the one shared 23 h constant).

Network budget is on by default, but the default rate is CONFIGURED, not measured: 14.4 req per
busy coworker per minute = the 180 req/min hard limit times the 0.8 headroom spread over the highest
configured network_budget.parallel_limit (10), at which the bench saw 0 rate-limit events. The reason
text says so. Pass a measured rate as stats['req_per_coworker_min'] together with
stats['req_rate_source'] = 'measured'. The engine passes the live _parallel_cap value as
stats['parallel_limit'].

Plan-time estimates: when a task has no est_s, its duration is stats['turn_s_by_role'][role]
(median busy time of earlier done runs), else turn_s. fixed_s_by_width maps an expected width
(1, 2, ...) to the median serial time outside tasks of earlier done runs with that peak width.
stats_from_history(runs) builds both from earlier runs only.
"""

MEASURED_PARALLEL_TOP = 10  # bench: top network_budget.parallel_limit, 0 rate-limit events


def _hard_max_age_hours():
    """The one I32 limit (coworker_age_guard.HARD_MAX_AGE_HOURS); 23 only if that module cannot load."""
    try:
        from .coworker_age_guard import HARD_MAX_AGE_HOURS
        return float(HARD_MAX_AGE_HOURS)
    except Exception:
        return 23.0


DEFAULTS = {
    'max_age_h': _hard_max_age_hours(),  # I32: Welcome chats at or above this age are not used
    'unknown_age_young': False,  # coworkers without a known age are excluded
    'min_gain': 0.05,            # bench knee: below 5 % gain per extra coworker stop adding
    'req_limit_per_min': 180.0,  # network_budget.HARD_LIMIT_PER_MINUTE
    'req_headroom': 0.8,         # keep 20 % of the budget for the Main, polling and retries
    'req_per_coworker_min': 180.0 * 0.8 / MEASURED_PARALLEL_TOP,  # 14.4, see module docstring
    'req_per_turn': None,        # alternative: measured requests per turn (divided by turn_s)
    'parallel_limit': None,      # live NetworkBudget.parallel_limit(configured), if known
    'turn_s': 163.0,             # bench: median executor turn sent -> done
    'overhead_s': 6.0,           # bench: median assign -> send per task
    'fixed_s': 0.0,              # serial time outside tasks (Main wait, review stage) added once
    'max_coworkers': 40,
    'now': None,
}


def _stats(stats):
    out = dict(DEFAULTS)
    given = {k: v for k, v in (stats or {}).items() if v is not None}
    if 'req_per_turn' in given and 'req_per_coworker_min' not in given:
        out['req_per_coworker_min'] = None  # an explicit per-turn rate replaces the default rate
    out.update(given)
    return out


def _ids(tasks):
    return [str(t.get('id', i)) for i, t in enumerate(tasks)]


def _deps(tasks):
    ids = _ids(tasks)
    known = set(ids)
    return {i: set(str(d) for d in (t.get('depends_on') or []) if str(d) in known and str(d) != i)
            for i, t in zip(ids, tasks)}


def levels(tasks):
    """Waves of the dependency DAG (each wave can start once all earlier waves are done)."""
    remaining, done, out = _deps(tasks), set(), []
    while remaining:
        wave = sorted(i for i, d in remaining.items() if d <= done)
        if not wave:  # cycle: run the rest one by one
            out.extend([[i] for i in sorted(remaining)])
            break
        out.append(wave)
        done |= set(wave)
        remaining = {i: d for i, d in remaining.items() if i not in done}
    return out


def _files(task):
    return set(str(f).replace('\\', '/').lower() for f in (task.get('files') or []) if f)


def file_groups(tasks_in_wave):
    """Groups of tasks that touch overlapping files (transitively); each group runs serially."""
    groups = []
    for t in tasks_in_wave:
        fs = _files(t)
        hit = [g for g in groups if fs and g['files'] & fs]
        merged = {'files': set(fs), 'tasks': [t]}
        for g in hit:
            merged['files'] |= g['files']
            merged['tasks'] += g['tasks']
            groups.remove(g)
        groups.append(merged)
    return groups


def independent_parts(tasks):
    """(dag_width, file_width): widest wave, and widest wave after file-lock grouping."""
    by_id = dict(zip(_ids(tasks), tasks))
    dag_w = file_w = 0
    for wave in levels(tasks):
        dag_w = max(dag_w, len(wave))
        file_w = max(file_w, len(file_groups([by_id[i] for i in wave])))
    return dag_w, file_w


def _age_h(worker, now):
    if isinstance(worker, bool):
        return None
    if isinstance(worker, (int, float)):
        return float(worker)
    if not isinstance(worker, dict):
        return None
    if isinstance(worker.get('age_h'), (int, float)) and not isinstance(worker.get('age_h'), bool):
        return float(worker['age_h'])
    created = worker.get('created_at')
    if isinstance(created, (int, float)) and created > 1e9 and now:
        return max(0.0, (now - created) / 3600.0)
    return None


def age_split(free_coworkers, stats=None):
    """(young, too_old, unknown) lists."""
    st = _stats(stats)
    young, old, unknown = [], [], []
    for w in free_coworkers or []:
        age = _age_h(w, st['now'])
        if age is None:
            (young if st['unknown_age_young'] else unknown).append(w)
        elif age < st['max_age_h']:
            young.append(w)
        else:
            old.append(w)
    return young, old, unknown


def young_coworkers(free_coworkers, stats=None):
    return age_split(free_coworkers, stats)[0]


def budget_limit(stats=None):
    """Coworkers the 180 req/min budget can carry (None only when the rate is explicitly unknown)."""
    st = _stats(stats)
    rate = st['req_per_coworker_min']
    if not rate and st['req_per_turn'] and st['turn_s']:
        rate = float(st['req_per_turn']) / (float(st['turn_s']) / 60.0)
    if not rate or rate <= 0:
        return None
    # small epsilon: 144 / 14.4 must give 10, not 9 (float floor division)
    return max(1, int(st['req_limit_per_min'] * st['req_headroom'] / rate + 1e-9))


def expected_wall_time(tasks, count, stats=None):
    """List-scheduling simulation: count coworkers, DAG order, overlapping files never at once.

    Each task takes est_s (default turn_s) plus overhead_s; fixed_s is added once.
    """
    st = _stats(stats)
    if not tasks:
        return float(st['fixed_s'])
    count = max(1, int(count))
    ids = _ids(tasks)
    by_id = dict(zip(ids, tasks))
    deps = _deps(tasks)
    order = {i: n for n, i in enumerate(ids)}
    dur = {}
    for i, t in by_id.items():
        est = t.get('est_s')
        est = float(est) if isinstance(est, (int, float)) and not isinstance(est, bool) and est >= 0 else float(st['turn_s'])
        dur[i] = est + float(st['overhead_s'])
    files = {i: _files(t) for i, t in by_id.items()}
    finish, running, clock = {}, [], 0.0  # running: (end, id)
    waiting = set(ids)
    while waiting or running:
        started = True
        while started and len(running) < count:
            started = False
            busy = set().union(*[files[r] for _, r in running]) if running else set()
            ready = sorted((i for i in waiting if deps[i] <= set(finish) and all(finish[d] <= clock for d in deps[i])
                            and not (files[i] & busy)), key=lambda i: (-dur[i], order[i]))
            if ready:
                i = ready[0]
                waiting.discard(i)
                running.append((clock + dur[i], i))
                started = True
        if not running:  # unreachable deps (cycle): release the first waiting task
            i = min(waiting, key=lambda x: order[x])
            deps[i] = set()
            continue
        running.sort()
        end, i = running.pop(0)
        clock = end
        finish[i] = end
        while running and running[0][0] <= clock:
            e, j = running.pop(0)
            finish[j] = e
    return clock + float(st['fixed_s'])


# Bench B 2026-10-20 (6 read-only parts, real Edge transport): send -> collected 45-67 s per coworker task even
# for ~5 s of work; solo the Main did all 6 in 31 s. So a coworker costs a fixed ~45 s before it saves time.
DELEGATION = {
    'cw_fixed_s': 60.0,     # measured (heavy6c/6d): send -> first output 45-80 s, independent of the work size
    'dispatch_s': 4.0,      # measured: 6 sends spread over ~20-40 s (pre-send reads share the network budget)
    'collect_s': 15.0,      # measured: remote done -> result collected 6-29 s (median ~15 s)
    'review_s': 10.0,       # Main reads and checks the results
    'cw_slowdown': 1.15,    # a coworker re-reads context; assumed until the heavy bench measures it
    'min_save_s': 30.0,     # delegate only when it saves at least this much ...
    'min_save_ratio': 0.2,  # ... and at least 20 % of the solo time
}


def delegation_decision(parts, free_young=10, stats=None):
    """Should the Main call coworkers for these parts, and how many?

    parts: dicts with est_s (the Main's solo seconds for that part), optional id, depends_on, files.
    Returns dict(delegate, count, solo_s, parallel_s, saved_s, reason). Solo = sum of the parts.
    Parallel = dispatch + fixed coworker cost + list-scheduled work (slowed down) + collect + review.
    """
    st = dict(DELEGATION)
    st.update({k: v for k, v in (stats or {}).items() if k in DELEGATION and v is not None})
    parts = [p for p in (parts or []) if isinstance(p, dict)]
    est = [float(p.get('est_s') or 0) for p in parts]
    solo = sum(est)
    if len(parts) < 2 or free_young < 2:
        return dict(delegate=False, count=0, solo_s=round(solo, 1), parallel_s=None, saved_s=0.0,
                    reason='fewer than 2 independent parts or free coworkers: do it alone')
    sched = [dict(p, est_s=e * st['cw_slowdown'] + st['cw_fixed_s']) for p, e in zip(parts, est)]
    dag_w, file_w = independent_parts(sched)
    cap = max(1, min(int(free_young), dag_w, file_w))
    best = None
    for n in range(1, cap + 1):
        wall = expected_wall_time(sched, n, {'overhead_s': 0.0, 'fixed_s': 0.0})
        wall += st['dispatch_s'] * min(n, len(parts)) + st['collect_s'] + st['review_s']
        if best is None or wall < best[1] - 1.0:
            best = (n, wall)
    n, wall = best
    saved = solo - wall
    ok = saved >= st['min_save_s'] and saved >= st['min_save_ratio'] * solo
    if ok:
        reason = '%d coworkers: %.0f s instead of %.0f s alone (saves %.0f s)' % (n, wall, solo, saved)
    else:
        reason = 'alone is faster or equal: %.0f s alone vs %.0f s with %d coworkers (fixed cost ~%.0f s each)' % (
            solo, wall, n, st['cw_fixed_s'])
    return dict(delegate=ok, count=n if ok else 0, solo_s=round(solo, 1), parallel_s=round(wall, 1),
                saved_s=round(saved, 1), reason=reason)


def choose_coworkers(tasks, free_coworkers, stats=None):
    """Return (count, reason). count is 0 only when no young coworker is free or there is no task."""
    st = _stats(stats)
    tasks = [t for t in (tasks or []) if isinstance(t, dict)]
    young, old, unknown = age_split(free_coworkers, st)
    excluded = ''
    if old:
        excluded += '; %d coworker(s) >= %g h excluded' % (len(old), st['max_age_h'])
    if unknown:
        excluded += '; %d unknown age excluded' % len(unknown)
    if not tasks:
        return 0, 'no tasks'
    if not young:
        return 0, 'no free coworker younger than %g h%s' % (st['max_age_h'], excluded)
    dag_w, file_w = independent_parts(tasks)
    limits = [('free young coworkers', len(young)), ('DAG width', dag_w),
              ('file-disjoint parts', file_w), ('team limit', int(st['max_coworkers']))]
    budget = budget_limit(st)
    if budget is not None:
        limits.append(('network budget %g req/min' % st['req_limit_per_min'], budget))
    if isinstance(st['parallel_limit'], int) and not isinstance(st['parallel_limit'], bool):
        limits.append(('parallel limit', max(1, st['parallel_limit'])))
    cap = max(1, min(v for _, v in limits))
    binding = [name for name, v in limits if v == cap]
    times = {n: expected_wall_time(tasks, n, st) for n in range(1, cap + 1)}
    count = cap
    for n in range(1, cap + 1):
        # smallest n where every larger team buys less than min_gain per extra coworker
        if all(times[n] / times[m] - 1.0 < st['min_gain'] * (m - n) for m in range(n + 1, cap + 1) if times[m] > 0):
            count = n
            break
    parts = ', '.join('%s %d' % (name, v) for name, v in limits)
    if count < cap:
        reason = ('%d of max %d: more coworkers gain < %.0f %% each (expected %.0f s vs %.0f s with %d); limits: %s'
                  % (count, cap, st['min_gain'] * 100, times[count], times[cap], cap, parts))
    else:
        reason = '%d, bound by %s; limits: %s; expected %.0f s' % (count, ' and '.join(binding), parts, times[count])
    return count, reason + excluded
