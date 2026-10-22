"""Rule I35: model and reasoning effort per task.

Pure functions only: no I/O, no network, no global state changes. Not wired yet.

    choose(task) -> (model, effort, reason)
    notion_effort(effort, supported, escalation=0) -> effort string sent to Notion

model is a model_routing.MODELS key: 'haiku', 'sonnet' or 'opus'.
effort is 'low', 'medium' or 'high'.

Rule I35:
- Haiku 5.5 only for Haiku-class work: small, clear, mechanical (reply/echo checks,
  rename of at most 2 files, typo, docs/brief, one named simple test, a one-line file).
- Sonnet 5.5 for normal code and UI building, and for read-only check lists/audits.
- Opus 5.5 only for hard work: architecture/design specs, concurrency and race bugs
  (locks, leases, file claims), root-cause hunts, critical reviews, planning and integration.
Effort: Haiku low, Sonnet medium, Opus high. Sonnet goes low for pure read-only check
lists (hashes, CRLF, test counts) and small low-risk changes, medium for read-only audits
that need judgment, high for high risk, 4+ files or a broad change (difficulty.classify
score >= BROAD_SCORE, owned here; the team route/threshold is not used). A fix stage (title 'Fix:' / 'Fix 2:' or a set fix_of) goes one step up.

xhigh / max (decision): choose() never returns xhigh or max. Opus on hard work sends
'high' to Notion. xhigh is only sent as an escalation after a failed attempt of the same
task (escalation=1), max only on a second escalation (escalation>=2). Reason: Notion turn
time is the largest measured bottleneck (speed bench: 14.2 h of 21.6 h), and every
labeled hard task in the backtest was solvable at high; xhigh/max are kept for retries.
notion_effort() maps the choice to what is sent and clamps to the supported catalog.

Task fields read (all optional): title, prompt, role, task_class, risk, files
(list) or files_count (int), stage_role, fix_of, review_of.
"""
import re

from . import difficulty

MODELS = ('haiku', 'sonnet', 'opus')
EFFORTS = ('low', 'medium', 'high')
NOTION_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
# Only the start of a prompt is scanned: long prompts repeat boilerplate (tool access
# notes, restart rules) that must not change the class of the task.
PROMPT_HEAD = 300
# Sonnet effort goes high for a broad change only at this difficulty score. Owned here on
# purpose: difficulty.TEAM_THRESHOLD / route 'team' decide team routing and may change.
BROAD_SCORE = 0.8

_I = re.IGNORECASE
_PREFIX = re.compile(r'^(?:(?:fix(?:\s*\d+)?|review|part\s+\w+)\s*:\s*)+', _I)
_FIX_TITLE = re.compile(r'^fix\s*\d*\s*:', _I)

# Hard signals. (name, pattern, where): where is 'title' (core title only) or 'any'.
HARD = (
    ('architecture or design spec', re.compile(
        r'\barchitect\w*|\bredesign spec\b|\bcritique\b|\bcritic\b|\bdesigner\b', _I), 'any'),
    ('concurrency/race', re.compile(
        r'\brace\b|race condition|concurren\w*|deadlock\w*|thread[- ]safe\w*|'
        r'\bfile[ _-]?(?:lock|lease)s?\b|second main|'
        r'\b(?:file|lock|lease)\w*\b[^.]{0,40}\bclaims?\b|\bclaims?\b[^.]{0,40}\b(?:file|lock|lease)', _I), 'any'),
    ('root-cause hunt', re.compile(
        r'root[- ]cause|\bwho changed\b|\bfailures?\b|\bfail(?:ed|s|ing)?\b|'
        r'\bshows? (?:the )?wrong\b|\bwhy\b', _I), 'title'),
    ('root-cause hunt (prompt asks for the cause)', re.compile(
        r'root[- ]cause|\bfind (?:out )?(?:the )?(?:cause|reason|why)\b|\bexplain why\b|'
        r'\bfind out why\b|\bwho changed\b|\bdecide (?:if|whether)\b[^.]{0,40}\bwrong\b|; find out\b', _I), 'prompt'),
    ('forensic proof', re.compile(
        r'\bdoes notion really run\b|\breally runs? the chosen\b|\bprove that notion\b|\bforensic\w*', _I), 'any'),
    ('critical review', re.compile(
        r'review all changes critically|independently review|integration (?:and )?review|'
        r'review and combine|^review \+|acceptance check|final (?:independent )?review', _I), 'any'),
)
PLAN = re.compile(r'^plan\b', _I)

