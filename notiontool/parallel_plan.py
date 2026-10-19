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

Age limit: I32 is 23 h and this module uses 23 h. team_rotation.DEFAULT_MAX_AGE_HOURS (line 31) is
still 22; aligning it is a follow-up task (that file is outside this task's file list).

Network budget is on by default: req_per_coworker_min defaults to 14.4, i.e. the 180 req/min hard
limit times the 0.8 headroom spread over the highest measured network_budget.parallel_limit (10) at
which the bench saw 0 rate-limit events. The engine hook (engine.py:450-452 _parallel_cap) should
pass the live value as stats['parallel_limit'] and, once measured, stats['req_per_coworker_min'].
"""

MEASURED_PARALLEL_TOP = 10  # bench: top network_budget.parallel_limit, 0 rate-limit events

DEFAULTS = {
    'max_age_h': 23.0,           # I32: Welcome chats at or above this age are not used
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
