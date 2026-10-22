"""Request difficulty: team route (plan/critique/review) or direct route (single coworkers).

Pure function only: no I/O, no network, no state. The decision comes from the CONTENT of the
request, never from its length. German and English signals.

classify(text, context=None) -> {'route': 'team'|'direct', 'score': float 0..1, 'reasons': [str]}
score >= TEAM_THRESHOLD means team.
"""
import re

TEAM_THRESHOLD = 0.55
BASE_SCORE = 0.3
AMBIGUOUS_WEIGHT = 0.25
# Plain deletion of named files: many files, still one narrow change.
DELETE = re.compile(r'\b(lösch\w*|delete\w*|remove\w*|entfern\w*)\b', re.IGNORECASE)

_I = re.IGNORECASE

# Broad scope of the change.
SCOPE = re.compile(
    r'\b(re-?design\w*|komplett\w*|complete(?:ly)?|neu\s*(?:machen|gestalten|bauen|schreiben)|'
    r'neugestalt\w*|neu|from\s+scratch|von\s+grund\s+auf|architecture|architektur|'
    r'refactor\w*|refaktor\w*|ganzen?\s+(?:app|anwendung|projekt|seite|ui|code)|'
    r'whole\s+(?:app|project|ui|system|codebase)|system|ui|ux|design|security|sicherheit|'
    r'migration|migrate|migrier\w*|performance|overhaul|rewrite|umbau\w*|rebuild|'
    r'überarbeit\w*|umgestalt\w*|verbesser\w*|rework\w*|revamp\w*|improv\w*|modernis\w*|moderniz\w*)\b', _I)
# Restart/reload phrasings: their "neu" is not a scope word (starte ... neu, lade neu, neu installieren).
_RESTART_VERB = (r'(?:start(?:e|en|et|est)|lad(?:e|en|et)|installier(?:e|en|t)|'
                 r'verbind(?:e|en|et)|meld(?:e|en|et))')
RESTART = re.compile(
    r'\b' + _RESTART_VERB + r'\b[^.!?\n]{0,60}?\bneu\b(?!\s*(?:machen|gestalten|bauen|schreiben)\b)|\bneu\s*' + _RESTART_VERB + r'\b', _I)
# Subjective quality goals.
QUALITY = re.compile(
    r'\b(schön\w*|hübsch\w*|besser\w*|hässlich\w*|beschissen|schrecklich|modern\w*|ugly|'
    r'prett(?:y|ier)|nicer|better|cleaner|polish\w*|looks?\s+(?:bad|terrible|awful|ugly|like\s+shit)|'
    r'sieht\s+\w+(?:\s+\w+)?\s+aus)\b', _I)
# Many affected areas.
AREAS = re.compile(
    r'\b(überall|everywhere|alle[ns]?|all\s+(?:pages|screens|files|modules|parts|areas|views)|'
    r'across\s+the\s+(?:app|codebase|project)|mehrere|several|many|viele)\b', _I)
# Concrete targets.
FILE = re.compile(
    r'(?<![\w/.-])((?:[\w.-]+/)*[\w-]+\.(?:py|js|ts|tsx|jsx|md|txt|json|toml|ya?ml|css|scss|html?|'
    r'ps1|sh|cfg|ini))\b|\b(README|CHANGELOG|LICENSE|Makefile|Dockerfile)\b')
# Framework/tool names that look like files but are not targets.
FRAMEWORKS = frozenset(('node.js', 'next.js', 'vue.js', 'three.js', 'express.js', 'chart.js',
                        'nuxt.js', 'react.js', 'angular.js', 'd3.js', 'ember.js', 'backbone.js'))
_FRAMEWORK_NAME = re.compile(r'^[A-Z][\w-]*\.js$')
# Code symbols such as engine._restore or test_prompt_once are concrete targets.
IDENT = re.compile(r'\b(?:[a-z]\w*\._?[a-z]\w*(?!\.(?:py|js|ts|md|json))|[a-z]+(?:_[a-z0-9]+)+|[A-Z][A-Z0-9]*_[A-Z0-9_]+)\b')
LINE = re.compile(r'\b(?:line|zeile)\s+\d+\b|\.\w{1,5}:\d+\b', _I)
# Narrow change kinds.
NARROW = (
    ('typo', re.compile(r'\b(typo\w*|tippfehler\w*|rechtschreib\w*|spelling)\b', _I)),
    ('rename', re.compile(r'\b(rename\w*|umbenenn\w*)\b|\bbenenn\w*\b[^.]{0,60}\bum\b', _I)),
    ('single bug with error message', re.compile(
        r'\b\w*(?:Error|Exception)\b|\btraceback\b|\bfehlermeldung\b|\bstack\s*trace\b')),
    ('add one test', re.compile(
        r'\b(?:add|write|ergänz\w*|schreib\w*)\s+'
        r'(?:(?:one|a\s+single|a|an|another|ein|einen|einzelnen?|noch\s+einen)\s+)?'
        r'(?:unit\s*)?test(?:fall)?\b'
        r'(?![\s-]*(?:framework|suite|harness|plan|infrastructure|infrastruktur|system|setup))', _I)),
    ('restart only', None),
    ('set one value', re.compile(
        r'\b(?:setz\w*|set|erhöh\w*|senk\w*|raise|lower|increase|decrease|change|änder\w*|mach\w*)\b'
        r'[^.!?\n]{0,60}?(?:\b(?:auf|to)\s+(?:\d[\d_.]*|\'[^\']+\'|"[^"]+")|'
        r'\b(?:grün|rot|blau|gelb|schwarz|weiß|green|red|blue|yellow|black|white)\b)', _I)),
    ('run or report only', re.compile(
        r'\b(?:run|führ\w*|lass\w*)\b[^.!?\n]{0,50}\btests?\b[^.!?\n]{0,40}\b(?:tell|sag\w*|report|zeig\w*|melde?)\b', _I)),
    ('one failing test', re.compile(
        r'\b(?:the|den|einen|this|diesen)\s+(?:failing|fehlschlagenden|roten|kaputten)\s+test\b', _I)),
    ('bump version', re.compile(
        r'\bbump\w*\b|\bversion\s+(?:erhöh\w*|auf|to)\b|\berhöh\w*\s+(?:die\s+)?version\b', _I)),
    ('docs only', re.compile(
        r'^\s*(?:please\s+|bitte\s+)?(?:update|refresh|aktualisier\w*|ergänz\w*)\s+(?:the\s+|die\s+|das\s+)?'
        r'(?:docs?|documentation|doku\w*|readme|changelog)\b[^.!?\n]{0,60}$', _I)),
)
TESTS_GREEN = re.compile(
    r'\b(?:mach\w*|make|get|krieg\w*|bring\w*)\s+(?:all\s+|alle\s+)?(?:the\s+|die\s+)?(?:tests?|ci|build)\s+'
    r'(?:wieder\s+|again\s+)?(?:grün|green|pass(?:ing)?)\b', _I)


