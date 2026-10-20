"""Chat pool rules: one Main per account; Coworkers are joined pool chats the user never renamed.

New chats are created as 'Welcome to Notion'. Pool titles are decided by
cowork_names.is_pool_title ('Welcome to Notion', 'Coworker N', 'Coworker (retired)' and the
legacy 'Cowork joined', 'Cowork N', 'Cowork (retired)'). Naming (rule I41) runs in
name_coworkers -> apply_cowork_names -> adapter.rename_thread: periodic check every 300 s
(team_rotation.TeamRotator.maybe_check, CHECK_S) and after rotation (TeamRotator._rotate_once,
right after apply_chat_pool). The periodic check also names a new Welcome chat soon after it
joined. Legacy titles are migrated to 'Coworker N' with the same number.

A chat counts as joined once Notion stored at least one completed turn for it
(thread.data.turn_outcomes). Chats the user renamed (any other title) are never
used as Coworkers. Fixed pool: pool_decision never enables a chat that is not
already enabled; enabling is only possible through the explicit settings action
(/api/worker), because user chats carry the same 'Welcome to Notion' title.
Only name_coworkers performs network calls (thread_records, rename_thread).
"""
import json
import time

from .cowork_names import RETIRED_TITLES, is_pool_title, rename_plan, user_renamed

MAIN_TITLE = 'Main'
MAIN_TITLES = ('Main', 'Main Chat')
COWORK_TITLE = 'Cowork joined'
WELCOME_TITLE = 'Welcome to Notion'
SCRIPT_MODE_MARKERS = ('is not available in script mode', 'Connection NotionTool is not available')
UNTOUCHED_PURPOSES = ('external', 'operator')
MAX_RENAMES_PER_PASS = 5
RENAME_RETRY_S = 900  # same backoff as team_rotation.BACKOFF_S


def joined(record):
    record = record or {}
    if not record or record.get('alive') is False:
        return False
    outcomes = (record.get('data') or {}).get('turn_outcomes') or {}
    return any(isinstance(o, dict) and o.get('status') == 'completed' for o in outcomes.values())


def user_titled(record, worker=None):
    """The user renamed the chat; a title the harness set itself does not count."""
    return user_renamed(worker or {}, record)


def pool_decision(worker, record):
    """Return (changes, rename_to, event) for one non-main chat.

    rename_to is always None now: naming is done by name_coworkers (rule I41).
    Fixed pool: a worker that is neither enabled nor admitted is never enabled here.
    Admission (pool_admitted) comes only from the pool itself: ensure_team admits the
    Welcome chats it created and Notion confirmed, and an enabled pool chat that is
    not joined right now (or whose record could not be read) keeps its admission so a
    single failed read cannot drop it from the pool for good. Only /api/worker enables
    other chats. A release ends the admission. External and operator chats are never touched.
    """
    if worker.get('purpose') in UNTOUCHED_PURPOSES:
        return {}, None, None
    changes, rename, event = _pool_rule(worker, record)
    admitted = bool(worker.get('enabled') or worker.get('pool_admitted'))
    if changes.get('enabled') and not admitted:
        return dict(enabled=False), None, None
    if changes.get('join_state') == 'released':
        if worker.get('pool_admitted'):
            changes = dict(changes, pool_admitted=False)
    elif changes.get('enabled'):
        if worker.get('pool_admitted'):
            changes = dict(changes, pool_admitted=False)
    elif admitted and changes.get('join_state') == 'pending' and not worker.get('pool_admitted'):
        changes = dict(changes, pool_admitted=True)
    return changes, rename, event


def _pool_rule(worker, record):
    """Join-state rule; pool_decision applies the fixed-pool guard on top. Never renames."""
    title = worker.get('label') or ''
    busy = worker.get('state') in ('busy', 'recovering') or bool(worker.get('lease'))
    is_joined = joined(record)
    state = worker.get('join_state', '')
    if title == COWORK_TITLE:
        if is_joined:
            # Legacy marker: keep the Coworker; name_coworkers migrates it to 'Coworker N'.
            return dict(enabled=True, welcome=True, join_state='joined'), None, None
        if busy:
            return dict(enabled=False, join_state='pending'), None, None
        return dict(enabled=False, join_state='pending'), None, 'coworker_not_joined'
    own = bool(worker.get('harness_title')) and title == worker.get('harness_title')
    if (is_pool_title(title) or own) and title not in RETIRED_TITLES and not user_titled(record, worker):
        # Every pool chat ('Welcome to Notion', 'Coworker N', legacy 'Cowork N') never renamed by the user.
        if is_joined:
            return dict(enabled=True, welcome=True, join_state='joined'), None, (None if state == 'joined' else 'coworker_joined')
        return dict(enabled=False, join_state='pending'), None, None
    # A user chat, a retired chat or a renamed Coworker: keep it, never assign work to it.
    if state in ('joined', 'pending'):
        return dict(enabled=False, join_state='released'), None, 'coworker_released'
    return dict(enabled=False), None, None


