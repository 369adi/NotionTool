"""Coworker chat names 'Coworker 1', 'Coworker 2', ... (rule I41; pure module: no I/O, no network).

- assign_numbers(workers, saved, ledger=None) gives every coworker slot a stable
  number. With a ledger (account['team_threads']) the worker whose thread is in
  role workerK gets K, so a replacement chat of the same slot (age rotation,
  context switch) keeps the number. Other slots keep their saved number; new
  slots get the lowest free number.
- title_for(n) -> 'Coworker N'.
- is_pool_title(title) -> True for titles that belong to the harness pool,
  including the legacy names 'Cowork N', 'Cowork joined' and 'Cowork (retired)'.
- rename_plan(workers, chats, saved, ledger=None, now=None) -> [(thread_id, new_title)]
  for joined, idle coworkers. Legacy 'Cowork N' titles are migrated to
  'Coworker N'. Chats the user renamed (user_set_title, unless the title is the
  one the harness set), Main chats, private chats and protected chats are never
  in the plan.

New chats are still created as 'Welcome to Notion' and only renamed after they
joined.
"""
import re

WELCOME_TITLE = 'Welcome to Notion'
TITLE_PREFIX = 'Coworker'
LEGACY_TITLE = 'Cowork joined'
RETIRED_TITLE = 'Coworker (retired)'
LEGACY_RETIRED_TITLE = 'Cowork (retired)'
RETIRED_TITLES = (RETIRED_TITLE, LEGACY_RETIRED_TITLE)
LEGACY_TITLES = (LEGACY_TITLE, LEGACY_RETIRED_TITLE)
POOL_TITLES = (WELCOME_TITLE, LEGACY_TITLE, RETIRED_TITLE, LEGACY_RETIRED_TITLE)
PROTECTED_TITLES = ('NotionTool', 'Sonstiges', 'Studio432', 'Roblox')
MAIN_PREFIX = 'Main'
MAIN_RE = re.compile(r'^Main(?: |$|\d)')  # 'Main', 'Main Chat', 'Main 2', 'Main (old, Archive ...)'; not 'Maintenance'
NUMBERED_RE = re.compile(r'^Coworker (\d+)$')
LEGACY_NUMBERED_RE = re.compile(r'^Cowork (\d+)$')
SLOT_RE = re.compile(r'^worker(\d+)$')
BUSY_STATES = ('busy', 'recovering')
RETIRED_STATES = ('retired', 'missing')

__all__ = [
    'LEGACY_RETIRED_TITLE', 'LEGACY_TITLE', 'LEGACY_TITLES', 'POOL_TITLES', 'PROTECTED_TITLES', 'RETIRED_TITLE',
    'RETIRED_TITLES', 'TITLE_PREFIX', 'WELCOME_TITLE', 'assign_numbers', 'chat_title', 'harness_titled',
    'is_legacy_numbered', 'is_numbered_title', 'is_pool_title', 'is_protected_title', 'is_retired_title',
    'is_unnamed_welcome', 'one_per_thread', 'rename_plan', 'slot_number', 'title_for', 'user_renamed',
]


