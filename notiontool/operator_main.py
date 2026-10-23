"""Operator Main: the user's own Notion AI chat plans and hands tasks straight to the Coworkers.

The harness never writes into the operator's thread. Its worker gets purpose 'operator'
(never Main, never Coworker, never re-enabled by the pool rules), and operator runs have no
planner or integrator turn: the operator reads the reports itself
(GET /api/run?id=<run>, GET /api/report?id=<task>) and integrates them in its own chat.
A run's quality ('fast' | 'standard' | 'high', operator_roles.choose_quality) adds Coworker stages:
standard and high review every build on a different Coworker and fix it (max 2 rounds); high starts
with one read-only design/approach task that every build depends on.
"""
import json
import os
import re
import time

from .allocation import parallel_width
from .team_size import team_size
from .protocol import validate_plan
from .store import uid

OPERATOR = 'operator'
OPERATOR_TITLE = 'Operator Main'
MAX_DELEGATED_TASKS = 40
BUSY = ('busy', 'recovering')
RESULT_LINE = 'End your answer with a final line RESULT: DONE, or RESULT: NOT DONE: <reason> if you could not fully do the task.'
FULL_RESULT = 'Full result: '
READ_FULL_HINT = 'Read a Full result file only when the review fails, tests fail or more detail is explicitly needed.'


def result_done(result):
    """True when a task answer has a line that is exactly RESULT: DONE (case-insensitive, whitespace ignored)."""
    texts = [str(result or '')]
    try:
        data = json.loads(texts[0])
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        texts += [v for v in data.values() if isinstance(v, str)]
    # The last RESULT line decides: 'RESULT: DONE' followed by 'RESULT: NOT DONE: ...' is not done.
    marks = [line.strip().casefold() for text in texts for line in text.splitlines()
             if line.strip().casefold().startswith('result:')]
    if any(m.startswith('result: not done') for m in marks):
        return False
    return 'result: done' in marks


# Read-only audits once copied stale numbers from the brief instead of counting again.
AUDIT_TEXT = re.compile(r'\b(?:read[- ]only|audit\w*)\b', re.I)
AUDIT_LINE = ('Read-only audit: recount every number fresh from the files and data right now; never copy numbers '
              'from earlier reports, memory or this prompt. Name the command or file behind each number.')


def _with_result_line(prompt):
    if not isinstance(prompt, str) or not prompt.strip():
        return prompt  # validate_plan reports the missing prompt
    text = prompt.rstrip()
    if AUDIT_TEXT.search(text) and AUDIT_LINE not in text:
        text += '\n\n' + AUDIT_LINE
    return text + '\n\n' + RESULT_LINE


KNOWN_TASK_KEYS = frozenset(('id', 'title', 'prompt', 'acceptance', 'depends_on', 'deps', 'after', 'files', 'model',
                             'role', 'task_class', 'summary', 'quality', 'use_main', 'effort', 'risk',
                             'route', 'team'))
# Notion reasoning effort names a task may set (Rule I38); notion.choose_task_effort clamps them per model.
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')


def task_effort(task):
    """The valid per-task effort set by the Main, lower case ('' when missing or not one of EFFORTS)."""
    value = (task or {}).get('effort') if isinstance(task, dict) else None
    value = value.strip().lower() if isinstance(value, str) else ''
    return value if value in EFFORTS else ''


def _warnings(store, chat, tasks):
    """Non-blocking hints for the operator about what the Coworkers will not be able to do."""
    worker = store.get('workers', chat.get('main_worker'), {}) if chat.get('main_worker') else {}
    account = store.get('accounts', worker.get('account_id'), {}) if worker.get('account_id') else {}
    warnings = []
    with_files = [str(t.get('id', '')) for t in tasks if t.get('files')]
    if with_files and not account.get('bridge_connected'):
        warnings.append('Account is not bridge_connected: task(s) with files (%s) stay blocked until NotionTool PC tools are connected'
                        % ', '.join(with_files))
    if not store.settings().get('advertise_project_tools', False):
        warnings.append('Setting advertise_project_tools is off: Coworkers get no project tools')
    for t in tasks:
        unknown = sorted(str(k) for k in t if k not in KNOWN_TASK_KEYS)
        if unknown:
            warnings.append('Task %s: unknown key(s) %s were ignored' % (t.get('id', '?'), ', '.join(unknown)))
        if t.get('effort') not in (None, '') and not task_effort(t):
            warnings.append('Task %s: effort %r is not one of %s and was ignored (automatic effort is used)'
                            % (t.get('id', '?'), t.get('effort'), ', '.join(EFFORTS)))
    return warnings