# Haiku signals. where: 'title' = core title only, 'any' = title or prompt head.
HAIKU = (
    ('echo/arithmetic check', re.compile(
        r'reply with exactly|answer with just|state the result|\bwork out\b|'
        r'\blist (?:two|three|four|five|\d+) short\b', _I), 'any'),
    ('one-line file', re.compile(r'\bexactly (?:the )?(?:single|one) line\b|\bwith the single line\b', _I), 'any'),
    ('one-line edit', re.compile(r'\bchange only line\b', _I), 'any'),
    ('docs', re.compile(
        r'^(?:docs?|document|brief|readme)\b|^(?:write|update|refresh) (?:the |a )?(?:[\w-]+ ){0,3}'
        r'(?:brief|docs?|readme)\b', _I), 'title'),
    ('small text change', re.compile(r'^point the first message\b', _I), 'title'),
    ('typo', re.compile(r'\btypo\w*', _I), 'title'),
    ('rename', re.compile(r'^(?:rename|umbenenn)\w*\b', _I), 'title'),
    ('simple test', re.compile(
        r'line-ending guard|^(?:write|add) (?:a |one |the )?(?:[\w.-]+ ){0,3}test\b(?! suite)', _I), 'title'),
    ('rerun and report', re.compile(r'^recheck\b', _I), 'title'),
)
# Reviews of backtest fixture work (a file with one line, a file and its test) are mechanical.
REVIEW_MECH = re.compile(r'\bbacktest\b[^.]{0,20}\bfile\b|\bfile and test\b', _I)
NOT_SIMPLE_TEST = re.compile(r'integration|end[- ]to[- ]end|\be2e\b|\bsuite\b|\blive\b', _I)

READ_ONLY = re.compile(
    r'change nothing|change no files|keine datei (?:ändern|aendern)|do not edit any file|'
    r'don\'t edit any file|read-only|nur lesen|write nothing', _I)
# Read-only work that asks for a judgment gets medium; pure check lists stay low.
JUDGMENT = re.compile(
    r'\baudit\w*|supersed\w*|list every\b|\bevery field\b|\bjudge\b|\bassess\w*|\bwhether\b|'
    r'\bsay if\b|\bdecide\b|\bbewert\w*', _I)

OPUS_ROLES = ('planner', 'architect', 'integrator')
TOP_CLASSES = ('review',)


def _files_count(task):
    files = task.get('files')
    if isinstance(files, (list, tuple)):
        return len(files)
    try:
        return max(0, int(task.get('files_count') or 0))
    except (TypeError, ValueError):
        return 0


def _text(task):
    title = ' '.join(str(task.get('title') or '').split())
    prompt = ' '.join(str(task.get('prompt') or '').split())[:PROMPT_HEAD]
    return title, _PREFIX.sub('', title), prompt


def _hits(rules, core, prompt):
    out = []
    for name, rx, where in rules:
        texts = {'title': (core,), 'prompt': (prompt,), 'any': (core, prompt)}[where]
        if any(t and rx.search(t) for t in texts):
            out.append(name)
    return out


def _haiku_hits(core, prompt, nfiles):
    hits = _hits(HAIKU, core, prompt)
    if 'rename' in hits and nfiles > 2:
        hits.remove('rename')
    if 'simple test' in hits and NOT_SIMPLE_TEST.search(core):
        hits.remove('simple test')
    return hits


