"""User-owned chats ('New chat' / 'Create own chat').

An own chat is created like every other chat (Welcome to Notion first), then
renamed to the name the user typed. The user's title wins: the worker gets
data.user_set_title=True, so pool naming, rotation and retirement never rename it.

Pure helpers (no I/O):
- validate_name(name, existing_titles) -> cleaned name, or ValueError with a clear message.
- plan_create(account_id, name, request_id) -> operation dict, identical for the same request_id.
- is_user_chat(worker_or_chat) -> True for records with purpose 'user'/'private' or owner 'user'.
- next_cowork_title(workers, saved) -> 'Cowork N' for the next new coworker slot.

App routes (desktop 'New chat' flow, called from server.py through handle_app_route):
- POST /api/app/chat/create  {name, main, request_id}
- POST /api/app/chat/promote {chat_id or thread_id, main}
- POST /api/app/chat/rename  {thread_id, name}

main=true chats are Mains (purpose 'main', a Main chat record, target of Main work).
main=false chats are private (purpose 'private', enabled False, no Main chat record):
the engine only picks purpose 'worker' as coworker and team_rotation only rotates
purpose 'worker', so a private chat never gets delegated work and is never rotated.
Creation reuses mains.Mains.request_create/run_create (reserved thread id, Welcome
creation sent at most once, confirmed from Notion's chat list).
"""
import importlib
import re
import time

MAX_NAME_CHARS = 80
RESERVED_TITLES = ('Welcome to Notion', 'Cowork joined', 'Cowork (retired)')
MAIN_PREFIX = 'Main'
NUMBERED_RE = re.compile(r'^Cowork \d+$')
CONTROL_RE = re.compile('[\x00-\x1f\x7f-\x9f]')
OP_KIND = 'create_own_chat'
USER_PURPOSE = 'user'
PRIVATE_PURPOSE = 'private'
MAIN_PURPOSE = 'main'
STEPS = ('create_welcome', 'rename_thread')

APP_LEDGER = 'app_chats'          # store kind: one entry per request_id, written before anything else
APP_PREFIX = 'appchat:'
CREATE_ROUTE = '/api/app/chat/create'
PROMOTE_ROUTE = '/api/app/chat/promote'
RENAME_ROUTE = '/api/app/chat/rename'
APP_ROUTES = (CREATE_ROUTE, PROMOTE_ROUTE, RENAME_ROUTE)
ACTIVE_TASK_STATES = ('queued', 'running', 'sending', 'uncertain', 'reviewing', 'cancelling', 'recovering')
PROTECTED_TITLES = ('NotionTool', 'Sonstiges', 'Studio432', 'Roblox')  # main_handoff_ops.PROTECTED_TITLES

MSG_TYPE = 'Chat name must be text.'
MSG_EMPTY = 'Chat name must not be empty.'
MSG_LONG = 'Chat name must be 80 characters or fewer.'
MSG_CONTROL = 'Chat name must not contain control characters (such as tabs or line breaks).'
MSG_MAIN = "Names starting with 'Main' are for Main chats. Tick 'Main' to create a Main chat."
MSG_COWORK = "Chat name must not look like 'Cowork N'; those names are reserved for coworker chats."
MSG_RESERVED = "'%s' is reserved for NotionTool worker chats; choose another name."
MSG_DUPLICATE = "A chat named '%s' already exists; choose another name."
MSG_ACCOUNT = 'account_id is required.'
MSG_REQUEST = 'request_id is required.'
MSG_NOT_FOUND = 'Chat not found.'
MSG_THREAD = 'thread_id is required.'
MSG_SWITCH = "Only chats created with 'New chat' can switch between private and Main."
MSG_BUSY = 'This Main still has an unfinished task; switch it to private when the task is done.'
MSG_RENAME = 'Only private chats and Main chats can be renamed here.'
MSG_PROTECTED = "'%s' is a named chat and is never renamed by NotionTool."
MSG_ROUTE = 'Unknown app chat route.'

__all__ = [
    'APP_ROUTES', 'MAX_NAME_CHARS', 'RESERVED_TITLES', 'create_chat', 'finish_create', 'handle_app_route',
    'is_private_chat', 'is_user_chat', 'next_cowork_title', 'plan_create', 'private_chats', 'promote_chat',
    'rename_chat', 'validate_name',
]


def validate_name(name, existing_titles=(), allow_main=False):
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
    if cleaned.startswith(MAIN_PREFIX) and not allow_main:  # the Main tick decides, not the name
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
    """True if a worker or chat record (or its 'data' dict) has purpose 'user'/'private' or owner 'user'."""
    if not isinstance(worker_or_chat, dict):
        return False
    data = worker_or_chat.get('data')
    for source in (worker_or_chat, data if isinstance(data, dict) else {}):
        if source.get('purpose') in (USER_PURPOSE, PRIVATE_PURPOSE) or source.get('owner') == USER_PURPOSE:
            return True
    return False