def operator_chat(store, account_id=''):
    """The active operator chat (optionally of one account), else None."""
    for chat in store.all('chats'):
        worker = store.get('workers', chat['main_worker'], {}) if chat.get('main_worker') else {}
        if (chat.get('operator') and chat.get('state') != 'released' and worker.get('purpose') == OPERATOR
                and (not account_id or worker.get('account_id') == account_id)):
            return chat
    return None


def delegating_worker(store, chat):
    """The Notion chat of a Main or operator chat that may delegate, else None (rule: every Main may delegate)."""
    worker = store.get('workers', chat.get('main_worker') or '', {}) if chat.get('main_worker') else {}
    worker = worker or {}
    if worker.get('purpose') == OPERATOR and chat.get('operator'):
        return worker
    if worker.get('purpose') == 'main':
        return worker
    return None


def delegating_chat(store):
    """Chat used without chat_id: an operator chat of a ready account, else any Main of a ready account."""
    ready = {a.get('id') for a in store.all('accounts') if a.get('state') == 'ready'}
    live = [c for c in store.all('chats') if c.get('state') != 'released']
    for want in (OPERATOR, 'main'):
        for chat in live:
            worker = delegating_worker(store, chat)
            if worker and worker.get('purpose') == want and worker.get('account_id') in ready:
                return chat
    return operator_chat(store)


def register_operator(store, worker_id, project_path='', now=None):
    """Make worker_id the operator Main of its account and return its chat (idempotent)."""
    stamp = time.time() if now is None else now
    with store.lock:
        worker = store.get('workers', worker_id) if worker_id else None
        if not worker:
            raise ValueError('Notion chat not found')
        if worker.get('purpose') == 'main':
            raise ValueError('This Notion chat is a Main; release its chat first')
        if worker.get('purpose') == 'external':
            raise ValueError('This Notion chat was released to its user')
        if worker.get('state') in BUSY or worker.get('lease'):
            raise ValueError('This Notion chat still holds a harness task; reconcile it first')
        other = next((w for w in store.all('workers') if w.get('purpose') == OPERATOR
                      and w.get('account_id') == worker.get('account_id') and w['id'] != worker_id), None)
        if other:
            raise ValueError('This account already has an operator Main: ' + str(other.get('label') or other['id']))
        worker.update(purpose=OPERATOR, enabled=False, state='ready', lease='',
                      operator_since=worker.get('operator_since') or stamp)
        store.put('workers', worker)
        chat = next((c for c in store.all('chats') if c.get('operator') and c.get('main_worker') == worker_id), None)
        created = chat is None
        if created:
            chat = dict(id=uid('c'), title=OPERATOR_TITLE, main_worker=worker_id, folder_id='',
                        project_path=project_path, created=stamp, state='active', operator=True)
        elif project_path:
            chat['project_path'] = project_path
        store.put('chats', chat)
        store.event('operator_registered', message='Operator Main registered: ' + str(worker.get('label', '')),
                    chat_id=chat['id'])
    return dict(ok=True, chat_id=chat['id'], worker_id=worker_id, created=created)


SUMMARY_LIMIT = 200
RUN_FINISHED = ('done', 'failed', 'cancelled', 'completed')


def clean_summary(value, limit=SUMMARY_LIMIT):
    """An optional plain-language summary: one line, at most limit characters ('' if missing or not text)."""
    if not isinstance(value, str):
        return ''
    text = re.sub(r'\s+', ' ', value).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '\u2026'


def first_sentence(value, limit=SUMMARY_LIMIT):
    """The first sentence of free text, on one line ('' if empty)."""
    text = clean_summary(value, limit=100000)
    match = re.search(r'(?<=[.!?])\s', text)
    return clean_summary(text[:match.start()] if match else text, limit)


def result_summary(result):
    """First sentence of the summary inside a task result (JSON {summary} or a 'Summary:' line), else ''."""
    data = result if isinstance(result, dict) else None
    text = '' if data is not None else str(result or '').strip()
    if data is None and text.startswith('{'):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if isinstance(data, dict):
        summary = data.get('summary') if isinstance(data.get('summary'), str) else ''
        from .run_context import valid_result_file
        path = valid_result_file(data.get('result_file'))
        if path:
            short = clean_summary(summary)
            return (short + ' ' if short else '') + FULL_RESULT + path
        return first_sentence(summary) if summary else ''
    match = re.search(r'^\s*(?:summary|zusammenfassung)\s*:\s*(.+)$', text, re.I | re.M)
    return first_sentence(match.group(1)) if match else ''


