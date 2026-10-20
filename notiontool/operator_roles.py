"""Role stages for operator runs: design (high), build, review and fix (standard and high).

The operator still integrates the reports itself; every stage task runs on a Coworker, never in the
operator's own chat. Designers and reviewers are read-only: they hold no files, so they never take
a file lock and never block another Coworker.
"""
import json
import re
import time

QUALITIES = ('fast', 'standard', 'high')
MAX_FIX_ROUNDS = 2
LIVE = ('queued', 'retry', 'running', 'uncertain', 'cancelling')
UI_WORDS = re.compile(r'(?i)\b(ui|ux|design|designer|layout|css|styles?|styling|visual|theme|sidebar|'
                      r'buttons?|colou?rs?|spacing|frontend|front-end|typography|icons?)\b')
UI_FILES = re.compile(r'(?i)\.(css|scss|html?|jsx|tsx|vue|svg)$|(^|/)web/')
CODE_FILES = re.compile(r'(?i)\.(py|js|ts|jsx|tsx|ps1|sh|go|rs|java|cs|cpp|c|h|rb|php|sql)$')
TESTS = re.compile(r'tests?/[\w./-]+\.py')
STAGE_WORDS = dict(design='Designing', build='Building', review='Reviewing', fix='Fixing')
# TEAM graphs (team_graph.py): stage_role values copied to the top-level task keys on import.
DESIGN_ROLES = ('design', 'design_critique')
GATE_ROLES = ('integrate', 'synthesis')  # wait until every build they depend on is finally reviewed
REVIEW_FINAL = ('approved', 'skipped', 'open')
GATE_HOLD_S = 30.0


def _files(tasks):
    return sorted({str(f).replace('\\', '/') for t in tasks for f in (t.get('files') or [])})


def is_ui(goal, tasks):
    text = ' '.join([str(goal or '')] + [str(t.get(k) or '') for t in tasks for k in ('title', 'prompt', 'role')])
    return bool(UI_WORDS.search(text) or any(UI_FILES.search(f) for f in _files(tasks)))


def choose_quality(goal, tasks, explicit=''):
    """(quality, reason): an explicit 'fast'|'standard'|'high' wins; otherwise chosen from goal and tasks."""
    explicit = str(explicit or '').strip().lower()
    if explicit:
        if explicit not in QUALITIES:
            raise ValueError("quality must be 'fast', 'standard' or 'high'")
        return explicit, 'requested'
    risks = {str(t.get('risk') or '').strip().lower() for t in tasks}
    files = _files(tasks)
    if is_ui(goal, tasks):
        return 'high', 'UI/design work'
    if 'high' in risks:
        return 'high', 'high-risk change'
    if 'medium' in risks:
        return 'standard', 'medium-risk change'
    if len(files) > 1:
        return 'standard', 'multi-file change'
    if any(CODE_FILES.search(f) for f in files):
        return 'standard', 'code change'
    return 'fast', 'small task without code risk'


def stage_of(task):
    """Plain stage word of one task."""
    if task.get('stage_role') in DESIGN_ROLES:
        return STAGE_WORDS['design']
    if task.get('role') == 'reviewer' or task.get('stage_role') == 'critique':
        return STAGE_WORDS['review']
    if task.get('fix_of'):
        return STAGE_WORDS['fix']
    return STAGE_WORDS['build']


def run_stage(tasks):
    """Plain stage word of the run: the earliest stage that still has open work."""
    live = [t for t in tasks if t.get('state') in LIVE]
    for word in ('Designing', 'Fixing', 'Reviewing', 'Building'):
        if any(stage_of(t) == word for t in live):
            return word
    return 'Done' if tasks and not live else ''


def design_prompt(goal, tasks, ui):
    files = _files(tasks)
    where = ('\nFiles involved (read them, do not edit them): ' + ', '.join(files)) if files else ''
    if ui:
        ask = ('Act as the designer. Do not edit any file. Give a concrete visual spec the builders follow exactly: '
               'layout and structure, spacing grid (px values), colors (hex), type sizes and weights, and every state '
               '(default, hover, focus, active, disabled, empty, loading, error). Start with a short, honest critique '
               'of the current look and what makes it weak.')
    else:
        ask = ('Act as the architect. Do not edit any file. Give a concrete approach the builders follow: steps, '
               'interfaces and data flow, edge cases, risks and the tests that prove it works.')
    parts = '\n'.join('- %s: %s' % (t.get('id', ''), t.get('title', '')) for t in tasks)
    return ask + where + '\nThe parts that will be built from your result:\n' + parts


def _chain(task, by_id):
    """The original executor task and its fixers, oldest first."""
    chain = [task]
    while chain[0].get('fix_of') and chain[0]['fix_of'] in by_id:
        chain.insert(0, by_id[chain[0]['fix_of']])
    return chain