def is_fix_stage(task):
    """A fix stage is a set fix_of or a title 'Fix:' / 'Fix 2:'. 'Fix the import' is not."""
    title = ' '.join(str((task or {}).get('title') or '').split())
    return bool((task or {}).get('fix_of')) or bool(_FIX_TITLE.match(title))


def _up(effort):
    return EFFORTS[min(len(EFFORTS) - 1, EFFORTS.index(effort) + 1)]


def _is_ui(task, text):
    try:
        from .operator_roles import is_ui
        return bool(is_ui('', [{'title': text, 'files': list(task.get('files') or [])
                                 if isinstance(task.get('files'), (list, tuple)) else []}]))
    except Exception:  # never let the UI flag break the choice
        return False


def choose(task):
    """Return (model, effort, reason) for one task dict. The input is never changed."""
    task = dict(task or {})
    role = str(task.get('role') or 'executor').strip().lower()
    cls = str(task.get('task_class') or '').strip().lower()
    risk = str(task.get('risk') or '').strip().lower()
    nfiles = _files_count(task)
    title, core, prompt = _text(task)
    fix = is_fix_stage(task)
    tail = ' (fix stage, effort +1)' if fix else ''

    def done(model, effort, why):
        if fix:
            effort = _up(effort)
        return model, effort, '%s 5.5 / %s: %s%s' % (model.capitalize(), effort, why, tail)

    if role in OPUS_ROLES:
        return done('opus', 'high', 'role %s (planning/integration is hard work)' % role)
    if str(task.get('stage_role') or '').strip().lower() == 'design':
        return done('opus', 'high', 'design stage (architecture/visual spec)')
    if role == 'reviewer' or task.get('review_of'):
        easy = _haiku_hits(core, '', nfiles)
        if REVIEW_MECH.search(core):
            easy.append('backtest file/test')
        if easy and not _hits(HARD, core, ''):
            return done('sonnet', 'low', 'review of mechanical work (%s): no critical review needed'
                        % ', '.join(easy))
        return done('opus', 'high', 'critical review')
    if role == 'context_compactor':
        return done('haiku', 'low', 'context compaction is mechanical')

    hard = _hits(HARD, core, prompt)
    if PLAN.search(core):
        hard.append('planning')
    if cls in TOP_CLASSES:
        hard.append('task_class review')
    if hard:
        return done('opus', 'high', 'hard work: ' + ', '.join(hard))

    if READ_ONLY.search(core) or READ_ONLY.search(prompt):
        if JUDGMENT.search(core) or JUDGMENT.search(prompt):
            return done('sonnet', 'medium', 'read-only audit that needs judgment (no hard signal)')
        return done('sonnet', 'low', 'read-only check list (hashes, line endings, test counts)')

    ui = _is_ui(task, core + ' ' + prompt)
    easy = _haiku_hits(core, prompt, nfiles)
    if easy and not ui and nfiles < 4 and risk != 'high':
        return done('haiku', 'low', 'Haiku-class: ' + ', '.join(easy))

    route = difficulty.classify(core + '. ' + prompt)
    why = ['normal %s' % ('UI building' if ui else 'code')]
    if easy:
        why.append('mechanical signal %s overridden (%s)' % (', '.join(easy),
                   'UI work' if ui else ('%d files' % nfiles if nfiles >= 4 else 'risk high')))
    if risk == 'high':
        effort = 'high'
        why.append('risk high')
    elif nfiles >= 4:
        effort = 'high'
        why.append('%d files' % nfiles)
    elif route['score'] >= BROAD_SCORE:
        effort = 'high'
        why.append('broad change (difficulty score %.2f)' % route['score'])
    elif risk == 'low' and nfiles <= 3:
        effort = 'low'
        why.append('risk low, small change')
    else:
        effort = 'medium'
    return done('sonnet', effort, '; '.join(why))


