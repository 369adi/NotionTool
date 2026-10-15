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
