"""Coworker chat names 'Cowork 1', 'Cowork 2', ... (pure module: no I/O, no network).

- assign_numbers(workers, saved) gives every coworker slot (worker id) a stable
  number. A new slot gets the lowest free number. A replacement chat of the same
  slot keeps the number, because the number belongs to the worker id, not to the
  thread. Numbers of disabled or retired slots stay reserved until the slot is
  removed from the worker list.
- title_for(n) -> 'Cowork N'.
- is_pool_title(title) -> True for titles that belong to the harness pool.
- rename_plan(workers, chats, saved) -> [(thread_id, new_title)] for joined, idle
  coworkers. Chats the user renamed (data.user_set_title), Main chats and the
  user's own chats are never in the plan.

New chats are still created as 'Welcome to Notion' and only renamed after they
joined.
"""
import re

WELCOME_TITLE = 'Welcome to Notion'
LEGACY_TITLE = 'Cowork joined'
RETIRED_TITLE = 'Cowork (retired)'
POOL_TITLES = (WELCOME_TITLE, LEGACY_TITLE, RETIRED_TITLE)
PROTECTED_TITLES = ('NotionTool', 'Sonstiges', 'Studio432', 'Roblox')
MAIN_PREFIX = 'Main'
NUMBERED_RE = re.compile(r'^Cowork (\d+)$')
BUSY_STATES = ('busy', 'recovering')
RETIRED_STATES = ('retired', 'missing')

__all__ = [
    'LEGACY_TITLE', 'POOL_TITLES', 'PROTECTED_TITLES', 'RETIRED_TITLE', 'WELCOME_TITLE', 'assign_numbers',
    'chat_title', 'is_numbered_title', 'is_pool_title', 'is_protected_title', 'is_unnamed_welcome',
    'rename_plan', 'title_for',
]


def _valid_number(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def title_for(n):
    """'Cowork N' for a positive int n; anything else raises ValueError."""
    if not _valid_number(n):
        raise ValueError('Cowork number must be a positive int: %r' % (n,))
    return 'Cowork %d' % n


def is_numbered_title(title):
    """True for 'Cowork N' with N >= 1."""
    match = NUMBERED_RE.match(title) if isinstance(title, str) else None
    return bool(match) and int(match.group(1)) >= 1


def is_pool_title(title):
    """True for 'Welcome to Notion', 'Cowork joined', 'Cowork (retired)' and r'^Cowork \\d+$'."""
    if not isinstance(title, str):
        return False
    return title in POOL_TITLES or bool(NUMBERED_RE.match(title))


def is_unnamed_welcome(title):
    """True for a pool chat that has not got its Cowork number yet (Welcome or legacy marker).

    Also accepts any title containing 'welcome', as the old main_handoff check did.
    Numbered and retired Cowork chats are excluded.
    """
    if not isinstance(title, str) or is_numbered_title(title) or title == RETIRED_TITLE:
        return False
    return is_pool_title(title) or 'welcome' in title.lower()


def is_protected_title(title):
    """The user's own chats (NotionTool, Sonstiges, Studio432, Roblox) and every Main* chat."""
    text = str(title or '').strip()
    return text in PROTECTED_TITLES or text.startswith(MAIN_PREFIX)


def _is_slot(worker):
    return isinstance(worker, dict) and worker.get('id') not in (None, '') and worker.get('purpose', 'worker') == 'worker'


def assign_numbers(workers, saved):
    """Return {worker_id: number} for every coworker slot in workers.

    saved is the mapping returned last time (may be None). Saved numbers of
    slots that still exist are kept, also for disabled or retired slots. Slots
    no longer in workers lose their number. New slots get the lowest free
    number in input order. Invalid or duplicate saved numbers are reassigned.
    """
    saved = saved if isinstance(saved, dict) else {}
    slots = []
    for worker in workers or ():
        if _is_slot(worker) and worker['id'] not in slots:
            slots.append(worker['id'])
    numbers, used = {}, set()
    for ident in slots:
        number = saved.get(ident)
        if _valid_number(number) and number not in used:
            numbers[ident] = number
            used.add(number)
    candidate = 1
    for ident in slots:
        if ident in numbers:
            continue
        while candidate in used:
            candidate += 1
        numbers[ident] = candidate
        used.add(candidate)
    return numbers


def _chat_index(chats):
    """Accept {thread_id: record} or [record with 'id'] and return {thread_id: record}."""
    if isinstance(chats, dict):
        return {k: v for k, v in chats.items() if isinstance(v, dict)}
    index = {}
    for chat in chats or ():
        if isinstance(chat, dict) and chat.get('id'):
            index[chat['id']] = chat
    return index


def chat_title(record, fallback=''):
    """Title of a chat record ('title' or data.title), else fallback."""
    record = record or {}
    title = record.get('title')
    if not isinstance(title, str):
        title = (record.get('data') or {}).get('title')
    return title if isinstance(title, str) else (fallback or '')


def _user_titled(record):
    return bool(((record or {}).get('data') or {}).get('user_set_title') or (record or {}).get('user_set_title'))


def rename_plan(workers, chats, saved):
    """Return [(thread_id, 'Cowork N')] for joined, enabled, idle coworkers whose title is not yet right.

    Skipped: Main workers, retired or missing slots, busy chats, chats with
    user_set_title, protected or Main* titles, titles outside the pool and
    'Cowork (retired)'. The plan is sorted by number.
    """
    numbers = assign_numbers(workers, saved)
    index = _chat_index(chats)
    plan, seen = [], set()
    for worker in workers or ():
        if not _is_slot(worker) or worker['id'] not in numbers:
            continue
        thread_id = worker.get('thread_id')
        if not thread_id or thread_id in seen:
            continue
        if worker.get('state') in RETIRED_STATES or worker.get('retired'):
            continue
        if not worker.get('enabled') or worker.get('join_state') != 'joined':
            continue
        if worker.get('state') in BUSY_STATES or worker.get('lease'):
            continue
        record = index.get(thread_id)
        title = chat_title(record, worker.get('label') or '')
        if _user_titled(record) or is_protected_title(title):
            continue
        if not is_pool_title(title) or title == RETIRED_TITLE:
            continue
        target = title_for(numbers[worker['id']])
        if title != target:
            plan.append((numbers[worker['id']], thread_id, target))
            seen.add(thread_id)
    return [(thread_id, target) for _, thread_id, target in sorted(plan)]
