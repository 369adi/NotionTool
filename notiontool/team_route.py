"""Automatic DIRECT/TEAM routing for operator delegations.

decide() scores one delegation (goal plus task list) and says whether it needs the full role team.
The setting team_route_mode decides what happens with that decision:

- 'log_only' (default): nothing changes; the decision is only saved on the run next to its allocation
  and returned in the delegate response.
- 'enforce': a TEAM decision expands the plan with team_graph.expand() (design -> critique -> builds ->
  integrate, or analysis parts with one critique each plus a synthesis).
- 'off': no decision is computed at all.

A request can override the automatic decision with route='direct' | 'team' ('auto' is the default).
decide() never raises: on any error it returns a DIRECT decision with a route_error reason.
"""
import copy

MODES = ('log_only', 'enforce', 'off')
DEFAULT_MODE = 'log_only'
ROUTES = ('auto', 'direct', 'team')
# The one shared list of stage roles for team graphs (builder A copies them onto engine tasks, builder B sets them).
STAGE_ROLES = ('design', 'design_critique', 'build', 'integrate', 'part', 'critique', 'synthesis')
# Task-level adjustments on top of the request-level base score: (name, delta).
ADJUSTMENTS = (('deps_chain', 0.15), ('multi_task', 0.10), ('risk_medium', 0.10), ('no_acceptance', 0.10),
               ('concrete_single', -0.15))
ANALYSIS_CLASSES = ('analysis', 'audit')
CAPACITY_FILES = 3
CAPACITY_CHARS = 1500
CAPACITY_ACCEPTANCE_LINES = 4
DEP_KEYS = ('depends_on', 'deps', 'after')


def setting_mode(settings):
    """The team_route_mode setting; unknown values fall back to the safe log-only default."""
    value = str((settings if isinstance(settings, dict) else {}).get('team_route_mode') or DEFAULT_MODE)
    value = value.strip().lower()
    return value if value in MODES else DEFAULT_MODE


def threshold_of(settings):
    from .difficulty import TEAM_THRESHOLD
    try:
        value = float((settings if isinstance(settings, dict) else {}).get('team_route_threshold', TEAM_THRESHOLD))
    except (TypeError, ValueError):
        value = TEAM_THRESHOLD
    return round(min(1.0, max(0.0, value)), 2)


def normal_route(value):
    """'' / None -> 'auto'; raises ValueError for anything but auto, direct or team."""
    text = str(value or 'auto').strip().lower()
    if text not in ROUTES:
        raise ValueError("route must be 'auto', 'direct' or 'team'")
    return text