def is_private_chat(worker):
    return isinstance(worker, dict) and worker.get('purpose') == PRIVATE_PURPOSE


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
    """'Coworker N' with the lowest number not used by any existing coworker slot in workers."""
    module = _cowork_module()
    if module is not None:
        numbers = module.assign_numbers(workers, saved)
    else:
        numbers = _fallback_numbers(workers, saved)
    used = set(numbers.values())
    number = 1
    while number in used:
        number += 1
    return module.title_for(number) if module is not None else 'Coworker %d' % number


# ---- App routes: New chat (create / promote / rename) -------------------------------------

def _flag(value):
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _selected_account(store, body):
    account_id = str((body or {}).get('account_id') or '').strip()
    if account_id:
        return account_id
    try:
        from .harness_account import selected_account
        return str(selected_account(store) or '')
    except Exception:
        return ''


def _titles(store, account_id, skip=''):
    return [w.get('label') for w in store.all('workers') if w.get('account_id') == account_id
            and w.get('id') != skip and w.get('state') != 'missing'
            and w.get('purpose') in (PRIVATE_PURPOSE, MAIN_PURPOSE, USER_PURPOSE)]


def _refresh_if_duplicate(server, body):
    """A chat deleted in Notion stays in the store until the next chat-list refresh, so its name looked taken.

    Before refusing a name, read the account's Notion chat list once; deleted chats then become 'missing'
    and drop out of _titles. A failed refresh only logs and the normal duplicate check decides.
    """
    store = server.store
    name = body.get('name')
    account_id = _selected_account(store, body)
    if not account_id or not isinstance(name, str) or not name.strip():
        return False
    taken = {str(t or '').strip().casefold() for t in _titles(store, account_id)}
    if name.strip().casefold() not in taken:
        return False
    try:
        server.adapter.refresh(account_id)
    except Exception as exc:
        store.event('app_chat_name_refresh_skipped', message=str(exc)[:200], account_id=account_id)
    return True


def _worker_by_thread(store, thread_id, account_id=''):
    return next((w for w in store.all('workers') if w.get('thread_id') == thread_id
                 and (not account_id or w.get('account_id') == account_id)), None)


def _mark_user_title(worker, name):
    """The user's title wins: nothing in NotionTool renames this chat back."""
    data = dict(worker.get('data')) if isinstance(worker.get('data'), dict) else {}
    data['user_set_title'] = True
    worker.update(label=name, user_set_title=True, data=data, app_chat=True)


def _mains(server):
    mains = getattr(server, 'mains', None)
    if mains is None:
        from .mains import Mains
        mains = Mains(server)
    return mains


def _role(ledger_or_worker):
    record = ledger_or_worker or {}
    if 'main' in record and 'purpose' not in record:
        return MAIN_PURPOSE if record.get('main') else PRIVATE_PURPOSE
    return MAIN_PURPOSE if record.get('purpose') == MAIN_PURPOSE else PRIVATE_PURPOSE


def _public(ledger, duplicate):
    return dict(id=ledger['id'], state=ledger.get('state', ''), role=_role(ledger),
                thread_id=ledger.get('thread_id', ''), chat_id=ledger.get('chat_id', ''),
                worker_id=ledger.get('worker_id', ''), duplicate=bool(duplicate),
                name=ledger.get('name', ''), error=ledger.get('error', ''))


def _reserve(server, ledger):
    """Create (or find) the durable create_main operation for this ledger entry; idempotent."""
    store = server.store
    purpose = MAIN_PURPOSE if ledger.get('main') else PRIVATE_PURPOSE
    op, _new = _mains(server).request_create(ledger['account_id'], ledger['id'], purpose=purpose)
    ledger.update(op_id=op['id'], state='reserved' if op.get('state') == 'reserved' else ledger.get('state', 'reserved'),
                  thread_id=op.get('thread_id') or ledger.get('thread_id', ''), updated=time.time())
    store.put(APP_LEDGER, ledger)
    return ledger


