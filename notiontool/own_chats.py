"""User-owned chats ('Create own chat'), pure module: no I/O, no network.

An own chat is created like every other chat (Welcome to Notion first), then
renamed to the name the user typed. It belongs to the user: it is stored with
purpose 'user' and enabled False, so the worker pool must never pick it as a
Coworker or Main (see is_user_chat).

- validate_name(name, existing_titles) -> cleaned name, or ValueError with a clear message.
- plan_create(account_id, name, request_id) -> operation dict, identical for the same request_id.
- is_user_chat(worker_or_chat) -> True for records with purpose 'user' or owner 'user'.
- next_cowork_title(workers, saved) -> 'Cowork N' for the next new coworker slot; uses
  cowork_names when it can be imported, otherwise its own lowest-free-number logic.
"""
import importlib
import re

MAX_NAME_CHARS = 80
RESERVED_TITLES = ('Welcome to Notion', 'Cowork joined', 'Cowork (retired)')
MAIN_PREFIX = 'Main'
NUMBERED_RE = re.compile(r'^Cowork \d+$')
CONTROL_RE = re.compile('[\x00-\x1f\x7f-\x9f]')
OP_KIND = 'create_own_chat'
USER_PURPOSE = 'user'
STEPS = ('create_welcome', 'rename_thread')

MSG_TYPE = 'Chat name must be text.'
MSG_EMPTY = 'Chat name must not be empty.'
MSG_LONG = 'Chat name must be 80 characters or fewer.'
MSG_CONTROL = 'Chat name must not contain control characters (such as tabs or line breaks).'
MSG_MAIN = "Chat name must not start with 'Main'; Main chats are reserved for NotionTool."
MSG_COWORK = "Chat name must not look like 'Cowork N'; those names are reserved for coworker chats."
MSG_RESERVED = "'%s' is reserved for NotionTool worker chats; choose another name."
MSG_DUPLICATE = "A chat named '%s' already exists; choose another name."
MSG_ACCOUNT = 'account_id is required.'
MSG_REQUEST = 'request_id is required.'

__all__ = [
    'MAX_NAME_CHARS', 'RESERVED_TITLES', 'is_user_chat', 'next_cowork_title', 'plan_create', 'validate_name',
]


def validate_name(name, existing_titles=()):
    """Return the trimmed name or raise ValueError with a message the UI can show as is."""
    if not isinstance(name, str):
        raise ValueError(MSG_TYPE)
    cleaned = name.strip()
    if not cleaned:
        raise ValueError(MSG_EMPTY)
    if len(cleaned) > MAX_NAME_CHARS:
        raise ValueError(MSG_LONG)
    if CONTROL_RE.search(cleaned):
        raise ValueError(MSG_CONTROL)
    if cleaned.startswith(MAIN_PREFIX):
        raise ValueError(MSG_MAIN)
    if NUMBERED_RE.match(cleaned):
        raise ValueError(MSG_COWORK)
    if cleaned.casefold() in {t.casefold() for t in RESERVED_TITLES}:
        raise ValueError(MSG_RESERVED % cleaned)
    existing = {t.strip().casefold() for t in (existing_titles or ()) if isinstance(t, str)}
    if cleaned.casefold() in existing:
        raise ValueError(MSG_DUPLICATE % cleaned)
    return cleaned


def plan_create(account_id, name, request_id):
    """Operation dict for 'Create own chat'. The same inputs always give an identical dict.

    The name is checked with validate_name (without existing titles; the caller
    checks duplicates against the account's current chats first).
    """
    account_id = str(account_id or '').strip()
    request_id = str(request_id or '').strip()
    if not account_id:
        raise ValueError(MSG_ACCOUNT)
    if not request_id:
        raise ValueError(MSG_REQUEST)
    cleaned = validate_name(name)
    return {
        'op': OP_KIND,
        'id': 'own:' + request_id,
        'account_id': account_id,
        'name': cleaned,
        'request_id': request_id,
        'purpose': USER_PURPOSE,
        'owner': USER_PURPOSE,
        'enabled': False,
        'steps': [
            {'step': 'create_welcome'},
            {'step': 'rename_thread', 'title': cleaned},
        ],
    }


def is_user_chat(worker_or_chat):
    """True if a worker or chat record (or its 'data' dict) has purpose 'user' or owner 'user'."""
    if not isinstance(worker_or_chat, dict):
        return False
    data = worker_or_chat.get('data')
    for source in (worker_or_chat, data if isinstance(data, dict) else {}):
        if source.get('purpose') == USER_PURPOSE or source.get('owner') == USER_PURPOSE:
            return True
    return False


def _cowork_module():
    """cowork_names, imported lazily; None if it is not available."""
    name = (__package__ + '.cowork_names') if __package__ else 'cowork_names'
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _valid_number(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _fallback_numbers(workers, saved):
    """Same rule as cowork_names.assign_numbers: saved numbers stay, new slots get the lowest free one."""
    saved = saved if isinstance(saved, dict) else {}
    slots = []
    for worker in workers or ():
        if (isinstance(worker, dict) and worker.get('id') not in (None, '')
                and worker.get('purpose', 'worker') == 'worker' and worker['id'] not in slots):
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


def next_cowork_title(workers, saved):
    """'Cowork N' with the lowest number not used by any existing coworker slot in workers."""
    module = _cowork_module()
    if module is not None:
        numbers = module.assign_numbers(workers, saved)
    else:
        numbers = _fallback_numbers(workers, saved)
    used = set(numbers.values())
    number = 1
    while number in used:
        number += 1
    return module.title_for(number) if module is not None else 'Cowork %d' % number
