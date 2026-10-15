"""Capacity is a ceiling, not a reason to manufacture work."""
import re
from .protocol import ContractError


def allocation(goal, capacity):
    text = goal.strip().lower()
    if re.fullmatch(r'(?:hi|hello|hey|hallo|guten tag|danke|thanks|thank you)[!.,\s]*', text):
        return dict(mode='direct', budget=1, advisers=0, reason='Conversational message')
    requested = re.search(r'\b(?:exactly|genau|use|nutze)\s+(\d+|ten|forty|zehn|vierzig)\s+(?:independent\s+)?(?:workers?|contributors?|contributions?|calculations?|parts|coworker|berechnungen|beiträge)', text)
    if requested:
        number = {'ten':10, 'forty':40, 'zehn':10, 'vierzig':40}.get(requested[1])
        number = number if number is not None else int(requested[1])
        return dict(mode='planned', requested_parts=number, budget=max(1, min(capacity, number)), advisers=min(3, capacity), reason='Explicit independent contribution count')
    small = len(text.split()) <= 30 and (re.match(r'(?:what is|what are|who is|how many|how much|calculate|translate|explain briefly|übersetze|rechne|berechne|wie viel|was ist|wer ist|was sind)\b', text) or (text.endswith('?') and len(text.split()) <= 15))
    complex_terms = re.search(r'\b(?:and|und|compare|vergleiche|research|recherchiere|implement|entwickle|diagnos\w*|legal|medical|medizin\w*)\b', text)
    if small and not complex_terms:
        return dict(mode='direct', budget=1, advisers=0, reason='One focused question: the Main answers directly')
    return dict(mode='planned', budget=max(1, capacity), advisers=min(3, capacity), reason='Planner selects useful independent work within available capacity')


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
