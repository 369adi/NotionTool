"""Executor model routing: rule table task_class x risk plus hysteresis.

Pure functions only: no I/O, no network, no global state changes.
Model IDs are the keys of model_routing.MODELS (opus, sonnet, haiku).
Reasons are built with model_routing.routing_reason(); its logic is not copied.

State passed between decisions (a plain dict, never mutated in place):
    model    current model key or None
    pending  model the rule has asked for but not yet switched to, or None
    streak   consecutive decisions in which the rule asked for `pending`
    failed   model keys that failed for this task, in order
"""
from .model_routing import MODELS, choice, routing_reason

ROLE = 'executor'
RISKS = ('low', 'medium', 'high')
# Documented defaults for unknown or empty input.
DEFAULT_TASK_CLASS = 'code'
DEFAULT_RISK = 'medium'
# Rule must ask for a different model this many decisions in a row before a switch.
SWITCH_AFTER = 2

# Ordered fallback chain per primary model. Every chain covers all other models.
FALLBACKS = {
    'opus': ('sonnet', 'haiku'),
    'sonnet': ('opus', 'haiku'),
    'haiku': ('sonnet', 'opus'),
}

# task_class -> (low, medium, high)
_TABLE = {
    'code': ('sonnet', 'sonnet', 'opus'),
    'test': ('haiku', 'sonnet', 'opus'),
    'docs': ('haiku', 'sonnet', 'opus'),
    'research': ('sonnet', 'sonnet', 'opus'),
    'review': ('opus', 'opus', 'opus'),
    'routine': ('haiku', 'haiku', 'sonnet'),
}

TASK_CLASSES = tuple(_TABLE)


def _rule_reason(model, task_class, risk, fallback=False):
    return f'{routing_reason(ROLE, model, task_class, risk, fallback=fallback)}; risk {risk}'


def _build_rules():
    rules = {}
    for task_class, models in _TABLE.items():
        for risk, model in zip(RISKS, models):
            model = choice(model)
            rules[(task_class, risk)] = {
                'model': model,
                'fallback': list(FALLBACKS[model]),
                'model_reason': _rule_reason(model, task_class, risk),
            }
    return rules


RULES = _build_rules()


def allowed_models():
    """Model keys the router may return (all from model_routing.MODELS)."""
    return tuple(MODELS)


def normalize(task_class, risk):
    """Return (task_class, risk, notes). Unknown values fall back to the defaults."""
    notes = []
    cls = str(task_class or '').strip().lower()
    rk = str(risk or '').strip().lower()
    if cls not in _TABLE:
        notes.append(f"unknown task_class '{cls}', using default '{DEFAULT_TASK_CLASS}'")
        cls = DEFAULT_TASK_CLASS
    if rk not in RISKS:
        notes.append(f"unknown risk '{rk}', using default '{DEFAULT_RISK}'")
        rk = DEFAULT_RISK
    return cls, rk, notes


def rule_for(task_class, risk):
    """Rule entry for the input (after defaults); a fresh copy."""
    cls, rk, notes = normalize(task_class, risk)
    rule = RULES[(cls, rk)]
    reason = '; '.join([rule['model_reason']] + notes)
    return {'model': rule['model'], 'fallback': list(rule['fallback']), 'model_reason': reason,
            'task_class': cls, 'risk': rk}


def initial_state():
    return {'model': None, 'pending': None, 'streak': 0, 'failed': []}


def _copy_state(state):
    base = initial_state()
    if state:
        base.update({k: state.get(k, base[k]) for k in base})
    base['failed'] = [choice(m) for m in (base['failed'] or [])]
    base['model'] = choice(base['model']) if base['model'] else None
    base['pending'] = choice(base['pending']) if base['pending'] else None
    base['streak'] = int(base['streak'] or 0)
    return base


def _chain(primary):
    return [primary] + list(FALLBACKS[primary])


def _result(model, rule, state, reason, notes, exhausted=False):
    failed = state['failed']
    fallback = [m for m in _chain(rule['model']) if m != model and m not in failed] if model else []
    return {'model': model, 'fallback': fallback, 'model_reason': '; '.join([reason] + notes),
            'state': state, 'exhausted': exhausted}


def decide(task_class, risk, state=None, failed=False):
    """One routing decision.

    failed=True means the current model failed: switch at once to the next
    model in the fallback chain that has not failed yet. Without a failure the
    current model is kept unless the rule asks for the same different model in
    SWITCH_AFTER consecutive decisions. Returns a dict with model, fallback,
    model_reason, state (new dict) and exhausted.
    """
    cls, rk, notes = normalize(task_class, risk)
    rule = RULES[(cls, rk)]
    st = _copy_state(state)
    current = st['model']

    if failed:
        if current and current not in st['failed']:
            st['failed'].append(current)
        candidates = [m for m in _chain(rule['model']) if m not in st['failed']]
        st['pending'], st['streak'] = None, 0
        if not candidates:
            st['model'] = None
            reason = (f'{ROLE} ({cls}): fallback chain exhausted after failures of '
                      f"{', '.join(MODELS[m] for m in st['failed'])}; risk {rk}")
            return _result(None, rule, st, reason, notes, exhausted=True)
        nxt = candidates[0]
        st['model'] = nxt
        was = MODELS[current] if current else 'no model'
        reason = f'{_rule_reason(nxt, cls, rk, fallback=nxt != rule["model"])}; switched from {was} after failure'
        return _result(nxt, rule, st, reason, notes)

    if current is None:
        target = rule['model']
        if target in st['failed']:
            alive = [m for m in _chain(target) if m not in st['failed']]
            if not alive:
                reason = f'{ROLE} ({cls}): fallback chain exhausted; risk {rk}'
                return _result(None, rule, st, reason, notes, exhausted=True)
            target = alive[0]
        st['model'], st['pending'], st['streak'] = target, None, 0
        reason = _rule_reason(target, cls, rk, fallback=target != rule['model'])
        return _result(target, rule, st, reason, notes)

    wanted = rule['model']
    if wanted == current or wanted in st['failed']:
        st['pending'], st['streak'] = None, 0
        reason = _rule_reason(current, cls, rk, fallback=current != wanted)
        if wanted != current:
            reason += f'; rule model {MODELS[wanted]} failed earlier, kept {MODELS[current]}'
        return _result(current, rule, st, reason, notes)

    if st['pending'] == wanted:
        st['streak'] += 1
    else:
        st['pending'], st['streak'] = wanted, 1

    if st['streak'] >= SWITCH_AFTER:
        st['model'], st['pending'], st['streak'] = wanted, None, 0
        reason = (f'{_rule_reason(wanted, cls, rk)}; switched from {MODELS[current]} because the rule '
                  f'asked for {MODELS[wanted]} in {SWITCH_AFTER} consecutive decisions')
        return _result(wanted, rule, st, reason, notes)

    reason = (f'{_rule_reason(current, cls, rk, fallback=True)}; kept by hysteresis, rule asks for '
              f"{MODELS[wanted]} ({st['streak']} of {SWITCH_AFTER})")
    return _result(current, rule, st, reason, notes)
