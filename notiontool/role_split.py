"""Split task roles over the free coworkers (pure module, NOT wired yet).

Field names follow notiontool/autopilot.py and notiontool/role_policy.py:
a worker is free when purpose == 'worker', enabled is truthy and
state == 'ready'. The Main never counts (role_policy.is_main: kind or
purpose 'main').

Hard rule: with 2 or more free workers, the worker that holds 'executor'
(the builder) never also holds a checker role ('tester' or 'reviewer').
Checker roles found on the builder move to a neighbouring worker; every
worker's role list keeps the order of the input roles.

No I/O, no network, stdlib and project imports only.
"""
import re

from notiontool import role_policy

EXECUTOR = role_policy.EXECUTOR
REVIEWER = role_policy.REVIEWER
TESTER = 'tester'
CHECKER_ROLES = (TESTER, REVIEWER)
HEADING_PREFIX = '## As '

_HEADING_RE = re.compile(r'^##\s+As\s+(.+?):\s*$')
_ANY_HEADING_RE = re.compile(r'^#{1,2}\s')

__all__ = [
    'CHECKER_ROLES', 'EXECUTOR', 'HEADING_PREFIX', 'REVIEWER', 'TESTER', 'free_coworkers',
    'missing_roles', 'parse_role_sections', 'role_sections_prompt', 'split_roles',
]


def _unique(values):
    """Non-empty values in input order, each once."""
    out = []
    for value in values or ():
        if value in (None, '') or value in out:
            continue
        out.append(value)
    return out


def free_coworkers(workers):
    """Ids of free coworkers in input order.

    Free means: enabled, purpose 'worker', state 'ready' and not the Main.
    Entries that are not dicts or have no id are ignored; ids appear once.
    """
    ids = []
    for worker in workers or ():
        if not isinstance(worker, dict):
            continue
        if worker.get('purpose') != 'worker' or not worker.get('enabled') or worker.get('state') != 'ready':
            continue
        if role_policy.is_main(worker):
            continue
        ident = worker.get('id')
        if ident in (None, '') or ident in ids:
            continue
        ids.append(ident)
    return ids


def _even_chunks(roles, count):
    """Split roles in order into count lists; sizes differ by at most one.

    With more lists than roles the extra lists stay empty.
    """
    groups = [[] for _ in range(count)]
    used = min(count, len(roles))
    if not used:
        return groups
    base, extra = divmod(len(roles), used)
    start = 0
    for index in range(used):
        size = base + (1 if index < extra else 0)
        groups[index] = list(roles[start:start + size])
        start += size
    return groups


def _separate_builder(groups, roles):
    """Move checker roles away from the builder; keep the role order per worker."""
    order = {role: index for index, role in enumerate(roles)}
    builder = next((index for index, group in enumerate(groups) if EXECUTOR in group), None)
    if builder is None:
        return
    checkers = [role for role in groups[builder] if role in CHECKER_ROLES]
    if not checkers:
        return
    candidates = [builder + 1, builder - 1] + list(range(len(groups)))
    target = next(index for index in candidates
                  if 0 <= index < len(groups) and index != builder and groups[index])
    groups[builder] = [role for role in groups[builder] if role not in CHECKER_ROLES]
    groups[target] = sorted(groups[target] + checkers, key=order.get)
    # Rebalance: hand non-checker roles back to the builder while the target is clearly bigger.
    while len(groups[target]) - len(groups[builder]) > 1:
        movable = [role for role in groups[target] if role not in CHECKER_ROLES]
        if not movable:
            break
        role = movable[0] if target > builder else movable[-1]
        groups[target].remove(role)
        groups[builder] = sorted(groups[builder] + [role], key=order.get)


def split_roles(roles, worker_ids):
    """Return [{'worker_id', 'roles'}] for the given free workers, in worker order.

    One worker gets all roles. More workers: roles are spread in order as
    evenly as possible, then the builder rule is applied. Workers beyond the
    number of roles get an empty list. Duplicate roles and ids are used once.
    An empty worker list raises ValueError. Inputs are not changed.
    """
    ids = _unique(worker_ids)
    if not ids:
        raise ValueError('At least one free worker is needed to split roles.')
    roles = _unique(roles)
    if len(ids) == 1:
        return [dict(worker_id=ids[0], roles=list(roles))]
    groups = _even_chunks(roles, len(ids))
    _separate_builder(groups, roles)
    return [dict(worker_id=ident, roles=group) for ident, group in zip(ids, groups)]


def role_sections_prompt(roles):
    """English prompt block asking for one visible '## As <role>:' section per role."""
    roles = _unique(roles)
    if not roles:
        return ''
    lines = [
        'Role sections (required):',
        'Write one visible section per role in your answer, in this order. '
        'Start each section with exactly this heading line:',
    ]
    lines += [f'{HEADING_PREFIX}{role}:' for role in roles]
    lines.append("In each section give concrete findings, or write 'nothing found' and say why.")
    lines.append('Write these sections in the visible answer, never only in hidden thinking.')
    if EXECUTOR in roles:
        lines.append('For the executor role: prove every file you wrote with Test-Path, '
                     'the line count and the exact test output.')
    return '\n'.join(lines) + '\n'


def parse_role_sections(text, roles):
    """Return {role: section text or None} in the order of roles.

    A section starts at a line that is exactly '## As <role>:' (surrounding
    spaces allowed) and ends at the next '#' or '##' heading. Deeper headings
    ('###') stay inside the section. The first section of a role wins.
    """
    result = {role: None for role in _unique(roles)}
    current, buffer = None, []

    def finish():
        if current is not None:
            result[current] = '\n'.join(buffer).strip()

    for line in (text or '').splitlines():
        stripped = line.strip()
        match = _HEADING_RE.match(stripped)
        if match or _ANY_HEADING_RE.match(stripped):
            finish()
            current, buffer = None, []
            if match:
                role = match.group(1).strip()
                if role in result and result[role] is None:
                    current = role
            continue
        if current is not None:
            buffer.append(line)
    finish()
    return result


def missing_roles(text, roles):
    """Roles, in order, whose section is missing or empty."""
    parsed = parse_role_sections(text, roles)
    return [role for role, section in parsed.items() if not (section or '').strip()]