def apply_cowork_names(adapter, client, workers, chats, saved, settings=None, ledger=None, budget_ok=True,
                       max_per_pass=MAX_RENAMES_PER_PASS, now=None, require_record=False):
    """Rename joined Coworkers to 'Coworker N' through adapter.rename_thread (setting cowork_names, default on).

    Returns dict(numbers, renamed, failed, skipped, enabled). numbers is the slot mapping
    the caller saves; renamed lists (thread_id, title) that succeeded; failed lists
    (thread_id, title, error); skipped lists (thread_id, title, reason) not tried this pass
    (budget, max_per_pass, no record). Renames run only for chats rename_plan selects, so
    Main, private, user-renamed and protected chats are never touched.
    """
    from .cowork_names import assign_numbers
    numbers = assign_numbers(workers, saved, ledger)
    if not (settings or {}).get('cowork_names', True):
        return dict(numbers=numbers, renamed=[], failed=[], skipped=[], enabled=False)
    renamed, failed, skipped = [], [], []
    for thread_id, title in rename_plan(workers, chats, numbers, ledger, now):
        if require_record and not (isinstance(chats, dict) and isinstance(chats.get(thread_id), dict) and chats.get(thread_id)):
            skipped.append((thread_id, title, 'no record'))
        elif not budget_ok:
            skipped.append((thread_id, title, 'network budget'))
        elif len(renamed) + len(failed) >= max_per_pass:
            skipped.append((thread_id, title, 'max per pass'))
        else:
            try:
                adapter.rename_thread(client, thread_id, title)
                renamed.append((thread_id, title))
            except Exception as exc:
                failed.append((thread_id, title, str(exc)[:200]))
    return dict(numbers=numbers, renamed=renamed, failed=failed, skipped=skipped, enabled=True)


def budget_reason(adapter):
    """'' when the shared network budget allows calls, else why not (same rule as TeamRotator)."""
    status = getattr(getattr(adapter, 'network_budget', None), 'status', None)
    if not callable(status):
        return ''
    try:
        info = status() or {}
    except Exception:
        return 'network budget unavailable'
    if info.get('paused'):
        return 'remote calls paused'
    if (info.get('cooldown_seconds') or 0) > 0:
        return 'network cooldown'
    return ''


def name_coworkers(adapter, client, account_id, records=None, now=None, store=None):
    """One naming pass for one account: joined, idle Coworkers get 'Coworker N' (rule I41).

    Called by the periodic check every 300 s (TeamRotator.maybe_check -> name_pass) and after
    rotation (TeamRotator._rotate_once); with records=None the records are read here, budget
    permitting. Uses
    adapter.rename_thread (the rename path of the Main switch). A failure sets
    rename_retry_at = now + RENAME_RETRY_S and records coworker_rename_failed; the chat is
    tried again on a later pass, never in a loop. Returns the apply_cowork_names result.
    """
    store = store or adapter.store
    now = time.time() if now is None else now
    account = store.get('accounts', account_id) or {}
    settings_fn = getattr(store, 'settings', None)
    settings = (settings_fn() if callable(settings_fn) else None) or {}
    workers = [w for w in store.all('workers') if w.get('account_id') == account_id and w.get('state') != 'missing']
    ledger = account.get('team_threads') or {}
    budget_ok = not budget_reason(adapter)
    if records is None:
        wanted = [w['thread_id'] for w in workers if w.get('purpose', 'worker') == 'worker' and w.get('enabled')
                  and w.get('join_state') == 'joined' and w.get('thread_id')]
        records = adapter.thread_records(client, wanted) if (wanted and budget_ok) else {}
    result = apply_cowork_names(adapter, client, workers, records or {}, account.get('cowork_numbers'), settings,
                                ledger, budget_ok, MAX_RENAMES_PER_PASS, now, require_record=True)
    by_thread = {w.get('thread_id'): w for w in workers if w.get('purpose', 'worker') == 'worker'}
    for thread_id, title in result['renamed']:
        worker = by_thread.get(thread_id)
        if worker:
            worker.update(label=title, harness_title=title, renamed_at=now)
            worker.pop('rename_retry_at', None)
            store.put('workers', worker)
        store.event('coworker_renamed', message=title + ' ' + thread_id, slot=account.get('slot'))
    for thread_id, title, error in result['failed']:
        worker = by_thread.get(thread_id)
        if worker:
            worker['rename_retry_at'] = now + RENAME_RETRY_S
            store.put('workers', worker)
        store.event('coworker_rename_failed', message=title + ' ' + thread_id + ': ' + error, slot=account.get('slot'))
    if account and result['numbers'] != account.get('cowork_numbers'):
        account['cowork_numbers'] = result['numbers']
        store.put('accounts', account)
    return result


def _main_rank(worker):
    # Worker records carry no 'created' field (notion.py refresh); last_used is the stored fallback.
    stamp = worker.get('created') or worker.get('last_used') or 0
    try:
        stamp = float(stamp)
    except (TypeError, ValueError):
        stamp = 0.0
    return stamp, str(worker.get('thread_id') or '')


def pick_main(workers, ledger_main=''):
    """Single Main rule for one account.

    Candidates: not missing, not released, not external/operator; purpose 'main' first,
    else chats titled Main / Main Chat. The ledger thread wins; otherwise the newest by
    created (fallback last_used), ties broken by the highest thread_id.
    """
    alive = [w for w in workers if w.get('state') != 'missing' and w.get('join_state') != 'released'
             and w.get('purpose') not in UNTOUCHED_PURPOSES]
    mains = [w for w in alive if w.get('purpose') == 'main']
    titled = [w for w in alive if w.get('label') in MAIN_TITLES]
    if ledger_main:
        hit = next((w for w in mains + titled if w.get('thread_id') == ledger_main), None)
        if hit:
            return hit
    for group in (mains, titled):
        if group:
            return max(group, key=_main_rank)
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