def _distinct(matches):
    seen = []
    for m in matches:
        word = ' '.join(m.lower().split())
        if word not in seen:
            seen.append(word)
    return seen


def _is_framework(name):
    return '/' not in name and (name.lower() in FRAMEWORKS or bool(_FRAMEWORK_NAME.match(name)))


def _files(text, context):
    found = []
    names = [(m.group(1) or m.group(2)) for m in FILE.finditer(text)]
    names += [str(f) for f in ((context or {}).get('files') or [])]
    for raw in names:
        name = raw.replace('\\', '/')
        if not name or _is_framework(name):
            continue
        if name.lower() not in [f.lower() for f in found]:
            found.append(name)
    return found


def classify(text, context=None):
    """Decide 'team' or 'direct' from content signals only; length is never used."""
    if text is None:
        text = ''
    if not isinstance(text, str):
        raise TypeError('text must be a str')
    if context is not None and not isinstance(context, dict):
        raise TypeError('context must be a dict or None')
    reasons = []
    score = BASE_SCORE

    scope_text = RESTART.sub(' ', text)
    if scope_text != text:
        reasons.append('restart/reload phrasing: its "neu" is not counted as scope')
    scope = _distinct(m.group(0) for m in SCOPE.finditer(scope_text))
    restart_only = scope_text != text and not scope
    if scope:
        score += min(0.55, 0.35 + 0.1 * (len(scope) - 1))
        reasons.append('scope: ' + ', '.join(scope))
    quality = _distinct(m.group(0) for m in QUALITY.finditer(text))
    if quality:
        score += 0.3
        reasons.append('subjective quality goal: ' + ', '.join(quality))
    areas = _distinct(m.group(0) for m in AREAS.finditer(text))
    if areas:
        score += 0.2
        reasons.append('many areas: ' + ', '.join(areas))

    named = _files(text, context)
    # A symbol that is only part of a named file path (main_switch inside notiontool/main_switch.py) is the same target.
    files = named + [s for s in _distinct(IDENT.findall(text))
                     if not any(s.lower() in f.lower() for f in named)]
    lines = LINE.findall(text)
    if len(files) >= 3 and DELETE.search(text):
        score -= 0.2
        reasons.append('only deleting named files: %d' % len(files))
        scope = []
    elif len(files) >= 3:
        score += 0.3
        reasons.append('many files: %d' % len(files))
    elif files:
        if scope or quality:
            reasons.append('named file inside a broad change: ' + ', '.join(files))
        else:
            score -= 0.2
            reasons.append('concrete target file: ' + ', '.join(files))
    if lines:
        score -= 0.1
        reasons.append('concrete line reference')
    if not files and not lines:
        # An open request with no named target needs planning: the team decides what to touch.
        score += AMBIGUOUS_WEIGHT
        reasons.append('ambiguous: no named target file or line')

    narrow = [name for name, rx in NARROW if (restart_only if rx is None else rx.search(text))]
    if TESTS_GREEN.search(text):
        # "make the tests green" is an open fix with unknown causes, not setting a color value.
        narrow = [n for n in narrow if n != 'set one value']
        score += 0.1
        reasons.append('open fix: make failing tests pass')
    if narrow:
        score -= min(0.5, 0.35 + 0.1 * (len(narrow) - 1))
        reasons.append('narrow change: ' + ', '.join(narrow))

    score = round(max(0.0, min(1.0, score)), 3)
    route = 'team' if score >= TEAM_THRESHOLD else 'direct'
    reasons.append('score %.3f vs threshold %.2f -> %s' % (score, TEAM_THRESHOLD, route))
    return {'route': route, 'score': score, 'reasons': reasons}
