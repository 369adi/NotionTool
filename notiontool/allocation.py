"""Capacity is a ceiling, not a reason to manufacture work.

Phase 4 planner modes (pure decision in planner_mode):
- DIRECT: one bounded request, one turn on the Main, no planner or integrator stage.
- SPLIT: the planner builds a dependency graph within the available capacity.
- DEEP: analysis or review comes first, then execution.
mode stays 'direct' or 'planned' so stored runs and existing callers keep working.
"""
import re
from .protocol import ContractError

DIRECT_MAX_WORDS = 40
DEEP_MIN_WORDS = 150

CONVERSATIONAL = r'(?:hi|hello|hey|hallo|guten tag|danke|thanks|thank you)[!.,\s]*'
REQUESTED = (r'\b(?:exactly|genau|use|nutze)\s+(\d+|ten|forty|zehn|vierzig)\s+(?:independent\s+)?'
             r'(?:workers?|contributors?|contributions?|calculations?|parts|coworker|berechnungen|beiträge)')
# A bounded ask: a question word or a short imperative at a sentence start or after a colon.
ASK = (r'(?:^|[:.!?]\s*)(?:what|who|which|when|where|why|how|is|are|can|could|does|do|reply|answer|respond|say|'
       r'write|give|list|name|define|summari[sz]e|translate|explain|calculate|convert|'
       r'was|wer|welche[rsmn]?|wann|wo|warum|wie|ist|sind|kann|antworte|schreibe|nenne|gib|liste|definiere|'
       r'fasse|übersetze|erkläre|rechne|berechne)\b')
# Work that needs a plan, other workers, tools or files is never DIRECT.
NEEDS_PLAN = (r'\b(?:delegate|delegier\w*|split|aufteil\w*|coworkers?|workers?|arbeiter\w*|parallel\w*|independent|'
              r'unabhängig\w*|subtasks?|teilaufgaben|tasks|aufgaben|steps|schritte|plan|implement\w*|entwickle\w*|'
              r'build|baue|refactor\w*|fix|behebe|tools?|mcp|screen_capture|screenshot|capture|files?|datei\w*|'
              r'code|repo\w*|browser|search|suche|web|tests?)\b')
DEEP = (r'\b(?:compare|vergleiche?\w*|research|recherch\w*|diagnos\w*|debug\w*|audit\w*|review\w*|analy[sz]\w*|'
        r'investigate|untersuche\w*|legal|rechtlich\w*|medical|medizin\w*|architecture|architektur\w*)\b')
LIST_ITEM = r'(?m)^\s*(?:\d+[.)]|[-*•])\s+'


def planner_mode(goal):
    """Pure decision for one user goal: 'direct', 'split' or 'deep'."""
    text = (goal or '').strip().lower()
    if not text or re.fullmatch(CONVERSATIONAL, text):
        return 'direct'
    if re.search(REQUESTED, text):
        return 'split'
    words = len(text.split())
    if words >= DEEP_MIN_WORDS or re.search(DEEP, text):
        return 'deep'
    if (words <= DIRECT_MAX_WORDS and text.count('?') <= 1 and '\n\n' not in text
            and not re.search(LIST_ITEM, text) and not re.search(NEEDS_PLAN, text)
            and (re.search(ASK, text) or text.endswith('?'))):
        return 'direct'
    return 'split'


def allocation(goal, capacity):
    text = goal.strip().lower()
    if re.fullmatch(CONVERSATIONAL, text):
        return dict(mode='direct', planner_mode='direct', budget=1, advisers=0, reason='Conversational message')
    requested = re.search(REQUESTED, text)
    if requested:
        number = {'ten': 10, 'forty': 40, 'zehn': 10, 'vierzig': 40}.get(requested[1])
        number = number if number is not None else int(requested[1])
        return dict(mode='planned', planner_mode='split', requested_parts=number, budget=max(1, min(capacity, number)),
                    advisers=min(3, capacity), reason='Explicit independent contribution count')
    kind = planner_mode(text)
    if kind == 'direct':
        return dict(mode='direct', planner_mode='direct', budget=1, advisers=0,
                    reason='DIRECT: one bounded request; the Main answers in one turn without planner or integrator')
    if kind == 'deep':
        return dict(mode='planned', planner_mode='deep', budget=max(1, capacity), advisers=min(3, capacity),
                    reason='DEEP: analysis or review first, then execution within available capacity')
    return dict(mode='planned', planner_mode='split', budget=max(1, capacity), advisers=min(3, capacity),
                reason='SPLIT: planner selects useful independent work within available capacity')


def validate_allocation(plan, policy):
    requested = policy.get('requested_parts')
    if requested is None:
        return
    if len(plan['tasks']) != requested:
        raise ContractError(f'Plan needs exactly {requested} execution contributions. Do not include review or integration tasks; those are separate stages.')
    if any(t.get('depends_on') for t in plan['tasks']):
        raise ContractError('The requested contributions must be independent: use no dependencies and make each calculation/check self-contained, without relying on another contribution.')


def parallel_width(tasks):
    """Maximum runnable wave in the validated dependency graph."""
    remaining = {t['id']: set(t.get('depends_on', [])) for t in tasks}
    done, width = set(), 1
    while remaining:
        wave = {ident for ident, deps in remaining.items() if deps <= done}
        if not wave:
            break
        width = max(width, len(wave))
        done |= wave
        remaining = {ident: deps for ident, deps in remaining.items() if ident not in wave}
    return width
