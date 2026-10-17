"""Chat pool rules: one Main per account; Coworkers are joined pool chats the user never renamed.

New chats are created as 'Welcome to Notion'. Pool titles are decided by
cowork_names.is_pool_title ('Welcome to Notion', 'Cowork joined', 'Cowork (retired)',
'Cowork N'). With the setting cowork_names (default on) apply_cowork_names renames
joined Coworkers to 'Cowork N' right after joining via adapter.rename_thread. The
legacy marker 'Cowork joined' is still recognized and renamed back to 'Welcome to Notion'.

A chat counts as joined once Notion stored at least one completed turn for it
(thread.data.turn_outcomes). Chats the user renamed (any other title) are never
used as Coworkers. Nothing here performs network calls.
"""
import json

from .cowork_names import RETIRED_TITLE, is_pool_title, rename_plan

MAIN_TITLE = 'Main'
MAIN_TITLES = ('Main', 'Main Chat')
COWORK_TITLE = 'Cowork joined'
WELCOME_TITLE = 'Welcome to Notion'
SCRIPT_MODE_MARKERS = ('is not available in script mode', 'Connection NotionTool is not available')


def joined(record):
    record = record or {}
    if not record or record.get('alive') is False:
        return False
    outcomes = (record.get('data') or {}).get('turn_outcomes') or {}
    return any(isinstance(o, dict) and o.get('status') == 'completed' for o in outcomes.values())


def user_titled(record):
    return bool(((record or {}).get('data') or {}).get('user_set_title'))


def pool_decision(worker, record):
    """Return (changes, rename_to, event) for one non-main chat."""
    title = worker.get('label') or ''
    busy = worker.get('state') in ('busy', 'recovering') or bool(worker.get('lease'))
    is_joined = joined(record)
    state = worker.get('join_state', '')
    if title == COWORK_TITLE:
        if is_joined:
            # Legacy marker: keep the Coworker, but give the chat its unnamed title back when idle.
            return dict(enabled=True, welcome=True, join_state='joined'), (None if busy or user_titled(record) else WELCOME_TITLE), None
        if busy:
            return dict(enabled=False, join_state='pending'), None, None
        rename = None if user_titled(record) else WELCOME_TITLE
        return dict(enabled=False, join_state='pending'), rename, 'coworker_not_joined'
    if is_pool_title(title) and title != RETIRED_TITLE and not user_titled(record):
        # Every pool chat ('Welcome to Notion' or 'Cowork N') never renamed by the user is a Coworker once joined.
        if is_joined:
            return dict(enabled=True, welcome=True, join_state='joined'), None, (None if state == 'joined' else 'coworker_joined')
        return dict(enabled=False, join_state='pending'), None, None
    # A user chat, a retired chat or a renamed Coworker: keep it, never assign work to it.
    if state in ('joined', 'pending'):
        return dict(enabled=False, join_state='released'), None, 'coworker_released'
    return dict(enabled=False), None, None


def apply_cowork_names(adapter, client, workers, chats, saved, settings=None):
    """Rename joined Coworkers to 'Cowork N' through adapter.rename_thread (setting cowork_names, default on).

    Returns dict(numbers, renamed, failed). numbers is the slot mapping the caller
    should save for the next call; renamed lists (thread_id, title) that succeeded;
    failed lists (thread_id, title, error). Renames run only for chats rename_plan
    selects, so Main chats, user chats and user_set_title chats are never touched.
    """
    from .cowork_names import assign_numbers
    numbers = assign_numbers(workers, saved)
    if not (settings or {}).get('cowork_names', True):
        return dict(numbers=numbers, renamed=[], failed=[], enabled=False)
    renamed, failed = [], []
    for thread_id, title in rename_plan(workers, chats, numbers):
        try:
            adapter.rename_thread(client, thread_id, title)
            renamed.append((thread_id, title))
        except Exception as exc:
            failed.append((thread_id, title, str(exc)[:200]))
    return dict(numbers=numbers, renamed=renamed, failed=failed, enabled=True)


def pick_main(workers, ledger_main=''):
    """Existing Main worker for an account, else a chat titled Main / Main Chat."""
    alive = [w for w in workers if w.get('state') != 'missing']
    mains = [w for w in alive if w.get('purpose') == 'main']
    titled = [w for w in alive if w.get('label') in MAIN_TITLES]
    for group in (mains, titled):
        if group:
            return next((w for w in group if w.get('thread_id') == ledger_main),
                        next((w for w in group if w.get('label') == MAIN_TITLE), group[0]))
    return None


def script_mode_failure(steps):
    """True when a worker's own turn reports that the NotionTool MCP connection is missing."""
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        kind = str(step.get('type', ''))
        if kind.startswith('user') or kind in ('config', 'context', 'title'):
            continue
        text = json.dumps(step, ensure_ascii=False)
        if any(marker in text for marker in SCRIPT_MODE_MARKERS):
            return True
    return False