def create_chat(server, body):
    """(ledger, new). Writes the pending ledger entry first; the same request_id never creates a second chat."""
    store = server.store
    body = body if isinstance(body, dict) else {}
    request_id = str(body.get('request_id') or '').strip()[:100]
    if not request_id:
        raise ValueError(MSG_REQUEST)
    ident = APP_PREFIX + request_id
    if not store.get(APP_LEDGER, ident):
        _refresh_if_duplicate(server, body)  # outside the lock: it reads the Notion chat list
    with store.lock:
        ledger = store.get(APP_LEDGER, ident)
        if ledger:
            new = False
        else:
            account_id = _selected_account(store, body)
            if not account_id:
                raise ValueError(MSG_ACCOUNT)
            name = validate_name(body.get('name'), _titles(store, account_id), allow_main=_flag(body.get('main')))
            ledger = dict(id=ident, request_id=request_id, account_id=account_id, name=name,
                          main=_flag(body.get('main')), state='pending', op_id='', thread_id='', worker_id='',
                          chat_id='', renamed=False, error='', created=time.time(), updated=time.time())
            store.put(APP_LEDGER, ledger)  # pending ledger entry before any remote step (I5)
            new = True
    if not ledger.get('op_id'):
        try:
            ledger = _reserve(server, ledger)
        except Exception:
            if new:
                store.delete(APP_LEDGER, ident)  # nothing reserved or sent: the same request_id may try again
            raise
    return ledger, new


def finish_create(server, ident):
    """Run or resume the Welcome creation, then apply the user's name. Never resends the creation."""
    store, adapter = server.store, server.adapter
    ledger = store.get(APP_LEDGER, ident)
    if not ledger or ledger.get('state') == 'done':
        return ledger
    if not ledger.get('op_id'):
        ledger = _reserve(server, ledger)
    op = _mains(server).run_create(ledger['op_id']) or store.get('operations', ledger['op_id']) or {}
    ledger.update(thread_id=op.get('thread_id') or ledger.get('thread_id', ''), chat_id=op.get('chat_id') or '')
    if op.get('state') != 'done':
        ledger.update(state=op.get('state') or 'unconfirmed', error=op.get('error', ''), updated=time.time())
        store.put(APP_LEDGER, ledger)
        return ledger
    name = ledger['name']
    with store.lock:
        worker = _worker_by_thread(store, ledger['thread_id'], ledger['account_id'])
        if not worker:
            ledger.update(state='unconfirmed', error='Created chat absent from the Notion chat list', updated=time.time())
            store.put(APP_LEDGER, ledger)
            return ledger
        _mark_user_title(worker, name)  # stored first, so no pool rule renames it while Notion is renamed
        if not ledger.get('main'):
            worker.update(purpose=PRIVATE_PURPOSE, enabled=False)
        records = [('workers', worker)]
        chat = store.get('chats', ledger['chat_id']) if ledger.get('chat_id') else None
        if chat:
            chat.update(title=name, user_named=True, account_id=ledger['account_id'])
            records.append(('chats', chat))
        store.batch(records)
        ledger['worker_id'] = worker['id']
    if not ledger.get('renamed'):
        try:
            adapter.rename_thread(adapter.client(ledger['account_id']), ledger['thread_id'], name)
        except Exception as exc:
            ledger.update(state='rename_failed', error=str(exc)[:200], updated=time.time())
            store.put(APP_LEDGER, ledger)
            return ledger
        ledger['renamed'] = True
    ledger.update(state='done', error='', updated=time.time())
    store.put(APP_LEDGER, ledger)
    store.event('app_chat_created', message='%s chat created: %s' % (_role(ledger), name),
                worker_id=ledger['worker_id'], chat_id=ledger.get('chat_id', ''))
    return ledger


def _find_worker(store, body):
    thread_id = str(body.get('thread_id') or '').strip()
    if thread_id:
        return _worker_by_thread(store, thread_id)
    chat_id = str(body.get('chat_id') or '').strip()
    if not chat_id:
        return None
    chat = store.get('chats', chat_id)
    if chat and chat.get('main_worker'):
        return store.get('workers', chat['main_worker'])
    worker = store.get('workers', chat_id)
    if worker:
        return worker
    ledger = next((l for l in store.all(APP_LEDGER) if chat_id in (l.get('id'), l.get('chat_id'))), None)
    return store.get('workers', ledger['worker_id']) if ledger and ledger.get('worker_id') else None


