"""TEAM task graphs for operator delegations (plan_route, builder B).

expand(tasks, route, kind) turns the operator's flat task list into a role graph:

- build:    design -> design critique -> builds -> (per-build review, queued at run time by
            operator_roles.step with the design in its deps and review scope) -> integrator
- analysis: parts in parallel -> one critique per part that depends only on its own part
            (streaming: it starts as soon as that part is done) -> lead synthesis

Every generated task carries team = {'stage_role', 'read_only'}. Builder A copies these two
values to the top-level task keys stage_role / read_only after the import; everything downstream
(operator_roles, engine) reads only the top-level keys. The input list is never mutated.
When the graph would exceed MAX_TASKS, or the input is unusable, plain copies without any team key
are returned, so the caller falls back to DIRECT (reason graph_over_cap).
"""
import re

MAX_TASKS = 40
MAX_ID = 48
ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,48}$')
STAGE_ROLES = ('design', 'design_critique', 'build', 'integrate', 'part', 'critique', 'synthesis')
KINDS = ('build', 'analysis')
ALIASES = ('deps', 'after')
MAX_PROMPT = 20000  # protocol.validate_plan limit


def _safe_id(value, used, fallback='team'):
    """An id matching [A-Za-z0-9_-]{1,48} that is not in used (used is updated)."""
    base = re.sub(r'[^A-Za-z0-9_-]+', '-', str(value or '')).strip('-') or fallback
    base = base[:MAX_ID]
    ident, n = base, 2
    while ident in used:
        suffix = '-%d' % n
        ident = base[:MAX_ID - len(suffix)] + suffix
        n += 1
    used.add(ident)
    return ident


def _deps_of(task):
    """The user's dependencies; deps/after are tolerated if Builder A has not converted them yet."""
    deps = task.get('depends_on')
    if not deps:
        deps = next((task.get(a) for a in ALIASES if task.get(a)), None) or []
    return [str(d) for d in deps] if isinstance(deps, list) else []


def _plain(tasks):
    return [dict(t) for t in tasks if isinstance(t, dict)]


def _clip(text):
    text = str(text or '')
    return text if len(text) <= MAX_PROMPT - 200 else text[:MAX_PROMPT - 260].rstrip() + '\n[... list shortened]'


def _parts_list(items):
    return '\n'.join('- %s: %s' % (t['id'], t.get('title', '')) for t in items)


def _goal(route, tasks):
    goal = str((route or {}).get('goal') or '').strip() if isinstance(route, dict) else ''
    return goal or '; '.join(str(t.get('title') or '') for t in tasks)[:500]


def _team(role, read_only):
    return {'stage_role': role, 'read_only': bool(read_only)}


def graph_size(n, kind):
    """Number of plan tasks expand() creates for n input tasks (reviews are queued later at run time)."""
    return 2 * n + 1 if kind == 'analysis' else n + 3


def _base(tasks):
    """Copies of the user tasks with sanitized unique ids and remapped depends_on; None if unusable."""
    used, mapping, out = set(), {}, []
    for i, t in enumerate(tasks):
        old = str(t.get('id') or '')
        if old in mapping:
            return None  # duplicate user id: validate_plan reports it on the direct path
        new = old if ID_RE.match(old) and old not in used else _safe_id(old or 'task-%d' % (i + 1), used)
        used.add(new)
        mapping[old] = new
        out.append((t, new))
    copies = []
    for t, new in out:
        copy = {k: v for k, v in t.items() if k not in ALIASES and k != 'team'}
        copy['id'] = new
        copy['depends_on'] = [mapping.get(d, d) for d in _deps_of(t)]
        copies.append(copy)
    return copies, used