def _valid_number(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def title_for(n):
    """'Coworker N' for a positive int n; anything else raises ValueError."""
    if not _valid_number(n):
        raise ValueError('Coworker number must be a positive int: %r' % (n,))
    return '%s %d' % (TITLE_PREFIX, n)


def _numbered(regex, title):
    match = regex.match(title) if isinstance(title, str) else None
    return bool(match) and int(match.group(1)) >= 1


def is_numbered_title(title):
    """True for 'Coworker N' with N >= 1 (the current name)."""
    return _numbered(NUMBERED_RE, title)


def is_legacy_numbered(title):
    """True for the old name 'Cowork N' with N >= 1."""
    return _numbered(LEGACY_NUMBERED_RE, title)


def is_retired_title(title):
    return title in RETIRED_TITLES


def is_pool_title(title):
    """True for Welcome, the legacy markers, both retired titles, 'Coworker N' and 'Cowork N'."""
    if not isinstance(title, str):
        return False
    return title in POOL_TITLES or bool(NUMBERED_RE.match(title)) or bool(LEGACY_NUMBERED_RE.match(title))


def is_unnamed_welcome(title):
    """True for a pool chat that has not got its number yet (Welcome or legacy marker).

    Also accepts any title containing 'welcome', as the old main_handoff check did.
    Numbered (new or legacy) and retired chats are excluded.
    """
    if not isinstance(title, str) or is_numbered_title(title) or is_legacy_numbered(title) or is_retired_title(title):
        return False
    return is_pool_title(title) or 'welcome' in title.lower()


def is_protected_title(title):
    """The user's own chats (NotionTool, Sonstiges, Studio432, Roblox) and Main chats.

    Main chats are 'Main', 'Main Chat' and titles matching ^Main( |$|digit), e.g. 'Main 2' or
    'Main (old, Archive 1)'. Other titles that merely start with 'Main' ('Maintenance') are not protected.
    """
    text = str(title or '').strip()
    return text in PROTECTED_TITLES or bool(MAIN_RE.match(text))


def slot_number(role):
    """'worker7' -> 7; anything else -> None."""
    match = SLOT_RE.match(role) if isinstance(role, str) else None
    number = int(match.group(1)) if match else 0
    return number if number >= 1 else None


def _is_slot(worker):
    return isinstance(worker, dict) and worker.get('id') not in (None, '') and worker.get('purpose', 'worker') == 'worker'


def _ledger_numbers(ledger):
    """{thread_id: K} for every ledger role workerK with a thread id."""
    out = {}
    for role, item in (ledger or {}).items() if isinstance(ledger, dict) else ():
        number = slot_number(role)
        tid = item.get('id') if isinstance(item, dict) else None
        if number and tid:
            out.setdefault(str(tid), number)
            out.setdefault(str(tid).replace('-', ''), number)
    return out


def _role_number(worker, by_thread):
    tid = str(worker.get('thread_id') or '')
    return by_thread.get(tid) or by_thread.get(tid.replace('-', ''))


def _live_rank(worker):
    """Higher wins when two records point at the same chat."""
    retired = bool(worker.get('retired')) or worker.get('state') in RETIRED_STATES
    working = bool(worker.get('enabled')) and worker.get('join_state') == 'joined'
    return (not retired, working, bool(worker.get('pool_admitted')))


def one_per_thread(workers):
    """Keep one worker record per chat, in the original order.

    A Coworker context switch moves its worker record to a new Welcome chat that refresh or the
    rotation may already have registered as a record of its own. Two records for one chat got two
    numbers, so the chat was renamed twice per pass ('Coworker 4', then 'Coworker 9') and the
    working record kept the label 'Welcome to Notion'. The kept record is the live one: not retired,
    then enabled and joined, then pool-admitted, then the first one.
    """
    kept, order = {}, []
    for worker in workers or ():
        tid = str(worker.get('thread_id') or '').replace('-', '') if isinstance(worker, dict) else ''
        if not tid:
            order.append(worker)
        elif tid not in kept:
            order.append(tid)
            kept[tid] = worker
        elif _live_rank(worker) > _live_rank(kept[tid]):
            kept[tid] = worker
    return [kept[item] if isinstance(item, str) else item for item in order]


def assign_numbers(workers, saved, ledger=None):
    """Return {worker_id: number} for every coworker slot in workers.

    1. With a ledger, the worker whose thread is the id of role workerK gets K.
    2. Other slots keep a valid saved number that no role number uses.
    3. Without a saved number, an existing 'Coworker N' / legacy 'Cowork N' title keeps N (migration).
    4. Everyone else gets the lowest free number (role numbers stay reserved).
    saved may be None. Slots no longer in workers lose their number. With
    ledger=None the behaviour is the old one.
    """
    saved = saved if isinstance(saved, dict) else {}
    by_thread = _ledger_numbers(ledger)
    reserved = set(by_thread.values())
    slots = []
    for worker in one_per_thread(workers):
        if is_protected_title(worker.get('label') if isinstance(worker, dict) else '') and not _role_number(worker, by_thread):
            continue  # the user's own chats (NotionTool, Roblox, Main*) never take a Coworker number
        if _is_slot(worker) and worker['id'] not in [s['id'] for s in slots]:
            slots.append(worker)
    numbers, used = {}, set(reserved)
    for worker in slots:
        number = _role_number(worker, by_thread)
        if number and number not in numbers.values():
            numbers[worker['id']] = number
    for worker in slots:
        if worker['id'] in numbers:
            continue
        number = saved.get(worker['id'])
        if _valid_number(number) and number not in used:
            numbers[worker['id']] = number
            used.add(number)
    for worker in slots:
        if worker['id'] in numbers:
            continue
        label = str(worker.get('label') or '')
        match = NUMBERED_RE.match(label) or LEGACY_NUMBERED_RE.match(label)
        number = int(match.group(1)) if match else 0
        if number >= 1 and number not in used and not _valid_number(saved.get(worker['id'])):
            numbers[worker['id']] = number
            used.add(number)
    candidate = 1
    for worker in slots:
        if worker['id'] in numbers:
            continue
        while candidate in used:
            candidate += 1
        numbers[worker['id']] = candidate
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


def _user_set(record):
    return bool(((record or {}).get('data') or {}).get('user_set_title') or (record or {}).get('user_set_title'))


def harness_titled(worker, record):
    """True when the live title is the title the harness itself set last."""
    own = (worker or {}).get('harness_title')
    return bool(own) and chat_title(record, (worker or {}).get('label') or '') == own


def user_renamed(worker, record):
    """user_set_title is set and the title is not the harness's own title."""
    return _user_set(record) and not harness_titled(worker, record)


def _managed(worker, by_thread):
    return bool(worker.get('pool_admitted') or _role_number(worker, by_thread))


def rename_plan(workers, chats, saved, ledger=None, now=None):
    """Return [(thread_id, 'Coworker N')] for joined, enabled, idle coworkers whose title is not yet right.

    Skipped: Main/private/operator workers (purpose other than 'worker'), retired
    or missing slots, busy chats, user-renamed chats, protected or Main titles,
    retired titles and chats whose rename_retry_at is later than now. Unmanaged
    slots also need a pool title; managed slots (ledger role or pool_admitted)
    are renamed whatever their title. Legacy 'Cowork N' becomes 'Coworker N'.
    The plan is sorted by number.
    """
    numbers = assign_numbers(workers, saved, ledger)
    by_thread = _ledger_numbers(ledger)
    index = _chat_index(chats)
    plan, seen = [], set()
    for worker in one_per_thread(workers):
        if not _is_slot(worker) or worker['id'] not in numbers:
            continue
        thread_id = worker.get('thread_id')
        if not thread_id or thread_id in seen:
            continue
        if worker.get('state') in RETIRED_STATES or worker.get('retired'):
            continue
        if not (worker.get('enabled') and worker.get('join_state') == 'joined'):
            # Managed Welcome Coworkers still waiting for their join are named as well.
            if not (worker.get('join_state') == 'pending' and _managed(worker, by_thread)):
                continue
        if worker.get('state') in BUSY_STATES or worker.get('lease'):
            continue
        retry_at = worker.get('rename_retry_at') or 0
        if now is not None and isinstance(retry_at, (int, float)) and retry_at > now:
            continue
        record = index.get(thread_id)
        title = chat_title(record, worker.get('label') or '')
        if user_renamed(worker, record) or is_protected_title(title) or is_retired_title(title):
            continue
        if not is_pool_title(title) and not _managed(worker, by_thread):
            continue
        target = title_for(numbers[worker['id']])
        if title != target:
            plan.append((numbers[worker['id']], thread_id, target))
            seen.add(thread_id)
    return [(thread_id, target) for _, thread_id, target in sorted(plan)]