def promote_chat(server, body):
    """Switch an own chat between private and Main. Only local records change; nothing is created in Notion."""
    store = server.store
    body = body if isinstance(body, dict) else {}
    main = _flag(body.get('main'))
    with store.lock:
        worker = _find_worker(store, body)
        if not worker:
            raise ValueError(MSG_NOT_FOUND)
        purpose = worker.get('purpose')
        if purpose == PRIVATE_PURPOSE:
            pass
        elif purpose == MAIN_PURPOSE and worker.get('app_chat'):
            pass
        else:
            raise ValueError(MSG_SWITCH)
        chats = [c for c in store.all('chats') if c.get('main_worker') == worker['id']]
        chat_id = chats[0]['id'] if chats else ''
        if main and purpose != MAIN_PURPOSE:
            backup = worker.pop('main_chat_backup', None)
            chat = dict(chats[0]) if chats else (dict(backup) if isinstance(backup, dict) else None)
            if not chat:
                chat = dict(id='c_app_' + re.sub(r'[^A-Za-z0-9_-]', '_', worker['id'])[:80], created=time.time(),
                            folder_id='', project_path='')
            chat.update(title=worker.get('label') or 'Chat', main_worker=worker['id'], state='active',
                        account_id=worker.get('account_id', ''), user_named=True)
            if worker.get('main_number') and not chat.get('main_number'):
                chat['main_number'] = worker['main_number']
            worker.update(purpose=MAIN_PURPOSE, enabled=True, app_chat=True)
            store.batch([('workers', worker), ('chats', chat)])
            chat_id = chat['id']
        elif not main and purpose == MAIN_PURPOSE:
            if any(t.get('worker_id') == worker['id'] and t.get('state') in ACTIVE_TASK_STATES
                   for t in store.all('tasks')) or worker.get('state') in ('busy', 'recovering'):
                raise ValueError(MSG_BUSY)
            worker.update(purpose=PRIVATE_PURPOSE, enabled=False, app_chat=True,
                          main_chat_backup=chats[0] if chats else None)
            store.batch([('workers', worker)], deletes=[('chats', c['id']) for c in chats])
            chat_id = ''
    store.event('app_chat_role', message='Chat is now %s: %s' % (_role(worker), worker.get('label', '')),
                worker_id=worker['id'])
    return dict(ok=True, role=_role(worker), chat_id=chat_id, thread_id=worker.get('thread_id', ''),
                worker_id=worker['id'])


def rename_chat(server, body):
    """Rename a private chat or a Main: Notion title first, then the store (user title wins)."""
    store, adapter = server.store, server.adapter
    body = body if isinstance(body, dict) else {}
    thread_id = str(body.get('thread_id') or '').strip()
    if not thread_id:
        raise ValueError(MSG_THREAD)
    worker = _worker_by_thread(store, thread_id)
    if not worker:
        raise ValueError(MSG_NOT_FOUND)
    if worker.get('purpose') not in (PRIVATE_PURPOSE, MAIN_PURPOSE):
        raise ValueError(MSG_RENAME)
    current = str(worker.get('label') or '').strip()
    if current.casefold() in {t.casefold() for t in PROTECTED_TITLES}:
        raise ValueError(MSG_PROTECTED % current)
    name = validate_name(body.get('name'), _titles(store, worker.get('account_id'), skip=worker['id']),
                         allow_main=worker.get('purpose') == MAIN_PURPOSE)
    adapter.rename_thread(adapter.client(worker['account_id']), thread_id, name)
    with store.lock:
        worker = store.get('workers', worker['id']) or worker
        _mark_user_title(worker, name)
        records = [('workers', worker)]
        for chat in store.all('chats'):
            if chat.get('main_worker') == worker['id']:
                chat.update(title=name, user_named=True)
                records.append(('chats', chat))
        store.batch(records)
    return dict(ok=True, name=name, thread_id=thread_id, worker_id=worker['id'])


def handle_app_route(server, path, body):
    """Entry point for server.py's POST handler."""
    body = body if isinstance(body, dict) else {}
    if path == CREATE_ROUTE:
        ledger, new = create_chat(server, body)
        if ledger.get('state') not in ('done', 'failed'):
            job = getattr(server, 'job_once', None)
            if callable(job) and not getattr(server, 'app_chat_inline', False):
                job('Create chat ' + ledger['name'], lambda: finish_create(server, ledger['id']), 'op:' + ledger['id'])
                ledger = server.store.get(APP_LEDGER, ledger['id']) or ledger
            else:
                ledger = finish_create(server, ledger['id']) or ledger
        return _public(ledger, not new)
    if path == PROMOTE_ROUTE:
        return promote_chat(server, body)
    if path == RENAME_ROUTE:
        return rename_chat(server, body)
    raise ValueError(MSG_ROUTE)


def private_chats(store, account_id=''):
    """Rows for the desktop 'Chats' section; each carries the settings-button data for promote."""
    rows = []
    for worker in store.all('workers'):
        if worker.get('purpose') != PRIVATE_PURPOSE or worker.get('state') == 'missing':
            continue
        if account_id and worker.get('account_id') != account_id:
            continue
        thread = worker.get('thread_id') or ''
        rows.append(dict(worker_id=worker['id'], thread_id=thread, role=PRIVATE_PURPOSE,
                         title=str(worker.get('label') or 'Chat')[:120],
                         settings=dict(promote=dict(route=PROMOTE_ROUTE, body=dict(thread_id=thread, main=True),
                                                    label='Make this a Main'),
                                       rename=dict(route=RENAME_ROUTE, body=dict(thread_id=thread)))))
    rows.sort(key=lambda r: (r['title'].casefold(), r['worker_id']))
    return rows