def plain_task_summary(task):
    """One plain sentence for a task: its own summary, else the first sentence of its result summary, else the title.

    A result with a real result file always gives result_summary (summary plus 'Full result: <path>').
    """
    task = task if isinstance(task, dict) else {}
    from_result = result_summary(task.get('result'))
    if FULL_RESULT in from_result:
        return from_result
    return clean_summary(task.get('summary')) or from_result or clean_summary(task.get('title'))


def plain_run_summary(run):
    """One plain sentence for a request: its summary, else the first sentence of its goal."""
    run = run if isinstance(run, dict) else {}
    return clean_summary(run.get('goal_summary')) or first_sentence(run.get('goal') or run.get('title')) or 'Request'


def run_outcome(run):
    """One plain line about how a finished request ended ('' while it is still open)."""
    run = run if isinstance(run, dict) else {}
    state = run.get('state')
    if state in ('done', 'completed'):
        return first_sentence(run.get('summary')) or 'Finished.'
    if state == 'cancelled':
        return 'Stopped before the end.'
    if state == 'failed':
        reason = first_sentence(run.get('error'), 140)
        return 'Did not finish' + (': ' + reason if reason else '.')
    return ''


def _summary(engine, run, duplicate=False, warnings=None):
    from .operator_roles import run_stage, stage_of
    own = engine.tasks(run['id'])
    tasks = [dict(id=t['id'], title=t.get('title', ''), summary=plain_task_summary(t), state=t.get('state', ''),
                  worker_id=t.get('worker_id', ''), stage=stage_of(t))
             for t in own]
    note = {'note': READ_FULL_HINT} if any(FULL_RESULT in str(t['summary'] or '') for t in tasks) else {}
    return dict(ok=True, run_id=run['id'], state=run.get('state', ''), chat_id=run['chat_id'], tasks=tasks, **note,
                quality=run.get('quality', 'fast'), stage=run.get('stage') or run_stage(own),
                duplicate=duplicate, warnings=list(warnings or []),
                # team_route: the DIRECT/TEAM decision and the split hint, stored on the run (None for older runs).
                route=run.get('route'), capacity=run.get('capacity_hint'))


def _request_route(route, tasks):
    """The request's DIRECT/TEAM override and the warnings found while reading it: (route, warnings).

    The top-level route argument wins and a bad value raises ValueError. Otherwise the first valid task
    'route' field is used; a task-level value that is not auto, direct or team only adds a warning and
    counts as auto, so one bad field cannot fail the whole delegation.
    """
    from .team_route import normal_route
    if str(route or '').strip():
        return normal_route(route), []
    chosen, notes = '', []
    for task in tasks:
        value = task.get('route')
        if not value:
            continue
        try:
            value = normal_route(value)
        except ValueError:
            notes.append('Task %s: route %r is not auto, direct or team and was ignored (auto is used)'
                         % (task.get('id', '?'), value))
            continue
        chosen = chosen or value
    return chosen or 'auto', notes


def design_skip_reason(settings, plan_tasks, ui):
    """Why the quality-high design task is skipped, or '' to keep it.

    Setting skip_design_when_specified (default True): skipped only for non-UI work where every task already
    names its files and has an acceptance text. UI work and the setting off keep the design task.
    """
    if not (settings or {}).get('skip_design_when_specified', True) or ui:
        return ''
    for t in plan_tasks:
        if not [f for f in (t.get('files') or []) if str(f).strip()] or not str(t.get('acceptance') or '').strip():
            return ''
    return 'every task has files and acceptance and the work is not UI'