def review_prompt(goal, chain):
    last = chain[-1]
    files = list(dict.fromkeys(f for t in chain for f in (t.get('assigned_files') or t.get('files') or [])))
    tests = sorted(set(TESTS.findall(' '.join(str(t.get('prompt', '')) + ' ' + str(t.get('acceptance', '')) for t in chain))))
    return ('Independently review the delegated part "%s". You are read-only: do not edit any file.\n'
            'Read the files that were actually changed: %s.\n'
            'Check them against the acceptance criteria (%s) and against the user goal: %s\n'
            'Run these tests: %s.\n'
            'Then give the verdict on a separate line: APPROVED, or Changes needed followed by a concrete fix list '
            '(one row per fix, with the file and the exact change).'
            % (last.get('title', ''), ', '.join(files) or 'none listed (use the report)', last.get('acceptance', ''),
               goal, ', '.join(tests) or 'the checks named in the acceptance criteria'))


def _strip_result_line(prompt):
    from .operator_main import RESULT_LINE
    text = str(prompt or '')
    return text[:-len(RESULT_LINE)].rstrip() if text.endswith(RESULT_LINE) else text


def _fix_list(data):
    rows = []
    for fix in data.get('fixes') or []:
        if isinstance(fix, dict):
            files = ', '.join(fix.get('files') or [])
            rows.append('- ' + ' '.join(str(fix.get(k) or '') for k in ('title', 'prompt')).strip() + (' [' + files + ']' if files else ''))
    return '\n'.join(rows) or str(data.get('summary') or '')[:3000]


def _open_points(data):
    return (_fix_list(data) or str(data.get('summary') or ''))[:1500]


READ_ONLY_TEXT = re.compile(r'\bread[- ]only\b', re.I)


def _read_only(chain):
    """True when every task of the chain was read-only (flag or prompt) and reports no changed file."""
    for t in chain:
        if not (t.get('read_only') or READ_ONLY_TEXT.search(str(t.get('prompt') or ''))):
            return False
        proof = t.get('proof') or {}
        if isinstance(proof, dict) and proof.get('files'):  # proof.record: changed files of the write scope
            return False
    return True


def _design_ids(tasks):
    """Finished design-stage tasks of the run (legacy high-quality design or TEAM design + critique)."""
    return [t['id'] for t in tasks if t.get('stage_role') in DESIGN_ROLES and t.get('state') == 'done']


def _gate_wait_for(store, run, tasks, new_id, chain):
    """Make every integrator/synthesis that depends on this chain also wait for new_id (review or fixer)."""
    ids = {t['id'] for t in chain}
    for gate in tasks:
        if gate.get('stage_role') not in GATE_ROLES or not ids & set(gate.get('deps') or []):
            continue
        if gate.get('state') in ('queued', 'retry'):
            if new_id not in gate['deps']:
                gate['deps'] = list(gate['deps']) + [new_id]
                store.put('tasks', gate)
        else:
            store.event('gate_started_early', run['id'], task_id=gate['id'],
                        message='%s started before %s existed' % (gate.get('title', ''), new_id))


def _gate(engine, run, tasks):
    """Hold a queued integrator/synthesis while a build it depends on is done but not finally reviewed.

    Reviews are queued by step() only after a build's merge settled; this hold closes that window
    (dispatch reads next_at, engine.py tick). True when a task was written."""
    by_id = {t['id']: t for t in tasks}
    now = engine.clock() if callable(getattr(engine, 'clock', None)) else time.time()
    changed = False
    for gate in tasks:
        if gate.get('stage_role') not in GATE_ROLES or gate.get('state') not in ('queued', 'retry'):
            continue
        pending = [d for d in (by_id.get(i) for i in gate.get('deps') or [])
                   if d and d.get('role') == 'executor' and d.get('state') == 'done' and not d.get('fixed_by')
                   and d.get('review_state') not in REVIEW_FINAL]
        if pending:
            if not gate.get('team_hold') or gate.get('next_at', 0) <= now:
                if not gate.get('team_hold'):
                    gate['team_hold_next_at'] = gate.get('next_at', 0)
                gate.update(team_hold=True, next_at=now + GATE_HOLD_S)
                store_put = engine.store.put
                store_put('tasks', gate)
                changed = True
        elif gate.get('team_hold'):
            gate['next_at'] = gate.pop('team_hold_next_at', 0)
            gate.pop('team_hold', None)
            engine.store.put('tasks', gate)
            changed = True
    return changed


