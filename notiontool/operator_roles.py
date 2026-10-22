"""Role stages for operator runs: design (high), build, review and fix (standard and high).

The operator still integrates the reports itself; every stage task runs on a Coworker, never in the
operator's own chat. Designers and reviewers are read-only: they hold no files, so they never take
a file lock and never block another Coworker.
"""
import json
import re
import time
from pathlib import Path

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
# Review economy (token saving). Each saving has its own setting; missing or unknown values = the
# first entry (the default). 'opus' / 'full' restore the old behavior exactly.
REVIEW_MODEL_POLICIES = ('auto', 'opus')
REVIEW_FOLLOWUP_SCOPES = ('fix_only', 'full')
REVIEW_CORE_FILES = ('notiontool/engine.py', 'notiontool/notion.py', 'notiontool/token_budget.py',
                     'notiontool/bridge.py', 'notiontool/edge_transport.py', 'notiontool/server.py',
                     'notiontool/network_budget.py')
REVIEW_SONNET_MAX_FILES = 2
REVIEW_SMALL_FILE_LINES = 400  # size limit when the acceptance names none (setting review_small_file_lines)
SIZE_HINT = re.compile(r'(?i)\b(\d{1,6})\s*(?:lines?|loc)\b')


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


def _settings(engine):
    """store.settings() read defensively: any error or a non-dict means 'all defaults'."""
    try:
        got = engine.store.settings()
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def _choice(settings, key, allowed):
    value = str((settings or {}).get(key) or '').strip().lower()
    return value if value in allowed else allowed[0]


def _norm(path):
    text = str(path or '').strip().replace('\\', '/')
    while text.startswith('./'):
        text = text[2:]
    return text.lower()


def _core_files(settings):
    raw = (settings or {}).get('review_core_files')
    if raw is None:
        raw = REVIEW_CORE_FILES
    if isinstance(raw, str):
        raw = re.split(r'[,;\n]', raw)
    try:
        return {_norm(f) for f in raw if _norm(f)}
    except TypeError:
        return {_norm(f) for f in REVIEW_CORE_FILES}


def _changed_files(tasks):
    """Planned files plus the changed files of the proof record, in order, without duplicates."""
    out = []
    for t in tasks:
        proof = t.get('proof') if isinstance(t.get('proof'), dict) else {}
        listed = list(t.get('assigned_files') or t.get('files') or []) + list(proof.get('files') or [])
        for f in listed:
            f = f.get('path') if isinstance(f, dict) else f
            f = str(f or '').strip().replace('\\', '/')
            if f and f not in out:
                out.append(f)
    return out


def _tests(chain):
    return sorted(set(TESTS.findall(' '.join(str(t.get('prompt', '')) + ' ' + str(t.get('acceptance', '')) for t in chain))))


def _root(engine, run):
    try:
        return Path(engine._project_root(run))
    except Exception:
        return Path(run['project_path']) if run.get('project_path') else None


def _rel(root, name):
    path = Path(name)
    if root is not None and path.is_absolute():
        try:
            return path.resolve().relative_to(Path(root).resolve()).as_posix()
        except (ValueError, OSError):
            return name
    return name


def _line_count(root, name):
    """Lines of the file now, or None when it cannot be read (unknown size counts as large)."""
    path = Path(name)
    if not path.is_absolute():
        if root is None:
            return None
        path = Path(root) / name
    try:
        with open(path, 'rb') as fh:
            return sum(1 for _ in fh)
    except OSError:
        return None


def _line_limit(chain, settings):
    """Largest 'N lines' the acceptance names; otherwise setting review_small_file_lines."""
    hints = [int(n) for t in chain for n in SIZE_HINT.findall(str(t.get('acceptance') or ''))]
    if hints:
        return max(1, max(hints))
    try:
        return max(1, int((settings or {}).get('review_small_file_lines', REVIEW_SMALL_FILE_LINES)))
    except (TypeError, ValueError):
        return REVIEW_SMALL_FILE_LINES