def expand(tasks, route, kind):
    """Return a new plan task list for the TEAM graph of the given kind ('build' | 'analysis').

    Each returned task is a planner-contract dict (id, title, prompt, acceptance, depends_on, files, ...)
    plus team = {'stage_role', 'read_only'}. Over MAX_TASKS or on bad input: copies without team keys.
    """
    if not isinstance(tasks, list) or not tasks or not all(isinstance(t, dict) for t in tasks):
        return _plain(tasks) if isinstance(tasks, list) else []
    kind = kind if kind in KINDS else 'build'
    if graph_size(len(tasks), kind) > MAX_TASKS:
        return _plain(tasks)
    based = _base(tasks)
    if not based:
        return _plain(tasks)
    copies, used = based
    goal = _goal(route, tasks)
    if kind == 'analysis':
        return _analysis(copies, used, goal)
    return _build(copies, used, goal)


def _build(builds, used, goal):
    from .operator_roles import design_prompt, is_ui
    ui = is_ui(goal, builds)
    design_id = _safe_id('team-design', used)
    critique_id = _safe_id('team-design-critique', used)
    integrate_id = _safe_id('team-integrate', used)
    design = dict(id=design_id, title='Design: visual spec' if ui else 'Design: approach',
                  prompt=_clip(design_prompt(goal, builds, ui) + '\nUser goal: ' + goal),
                  acceptance='A concrete, followable design for every listed part; no file edited.',
                  depends_on=[], files=[], role='designer' if ui else 'architect',
                  team=_team('design', True))
    critique = dict(id=critique_id, title='Critique: design',
                    prompt=_clip('You are read-only: do not edit any file. Critically review the design in your context '
                                 '(task %s) against the user goal: %s\nName every gap, risk and contradiction, then give '
                                 'the corrected design points the builders must follow. Parts:\n%s'
                                 % (design_id, goal, _parts_list(builds))),
                    acceptance='Each finding names the design point and a concrete correction.',
                    depends_on=[design_id], files=[], role='reviewer', team=_team('design_critique', True))
    out = [design, critique]
    for b in builds:
        b['depends_on'] = list(dict.fromkeys(list(b['depends_on']) + [critique_id]))
        b['team'] = _team('build', bool(b.get('read_only')))
        out.append(b)
    integrate = dict(id=integrate_id, title='Integrate: check the combined result',
                     prompt=_clip('You are read-only: do not edit any file. All parts below were built and reviewed; '
                                  'their results are in your context. Check that together they meet the user goal: %s\n'
                                  'Run the tests the parts name, report every gap with file and fix, and state what you '
                                  'actually checked. Parts:\n%s' % (goal, _parts_list(builds))),
                     acceptance='The combined result is checked against the goal with the actual tests named.',
                     depends_on=[b['id'] for b in builds], files=[], role='integrator',
                     team=_team('integrate', True))
    out.append(integrate)
    return out


def _analysis(parts, used, goal):
    out, critiques = [], []
    for p in parts:
        p['team'] = _team('part', True)
        out.append(p)
    for p in parts:
        cid = _safe_id('crit-' + p['id'], used)
        critiques.append(dict(
            id=cid, title=('Critique: ' + str(p.get('title') or p['id']))[:200],
            prompt=_clip('You are read-only: do not edit any file. Critically check the analysis part %s (%s) in your '
                         'context: wrong or unproven claims, missing evidence (file:line, query), gaps against its '
                         'acceptance (%s). Give each finding with a concrete correction.'
                         % (p['id'], p.get('title', ''), p.get('acceptance', ''))),
            acceptance='Every finding of part %s has evidence and a concrete correction.' % p['id'],
            depends_on=[p['id']], files=[], role='reviewer', team=_team('critique', True)))
    out.extend(critiques)
    synthesis_id = _safe_id('team-synthesis', used)
    out.append(dict(
        id=synthesis_id, title='Synthesis: lead answer',
        prompt=_clip('You are read-only: do not edit any file. Combine the analysis parts and their critiques in your '
                     'context into one answer to the user goal: %s\nApply every critique correction, keep the evidence, '
                     'and say clearly what stays unproven. Parts:\n%s' % (goal, _parts_list(parts))),
        acceptance='One answer that covers every part and applies every critique correction.',
        depends_on=[p['id'] for p in parts] + [c['id'] for c in critiques], files=[], role='integrator',
        team=_team('synthesis', True)))
    return out