def step(engine, run, tasks):
    """Queue the next review or fix task for every finished build; True when anything changed."""
    from .operator_main import _with_result_line
    store = engine.store
    by_id = {t['id']: t for t in tasks}
    coworkers = {w['id'] for w in store.all('workers') if w.get('purpose') == 'worker' and w.get('enabled')
                 and w.get('account_id') == run.get('account_id')}
    changed = False
    for task in tasks:
        if (task['role'] != 'executor' or task['state'] != 'done' or task.get('fixed_by')
                or task.get('review_state') in REVIEW_FINAL):
            continue
        if task.get('patch_worktree') and task.get('patch_state') in ('working', 'waiting'):
            continue  # Review only after the merge settled: the copy is removed when it merges.
        chain = _chain(task, by_id)
        review = by_id.get(task.get('review_task') or '')
        if not review and _read_only(chain):
            # A read-only audit changed no file: there is nothing for a second coworker to review
            # (bench heavy8b: the review stage cost 30-40 s per audit). The Main checks the report.
            # Honest outcome: a skipped review is 'skipped', never 'approved' (nobody approved anything).
            task.update(review_state='skipped', review_skipped='read-only task: no changed files to review')
            store.put('tasks', task)
            store.event('review_skipped', run['id'], task_id=task['id'],
                        message='Review skipped (read-only, no changed file): ' + chain[0].get('title', ''))
            changed = True
            continue
        if not review:
            done_by = {t.get('worker_id') for t in chain}
            if not coworkers - done_by:
                task.update(review_state='open', review_open='No second Coworker is available for an independent review')
                store.put('tasks', task)
                # Logged so nobody mistakes the gated integrator's later run for a reviewed build.
                store.event('review_unavailable', run['id'], task_id=task['id'],
                            message='Not reviewed (no second Coworker free): ' + chain[0].get('title', ''))
                changed = True
                continue
            designs = _design_ids(tasks)
            prompt = review_prompt(run.get('goal', ''), chain)
            if designs:
                prompt += ('\nThe design and its critique (%s) are in your context: also check that the build '
                           'follows them.' % ', '.join(designs))
            review = engine.add_task(run, 'reviewer', prompt,
                                     'Review: ' + chain[0].get('title', ''), deps=[task['id']] + designs,
                                     acceptance=task.get('acceptance', ''))
            # engine._dispatch builds a review_of task's context only from review_scope: TEAM designs go in too.
            # The legacy quality-high design (role architect) stays out of the scope; tests/test_operator_roles.py:144
            # pins that scope and is not in this task's write list (follow-up).
            team_designs = [i for i in designs if by_id.get(i, {}).get('role') == 'executor']
            review.update(review_of=task['id'], review_scope=team_designs + [t['id'] for t in chain],
                          exclude_workers=sorted(w for w in done_by if w), read_only=True,
                          review_round=len(chain))
            store.put('tasks', review)
            task['review_task'] = review['id']
            store.put('tasks', task)
            _gate_wait_for(store, run, tasks, review['id'], chain)
            store.event('stage_changed', run['id'], task_id=review['id'], message='Reviewing: ' + chain[0].get('title', ''))
            changed = True
            continue
        if review['state'] != 'done':
            continue  # still running, or blocked: a blocked review blocks the run like any task
        try:
            data = json.loads(review.get('result') or '{}')
        except ValueError:
            data = {}
        if data.get('passed'):
            task['review_state'] = 'approved'
            store.put('tasks', task)
            changed = True
        elif len(chain) - 1 < MAX_FIX_ROUNDS:
            first = chain[0]
            prompt = (_strip_result_line(first.get('prompt')) + '\n\nThe independent review asked for these fixes; '
                      'apply all of them to the same files and re-run the listed tests:\n' + _fix_list(data))
            fixer = engine.add_task(run, 'executor', _with_result_line(prompt),
                                    'Fix %d: %s' % (len(chain), first.get('title', '')), deps=list(first.get('deps') or []),
                                    files=list(first.get('assigned_files') or first.get('files') or []),
                                    acceptance=first.get('acceptance', ''), specialty=first.get('specialty', ''),
                                    model=first.get('model'), model_reason=first.get('model_reason', ''),
                                    task_class=first.get('task_class', ''), risk=first.get('risk', ''))
            fixer.update(fix_of=task['id'], prefer_worker=task.get('worker_id', ''))
            store.put('tasks', fixer)
            task['fixed_by'] = fixer['id']
            store.put('tasks', task)
            _gate_wait_for(store, run, tasks, fixer['id'], chain)  # the integrator waits for the fix too
            store.event('stage_changed', run['id'], task_id=fixer['id'], message='Fixing: ' + first.get('title', ''))
            changed = True
        else:
            task.update(review_state='open', review_open=_open_points(data))
            store.put('tasks', task)
            changed = True
    if _gate(engine, run, engine.tasks(run['id'])):
        changed = True
    stage = run_stage(engine.tasks(run['id']))
    if stage and run.get('stage') != stage:
        run['stage'] = stage
        store.put('runs', run)
    return changed