def review_model(engine, run, chain, settings=None):
    """(model, model_reason) for the review of chain; (None, '') = old behavior (engine default model).

    Setting review_model_policy: 'auto' (default) picks 'sonnet' for a chain that changed at most
    REVIEW_SONNET_MAX_FILES files, none of them a core file (setting review_core_files) and none larger
    than its acceptance suggests; everything else gets an explicit 'opus'. 'opus' = old behavior."""
    settings = _settings(engine) if settings is None else settings
    if _choice(settings, 'review_model_policy', REVIEW_MODEL_POLICIES) == 'opus':
        return None, ''
    why = 'review_model_policy auto: '
    files = _changed_files(chain)
    if not files:
        return 'opus', why + 'no changed file is listed, so the risk is unknown: opus review'
    if len(files) > REVIEW_SONNET_MAX_FILES:
        return 'opus', why + '%d changed files (more than %d): opus review' % (len(files), REVIEW_SONNET_MAX_FILES)
    root = _root(engine, run)
    core_set = _core_files(settings)
    core = [f for f in files if _norm(_rel(root, f)) in core_set]
    if core:
        return 'opus', why + 'core file changed (%s): opus review' % ', '.join(core)
    limit = _line_limit(chain, settings)
    for f in files:
        lines = _line_count(root, f)
        if lines is None:
            return 'opus', why + 'size of %s unknown: opus review' % f
        if lines > limit:
            return 'opus', why + '%s has %d lines (more than %d): opus review' % (f, lines, limit)
    return 'sonnet', why + 'small non-core change (%s; at most %d lines each): sonnet review' % (', '.join(files), limit)


def _previous_fix_list(chain, by_id):
    """Fix list of the review that sent the last fixer, from that review's result (or the fixer prompt)."""
    if len(chain) < 2:
        return ''
    prev = by_id.get(chain[-2].get('review_task') or '') or {}
    try:
        data = json.loads(prev.get('result') or '{}')
    except ValueError:
        data = {}
    text = _fix_list(data) if isinstance(data, dict) else ''
    if text:
        return text
    prompt = _strip_result_line(chain[-1].get('prompt'))
    marker = 're-run the listed tests:\n'
    return prompt.split(marker, 1)[1].strip() if marker in prompt else ''


def followup_review_prompt(goal, chain, fix_list):
    """Review round >= 2 with review_followup_scope 'fix_only': check the fix, not the whole part again."""
    last = chain[-1]
    changed = _changed_files([last])
    return ('Follow-up review (round %d) of the delegated part "%s". You are read-only: do not edit any file.\n'
            'Do not re-review the whole part. The previous independent review asked for this fix list:\n%s\n'
            'The last fixer (%s) changed these files: %s.\n'
            'Check only two things: that every fix of this list was applied correctly, and that the fixer\'s '
            'change broke nothing (read those changed files; acceptance criteria: %s; user goal: %s).\n'
            'Run these tests: %s.\n'
            'Then give the verdict on a separate line: APPROVED, or Changes needed followed by a concrete fix list '
            '(one row per fix, with the file and the exact change).'
            % (len(chain), chain[0].get('title', ''), fix_list or '(fix list not available: use the fixer report)',
               last.get('title', '') or last.get('id', ''), ', '.join(changed) or 'none listed (use the report)',
               last.get('acceptance', ''), goal, ', '.join(_tests(chain)) or 'the checks named in the acceptance criteria'))


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
            settings = _settings(engine)
            followup = (len(chain) >= 2
                        and _choice(settings, 'review_followup_scope', REVIEW_FOLLOWUP_SCOPES) == 'fix_only')
            if followup:
                prompt = followup_review_prompt(run.get('goal', ''), chain, _previous_fix_list(chain, by_id))
            else:
                prompt = review_prompt(run.get('goal', ''), chain)
            if designs:
                prompt += ('\nThe design and its critique (%s) are in your context: also check that the build '
                           'follows them.' % ', '.join(designs))
            model, model_reason = review_model(engine, run, chain, settings)
            model_kw = dict(model=model, model_reason=model_reason) if model else {}  # none = old default
            review = engine.add_task(run, 'reviewer', prompt,
                                     'Review: ' + chain[0].get('title', ''), deps=[task['id']] + designs,
                                     acceptance=task.get('acceptance', ''), **model_kw)
            if followup:
                review['review_followup'] = 'fix_only'
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
