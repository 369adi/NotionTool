"""Architect-owned explicit model choices, scoped to each assignment."""
from .protocol import ContractError

MODELS = {'opus': 'Opus 5.5', 'sonnet': 'Sonnet 5.5', 'haiku': 'Haiku 5.5'}

def choice(value):
    text = str(value).strip().lower()
    for key, label in MODELS.items():
        if text in (key, label.lower()):
            return key
    raise ContractError('Choose explicitly: Haiku 5.5, Sonnet 5.5 or Opus 5.5. Auto is forbidden.')

def validate_models(plan):
    tasks = plan['tasks']
    for task in tasks:
        task['model'] = choice(task.get('model', ''))
        if not str(task.get('model_reason', '')).strip():
            raise ContractError('Every task needs a reason for the Architect model choice')
        if task.get('use_main') and (len(tasks) != 1 or task['model'] != 'opus' or task.get('depends_on')):
            raise ContractError('Working alone requires exactly one independent Opus task on the main chat')
    return tasks

# Engine roles that always run on the default model; executors without an Architect choice fall back to it.
DEFAULT_MODEL = 'opus'
ROLE_SCOPE = {'planner': 'planning', 'reviewer': 'review', 'integrator': 'integration', 'executor': 'execution'}

def routing_reason(role, model=DEFAULT_MODEL, task_class='', risk='', fallback=False):
    """Short reason naming the role, the task class or risk, and default versus fallback."""
    role = str(role or '').strip().lower() or 'executor'
    scope = str(task_class or '').strip()
    if not scope:
        scope = f'risk {str(risk).strip()}' if str(risk or '').strip() else ROLE_SCOPE.get(role, 'general')
    source = 'fallback' if fallback else 'default'
    return f'{role} ({scope}): {MODELS[choice(model)]} is the {source} model'

def with_reason(task):
    """Fill an empty model_reason in place. Never changes or adds task['model']."""
    if not str(task.get('model_reason', '')).strip():
        role = str(task.get('role', '') or 'executor').strip().lower()
        task['model_reason'] = routing_reason(role, task.get('model') or DEFAULT_MODEL,
                                              task.get('task_class', ''), task.get('risk', ''),
                                              fallback=role not in ('planner', 'reviewer', 'integrator'))
    return task