def notion_effort(effort, supported=NOTION_EFFORTS, escalation=0, purpose=''):
    """Effort string to send to Notion.

    Coworkers (any purpose that is not a Main purpose, owner rule 2026-10-22): the choice goes
    into the band medium..high first (low -> medium), escalation adds one step per failed attempt
    of the same task, and the result stays in the band (never xhigh or max); then it is clamped
    to `supported` with effort_policy.band_effort.
    Main purposes keep the full range: escalation raises ANY effort by one step
    (low -> medium -> high -> xhigh -> max), capped at max, clamped to `supported`
    (the highest supported level not above the wanted one, else the lowest supported)."""
    from .effort_policy import MAIN_PURPOSES, band_effort, cap_coworker
    effort = effort if effort in NOTION_EFFORTS else 'medium'
    steps = max(0, int(escalation or 0))
    sup = [e for e in NOTION_EFFORTS if e in tuple(supported or ())]
    if purpose not in MAIN_PURPOSES:
        idx = NOTION_EFFORTS.index(cap_coworker(effort, purpose)) + steps
        wanted = cap_coworker(NOTION_EFFORTS[min(idx, len(NOTION_EFFORTS) - 1)], purpose)
        if not sup:
            return wanted
        return band_effort(wanted, sup, purpose)[0]
    idx = min(NOTION_EFFORTS.index(effort) + steps, len(NOTION_EFFORTS) - 1)
    if not sup:
        return NOTION_EFFORTS[idx]
    below = [e for e in sup if NOTION_EFFORTS.index(e) <= idx]
    return below[-1] if below else sup[0]


def score(cases, picker):
    """Backtest helper (pure). cases: dicts with 'label_model' and 'label_effort';
    picker(case) -> (model, effort). Efforts are compared exactly: xhigh and max are NOT
    merged into high (they count as a miss and are listed in 'effort_counts').
    Returns accuracy, confusion and error costs."""
    confusion = {want: {got: 0 for got in MODELS + ('none',)} for want in MODELS}
    model_ok = effort_ok = both_ok = 0
    waste = {'opus_on_haiku': 0, 'opus_on_sonnet': 0, 'sonnet_on_haiku': 0}
    risk = {'haiku_on_opus': 0, 'haiku_on_sonnet': 0, 'sonnet_on_opus': 0}
    effort_counts = {}
    misses = []
    for case in cases:
        got_m, got_e = picker(case)
        got_m = got_m if got_m in MODELS else 'none'
        got_e = str(got_e or '').lower() or 'none'
        effort_counts[got_e] = effort_counts.get(got_e, 0) + 1
        want_m, want_e = case['label_model'], case['label_effort']
        confusion[want_m][got_m] += 1
        model_ok += got_m == want_m
        effort_ok += got_e == want_e
        both_ok += got_m == want_m and got_e == want_e
        key = '%s_on_%s' % (got_m, want_m)
        if key in waste:
            waste[key] += 1
        if key in risk:
            risk[key] += 1
        if got_m != want_m:
            misses.append((case.get('id', ''), want_m, got_m))
    n = len(cases) or 1
    # Cost units: price ratio Haiku:Sonnet:Opus taken as 1:3:5 (assumption) for waste;
    # quality risk is weighted by how far below the needed class the pick is.
    waste_units = 4 * waste['opus_on_haiku'] + 2 * waste['opus_on_sonnet'] + 2 * waste['sonnet_on_haiku']
    risk_units = 3 * risk['haiku_on_opus'] + 1 * risk['haiku_on_sonnet'] + 2 * risk['sonnet_on_opus']
    return {'n': len(cases), 'model_accuracy': model_ok / n, 'effort_accuracy': effort_ok / n,
            'both_accuracy': both_ok / n, 'confusion': confusion, 'waste': waste, 'risk': risk,
            'waste_units': waste_units, 'risk_units': risk_units, 'misses': misses,
            'effort_counts': effort_counts}