def delegate(engine, chat_id, goal, tasks, request_id='', now=None, summary='', quality='', route='', plan_id=''):
    """Queue the operator's own plan as executor tasks for the Coworkers; no Main turn is ever sent.

    tasks use the planner contract: id, title, prompt, acceptance, optional depends_on, files,
    model, role (specialty) and task_class, plus an optional plain-language 'summary' sentence per task
    and for the whole request (shown in the Live view). use_main is always forced off.
    quality ('fast' | 'standard' | 'high', also accepted as a task field) is chosen automatically when empty.
    route ('auto' | 'direct' | 'team', also accepted as a task field) overrides the automatic DIRECT/TEAM
    decision of team_route.decide. With the default setting team_route_mode='log_only' the decision is only
    saved on the run (run['route']) and returned; 'enforce' or route='team' expands the plan into a team graph.
    A bad route argument raises ValueError; a bad task-level route only adds a warning and auto is used.
    A known request_id returns the stored run without deciding or expanding again.
    Every new run also saves run['capacity_hint'] (returned as 'capacity'), whatever team_route_mode is.
    """
    store = engine.store
    goal = str(goal or '').strip()
    if not isinstance(tasks, list) or not tasks or not all(isinstance(t, dict) for t in tasks):
        raise ValueError('tasks must be a non-empty list of task objects')
    from .operator_roles import choose_quality, design_prompt, is_ui
    from .team_route import (apply_route, apply_team_keys, capacity_hint, capacity_settings, decide,
                             match_created, setting_mode, stage_role_of)
    quality = quality or next((t['quality'] for t in tasks if t.get('quality')), '')
    route, notes = _request_route(route, tasks)  # notes: warnings for bad task-level route values
    tasks = [{k: v for k, v in t.items() if k not in ('quality', 'route')} for t in tasks]
    for item in tasks:
        # 'deps' / 'after' are common names for depends_on; silently dropping them ran dependent tasks in parallel.
        # Converted before the route decision so decide, team_graph.expand and validate_plan see the real deps.
        for alias in ('deps', 'after'):
            if alias in item and not item.get('depends_on'):
                item['depends_on'] = list(item.pop(alias) or [])
    quality, quality_reason = choose_quality(goal, tasks, quality)
    settings = store.settings()
    user_tasks = tasks
    decision = None
    request_id = str(request_id or '')[:100]
    # Duplicates first: a known request_id returns the stored run and its stored route below, so it is never
    # decided or expanded again (the authoritative check stays inside the lock).
    repeat = bool(request_id) and any(r.get('request_id') == request_id for r in store.all('runs'))
    if not repeat and (setting_mode(settings) != 'off' or route != 'auto'):
        decision = decide(goal, tasks, quality, settings, route=route, explicit=quality_reason == 'requested')
        tasks, quality, quality_reason = apply_route(decision, tasks, quality, quality_reason, goal=goal)
    plan = {'tasks': [dict(t, use_main=False, **({'prompt': _with_result_line(t['prompt'])} if 'prompt' in t else {}))
                      for t in tasks]}
    validate_plan(plan, max_tasks=MAX_DELEGATED_TASKS)
    with engine.lock:
        chat = store.get('chats', chat_id) if chat_id else delegating_chat(store)
        if not chat or chat.get('state') == 'released' or not delegating_worker(store, chat):
            raise ValueError('Only a Main chat can delegate; register this chat as operator Main first')
        if not goal:
            raise ValueError('Describe the goal of the delegated work')
        request_id = str(request_id or '')[:100]
        if request_id:
            known = next((r for r in store.all('runs') if r.get('request_id') == request_id), None)
            if known:
                if known['chat_id'] != chat['id']:
                    raise ValueError('request_id already belongs to another chat')
                return _summary(engine, known, duplicate=True, warnings=_warnings(store, chat, tasks) + notes)
        operator = store.get('workers', chat.get('main_worker') or '') if chat.get('main_worker') else None
        if not operator or operator.get('purpose') not in (OPERATOR, 'main'):
            raise ValueError('The operator chat lost its Notion thread; register it again')
        account = store.get('accounts', operator.get('account_id') or '', {}) or {}
        if account.get('state') == 'ai_disabled':
            raise ValueError('Notion AI is disabled for the account of this Main (%s); delegate from a Main '
                             'of a ready account' % (account.get('email') or operator.get('account_id')))
        coworkers = [w for w in store.all('workers') if w.get('purpose') == 'worker' and w.get('enabled')
                     and w.get('account_id') == operator.get('account_id')]
        if not coworkers:
            raise ValueError('No enabled Coworker in this account')
        stamp = time.time() if now is None else now
        # I32: chats 23 h or older count for no capacity; with none young the tasks wait queued for the rotation.
        from .coworker_age_guard import young
        coworkers = young(store, coworkers, stamp) or coworkers[:1]
        capacity = max(1, min(int(store.settings().get('max_workers') or 1), team_size(store.settings()), len(coworkers)))
        run = dict(id=uid('r'), chat_id=chat['id'], main_worker=operator['id'], goal=goal, state='executing',
                   stage='', context='', project_path=chat.get('project_path', ''), folder_id=chat.get('folder_id', ''),
                   created=stamp, updated=stamp, capacity=capacity, account_id=operator.get('account_id', ''),
                   fixes=0, summary='', error='', request_id=request_id, operator=True, harness=True,
                   model_routing=False, goal_summary=clean_summary(summary),
                   allocation=dict(mode='operator', budget=capacity, advisers=0,
                                   reason='Operator Main delegates directly to Coworkers'),
                   quality=quality, quality_reason=quality_reason)
        startable = parallel_width(plan['tasks'])
        if decision is not None:
            run['route'] = decision  # next to allocation: mode, score, reasons, enforced, setting, kind, graph_size
        # The split hint does not depend on routing: every new run gets it, also with team_route_mode='off'.
        free = len([w for w in coworkers if w.get('state') not in BUSY])
        run['capacity_hint'] = dict(free=free, startable=startable,
                                    hint=capacity_hint(free, startable, user_tasks, **capacity_settings(settings)))
        run['worker_budget'] = max(1, min(capacity, startable))
        engine._import_plan(run, plan)  # checks every planned file path before any task is queued
        created = [t for t in engine.tasks(run['id']) if t.get('role') == 'executor']
        # plan id -> engine task: explicit first, then by position; by_position = the ids only the fallback matched
        matched, by_position = match_created(plan['tasks'], created)
        unmatched = [str(s['id']) for s in plan['tasks'] if str(s['id']) not in matched]
        for spec in plan['tasks']:
            task = matched.get(str(spec['id']))
            if task is None:
                continue
            changed = False
            text = clean_summary(spec.get('summary'))
            if text:
                task['summary'] = text
                changed = True
            effort = task_effort(spec)
            if effort:
                # Rule I38: the Main's effort; notion.choose_task_effort sends it (clamped) instead of the automatic one.
                task['requested_effort'] = effort
                changed = True
            # Team graphs: stage_role / read_only become top-level keys inside this lock, before any dispatch.
            # A task matched only by position may be the wrong one: it gets summary and effort, never team keys.
            if str(spec['id']) not in by_position and apply_team_keys(spec, task):
                changed = True
            if plan_id:
                task['plan_part'] = '%s:%s' % (plan_id, spec['id'])  # rank 10: finish_task_part stores the real seconds
                changed = True
            if changed:
                store.put('tasks', task)
        team_design = any(stage_role_of(t) == 'design' for t in plan['tasks'])
        design_skipped = ''
        if quality == 'high' and not team_design:
            ui = is_ui(goal, plan['tasks'])
            design_skipped = design_skip_reason(settings, plan['tasks'], ui)
            if not design_skipped:
                design = engine.add_task(run, 'architect', design_prompt(goal, plan['tasks'], ui),
                                         'Design: visual spec' if ui else 'Design: approach',
                                         specialty='designer' if ui else 'architect')
                design.update(stage_role='design', read_only=True)  # no files: holds no lock
                store.put('tasks', design)
                for task in created:
                    task['deps'] = list(task['deps']) + [design['id']]  # its result is injected via the deps context
                    store.put('tasks', task)
        run['stage'] = 'Designing' if (quality == 'high' and not design_skipped) or team_design else 'Building'
        store.put('runs', run)
        store.message(chat['id'], 'user', goal, run['id'])
        store.event('run_created', run['id'], message='Operator delegated %d task(s)' % len(plan['tasks']))
        if design_skipped:
            store.event('design_skipped', run['id'], message=design_skipped)
        warnings = _warnings(store, chat, plan['tasks']) + notes
        if unmatched:
            warnings.append('Plan task(s) %s matched no created Coworker task: their summary, effort and team keys '
                            'were not applied' % ', '.join(unmatched))
        if by_position:
            warnings.append('Plan task(s) %s were matched to a created Coworker task only by position: their summary '
                            'and effort were applied, their team keys (stage_role, read_only) were not'
                            % ', '.join(by_position))
        hint = (run.get('capacity_hint') or {}).get('hint')
        if hint and settings.get('capacity_hint_warn', False):
            warnings.append(hint)  # off by default: log-only keeps today's warnings; the hint is in 'capacity'
        return _summary(engine, run, warnings=warnings)