def _int_setting(settings, key, default):
    try:
        value = int((settings if isinstance(settings, dict) else {}).get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _files(tasks):
    out = []
    for task in tasks:
        for name in task.get('files') or []:
            if isinstance(name, str) and name not in out:
                out.append(name)
    return out


def _text(goal, tasks):
    parts = [str(goal or '')]
    for task in tasks:
        parts += [str(task.get('title') or ''), str(task.get('prompt') or '')]
    return '\n'.join(p for p in parts if p.strip())


def _risk(task):
    return str(task.get('risk') or '').strip().lower()


def kind_of(goal, tasks):
    """'analysis' for read-only/audit work without files, or when every task_class is analysis/audit; else 'build'."""
    from .operator_main import AUDIT_TEXT
    tasks = [t for t in (tasks or []) if isinstance(t, dict)]
    classes = {str(t.get('task_class') or '').strip().lower() for t in tasks}
    if tasks and classes and classes <= set(ANALYSIS_CLASSES):
        return 'analysis'
    if not _files(tasks) and AUDIT_TEXT.search(_text(goal, tasks)):
        return 'analysis'
    return 'build'


def _adjustments(tasks):
    hits = set()
    if any(t.get(key) for t in tasks for key in DEP_KEYS):
        hits.add('deps_chain')
    if len(tasks) > 1:
        hits.add('multi_task')
    if any(_risk(t) == 'medium' for t in tasks):
        hits.add('risk_medium')
    if any(not str(t.get('acceptance') or '').strip() for t in tasks):
        hits.add('no_acceptance')
    if len(tasks) == 1 and len(_files(tasks)) <= 1 and str(tasks[0].get('acceptance') or '').strip():
        hits.add('concrete_single')
    return [(name, delta) for name, delta in ADJUSTMENTS if name in hits]


def decide(goal, tasks, quality='', settings=None, route='auto', explicit=False):
    """DIRECT/TEAM decision for one delegation; never raises.

    Returns dict(mode, score, reasons, enforced, setting, threshold, kind, route, tasks_in, graph_size).
    route is the request override ('auto' | 'direct' | 'team'); explicit says the caller chose the quality.
    """
    setting = setting_mode(settings)
    try:
        threshold = threshold_of(settings)
    except Exception:  # difficulty import failed: still never raise
        threshold = 0.55
    count = len(tasks) if isinstance(tasks, list) else 0
    try:
        return _decide(goal, tasks, str(quality or ''), settings, route, bool(explicit), setting, threshold)
    except Exception as exc:
        text = str(exc).strip() or type(exc).__name__
        return dict(mode='direct', score=0.0, reasons=['route_error: ' + text[:200]], enforced=False,
                    setting=setting, threshold=threshold, kind='build', route='auto', tasks_in=count,
                    graph_size=None)


def _decide(goal, tasks, quality, settings, route, explicit, setting, threshold):
    if not isinstance(tasks, list) or not tasks or not all(isinstance(t, dict) for t in tasks):
        raise TypeError('tasks must be a non-empty list of task objects')
    goal = '' if goal is None else str(goal)
    reasons = []
    try:
        forced = normal_route(route)
    except ValueError:
        forced = 'auto'
        reasons.append('unknown route %r ignored' % (route,))
    from . import effort_policy
    from .operator_roles import is_ui
    base = effort_policy.route_for_request(_text(goal, tasks), {'files': _files(tasks)})
    score = float(base.get('score') or 0.0)
    reasons = ['base: ' + str(r) for r in base.get('reasons') or []] + reasons
    for name, delta in _adjustments(tasks):
        score += delta
        reasons.append('%s %+.2f' % (name, delta))
    score = round(max(0.0, min(1.0, score)), 2)
    mode = 'team' if score >= threshold else 'direct'
    reasons.append('score %.2f vs threshold %.2f -> %s' % (score, threshold, mode))
    if any(_risk(t) == 'high' for t in tasks):
        mode = 'team'
        reasons.append('hard:risk_high')
    probe = [dict(title=t.get('title') or '', prompt='', role='', files=list(t.get('files') or [])) for t in tasks]
    if is_ui(goal, probe):
        mode = 'team'
        reasons.append('hard:ui')
    if forced == 'direct':
        mode = 'direct'
        reasons.append('forced:direct')
    elif forced == 'team':
        mode = 'team'
        reasons.append('forced:team')
    elif explicit and quality == 'fast' and mode == 'team':
        # The caller asked for fast on purpose: never silently raise it to a team run.
        mode = 'direct'
        reasons.append('explicit_fast')
    enforced = mode == 'team' and (setting == 'enforce' or forced == 'team')
    return dict(mode=mode, score=score, reasons=reasons, enforced=enforced, setting=setting,
                threshold=threshold, kind=kind_of(goal, tasks), route=forced, tasks_in=len(tasks),
                graph_size=None)


def has_team(tasks):
    return any(isinstance(t, dict) and stage_role_of(t) for t in tasks or [])


def stage_role_of(task):
    team = task.get('team') if isinstance(task, dict) else None
    role = task.get('stage_role') if isinstance(task, dict) else ''
    if not role and isinstance(team, dict):
        role = team.get('stage_role')
    return role if role in STAGE_ROLES else ''


def _fallback(decision, reason):
    decision.update(mode='direct', enforced=False)
    decision['reasons'].append(reason)
    return decision


def apply_route(decision, tasks, quality, quality_reason):
    """Apply a decision to the (alias-converted) task list.

    Enforced: tasks = team_graph.expand(tasks, decision, kind); on an error or when the graph would exceed
    the cap (no task carries a team key) the decision falls back to direct and the tasks stay as they are.
    Quality is raised to at least standard so the review step runs. Not enforced: tasks and quality stay
    unchanged; a TEAM decision only records the graph size it would have (None when that fails).
    Returns (tasks, quality, quality_reason).
    """
    kind = decision.get('kind') or 'build'
    if decision.get('enforced'):
        try:
            from .team_graph import expand
            out = expand(tasks, decision, kind)
        except Exception as exc:
            _fallback(decision, 'graph_error: ' + (str(exc).strip() or type(exc).__name__)[:200])
            return tasks, quality, quality_reason
        if not isinstance(out, list) or not has_team(out):
            _fallback(decision, 'graph_over_cap')
            return tasks, quality, quality_reason
        decision['graph_size'] = len(out)
        if quality not in ('standard', 'high'):
            quality_reason = (quality_reason + '; ' if quality_reason else '') + \
                'raised from %s to standard for the team route' % (quality or 'none')
            quality = 'standard'
        return out, quality, quality_reason
    if decision.get('mode') == 'team':
        try:
            from .team_graph import expand
            out = expand(copy.deepcopy(tasks), dict(decision, enforced=True), kind)
            decision['graph_size'] = len(out) if isinstance(out, list) and has_team(out) else None
            if decision['graph_size'] is None:
                decision['reasons'].append('graph_over_cap (log only)')
        except Exception as exc:
            decision['graph_size'] = None
            decision['reasons'].append('graph_size_error: ' + (str(exc).strip() or type(exc).__name__)[:200])
    return tasks, quality, quality_reason


def apply_team_keys(spec, task):
    """Copy team.stage_role / team.read_only from a plan task onto its engine task; True when changed."""
    team = spec.get('team') if isinstance(spec, dict) else None
    if not isinstance(team, dict):
        return False
    changed = False
    role = team.get('stage_role')
    if role in STAGE_ROLES and task.get('stage_role') != role:
        task['stage_role'] = role
        changed = True
    if 'read_only' in team and task.get('read_only') != bool(team['read_only']):
        task['read_only'] = bool(team['read_only'])
        changed = True
    return changed


def match_created(specs, created):
    """Map plan task id -> created engine task explicitly (title, prompt, files, acceptance and mapped deps).

    _import_plan does not return its id map, so this replaces the old positional zip: tasks are resolved in
    dependency order and each engine task is used once, so equal titles or extra graph tasks cannot shift it.
    """
    left = list(created)
    out = {}
    pending = [s for s in specs if isinstance(s, dict)]
    while pending:
        progressed = False
        for spec in list(pending):
            deps = list(spec.get('depends_on') or [])
            if any(d not in out for d in deps):
                continue
            want = (spec.get('title'), spec.get('prompt'), list(spec.get('files') or []), spec.get('acceptance'),
                    sorted(out[d]['id'] for d in deps))
            hit = next((t for t in left if (t.get('title'), t.get('prompt'), list(t.get('files') or []),
                                            t.get('acceptance'), sorted(t.get('deps') or [])) == want), None)
            if hit is not None:
                left.remove(hit)
                out[str(spec.get('id'))] = hit
            pending.remove(spec)
            progressed = True
        if not progressed:
            break
    return out


def _big(task, files, chars, lines):
    why = []
    if len(task.get('files') or []) >= files:
        why.append('%d files' % len(task['files']))
    if len(str(task.get('prompt') or '')) > chars:
        why.append('prompt over %d chars' % chars)
    acceptance = [x for x in str(task.get('acceptance') or '').splitlines() if x.strip()]
    if len(acceptance) >= lines:
        why.append('%d acceptance lines' % len(acceptance))
    return why


def capacity_hint(free, startable, tasks, files=CAPACITY_FILES, chars=CAPACITY_CHARS,
                  acceptance_lines=CAPACITY_ACCEPTANCE_LINES):
    """A split hint when coworkers would idle while a big task runs alone; None otherwise."""
    try:
        idle = int(free) - int(startable)
    except (TypeError, ValueError):
        return None
    if idle <= 0:
        return None
    big = [str(t.get('id') or t.get('title') or '?') for t in tasks or []
           if isinstance(t, dict) and _big(t, files, chars, acceptance_lines)]
    if not big:
        return None
    head = ('Task %s looks big' % big[0]) if len(big) == 1 else ('Tasks %s look big' % ', '.join(big))
    return '%s and %d coworker(s) are idle: consider splitting it into independent parts.' % (head, idle)


def capacity_settings(settings):
    return dict(files=_int_setting(settings, 'capacity_hint_files', CAPACITY_FILES),
                chars=_int_setting(settings, 'capacity_hint_chars', CAPACITY_CHARS))
