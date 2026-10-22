"""Delete retired Coworker chats (ids kept in worker['retired_threads']).

Safety rules (a chat is only deleted when all hold):
- the id is listed in some worker's retired_threads;
- it is no worker's current thread_id;
- it does not belong to a Main or operator chat (chat records with main_worker / operator,
  or a worker with purpose 'main');
- its account is 'ready';
- its Notion title is ours: empty, a pool title, 'Cowork...' or 'Welcome to Notion...',
  not protected and not set by the user (data.user_set_title).
Already deleted chats (alive=False) are dropped from retired_threads and reported as deleted.
Remote work runs sequentially, one account at a time (request budget I11).
dry_run decides and reports only: no remote write and no store change.
One bad chat never raises; it ends in skipped or failed.
"""
import contextlib

from .cowork_names import WELCOME_TITLE, is_pool_title, is_protected_title
from .main_handoff_ops import is_protected


def _title(record):
    data = record.get('data') if isinstance(record.get('data'), dict) else {}
    value = data.get('title', record.get('title', ''))
    if isinstance(value, list):
        value = ''.join(str(x[0]) if isinstance(x, list) and x else str(x) for x in value)
    return str(value or '').strip()


def _user_named(record):
    data = record.get('data') if isinstance(record.get('data'), dict) else {}
    return bool(data.get('user_set_title') or record.get('user_set_title'))


def title_reason(record):
    """'' when the Notion title is ours, otherwise the skip reason."""
    title = _title(record)
    if title and (is_protected(title) or is_protected_title(title)):
        return 'protected title: ' + title
    if _user_named(record):
        return 'title set by the user: ' + (title or '(empty)')
    if not title or is_pool_title(title) or title.startswith('Cowork') or title.startswith(WELCOME_TITLE):
        return ''
    return 'named chat: ' + title


def _lock(store):
    lock = getattr(store, 'lock', None)
    return lock if lock is not None else contextlib.nullcontext()


RETIRED_PURPOSE = 'retired_worker'


def _drop(store, worker_id, tid):
    """Remove tid from the worker's retired_threads on a fresh read; mark an age-retired worker's own chat deleted."""
    with _lock(store):
        worker = store.get('workers', worker_id)
        if not worker:
            return
        changed = False
        retired = [t for t in (worker.get('retired_threads') or []) if t != tid]
        if retired != (worker.get('retired_threads') or []):
            worker['retired_threads'] = retired
            changed = True
        if worker.get('purpose') == RETIRED_PURPOSE and worker.get('thread_id') == tid and not worker.get('chat_deleted'):
            worker.update(chat_deleted=True, enabled=False, state='missing')
            changed = True
        if changed:
            store.put('workers', worker)


def _event(store, kind, **fields):
    try:
        store.event(kind, **fields)
    except Exception:
        pass


def delete_retired(store, adapter, dry_run=False, limit=50):
    """Delete retired Coworker chats -> dict(deleted=[...], skipped=[{thread_id, reason}], failed=[{thread_id, error}])."""
    deleted, skipped, failed = [], [], []
    workers = store.all('workers')
    chats = store.all('chats')
    # Age-retired Coworkers (purpose 'retired_worker') keep their old chat as thread_id: that chat is retired too.
    current = {w.get('thread_id') for w in workers if w.get('thread_id') and w.get('purpose') != RETIRED_PURPOSE}
    main_workers = {c.get('main_worker') for c in chats if c.get('main_worker')}
    main_workers |= {w['id'] for w in workers if w.get('purpose') in ('main', 'operator')}
    special_threads = {c.get('thread_id') for c in chats
                       if c.get('thread_id') and (c.get('main_worker') or c.get('operator'))}
    special_threads |= {w.get('thread_id') for w in workers if w['id'] in main_workers and w.get('thread_id')}
    operator_workers = {c.get('worker_id') for c in chats if c.get('operator') and c.get('worker_id')}

    by_account, seen = {}, set()
    for worker in workers:
        own = [worker.get('thread_id')] if worker.get('purpose') == RETIRED_PURPOSE and not worker.get('chat_deleted') else []
        for tid in (worker.get('retired_threads') or []) + own:
            if not tid or tid in seen:
                continue
            seen.add(tid)
            if tid in current:
                skipped.append(dict(thread_id=tid, reason='current thread of a worker'))
            elif tid in special_threads or worker['id'] in main_workers or worker['id'] in operator_workers:
                skipped.append(dict(thread_id=tid, reason='belongs to a Main or operator chat'))
            else:
                by_account.setdefault(worker.get('account_id', ''), []).append((worker['id'], tid))

    budget = max(0, int(limit))
    for account_id, items in by_account.items():
        account = store.get('accounts', account_id) or {}
        if account.get('state') != 'ready':
            skipped += [dict(thread_id=tid, reason='account %s is not ready' % (account_id or '?')) for _, tid in items]
            continue
        try:
            client = adapter.client(account_id)
            records = adapter.thread_records(client, [tid for _, tid in items]) or {}
        except Exception as exc:
            failed += [dict(thread_id=tid, error='cannot read chat titles: ' + str(exc)[:160]) for _, tid in items]
            continue
        for worker_id, tid in items:
            record = records.get(tid)
            if not isinstance(record, dict):
                skipped.append(dict(thread_id=tid, reason='no Notion record; title cannot be checked'))
                continue
            if record.get('alive') is False:
                deleted.append(dict(thread_id=tid, worker_id=worker_id, account_id=account_id,
                                    note='already gone' + (' (dry run)' if dry_run else '')))
                if not dry_run:
                    _drop(store, worker_id, tid)
                    _event(store, 'coworker_old_chat_deleted', worker_id=worker_id, thread_id=tid,
                           message=tid + ': already gone; removed from retired_threads')
                continue
            reason = title_reason(record)
            if reason:
                skipped.append(dict(thread_id=tid, reason=reason))
                continue
            if budget <= 0:
                skipped.append(dict(thread_id=tid, reason='limit reached'))
                continue
            budget -= 1
            if dry_run:
                deleted.append(dict(thread_id=tid, worker_id=worker_id, account_id=account_id, note='dry run: would delete'))
                continue
            try:
                adapter.delete_thread(client, tid)
            except Exception as exc:
                error = str(exc)[:200] or exc.__class__.__name__
                failed.append(dict(thread_id=tid, error=error))
                _event(store, 'coworker_old_chat_delete_failed', worker_id=worker_id, thread_id=tid,
                       message=tid + ': delete failed: ' + error + '; kept in retired_threads')
                continue
            _drop(store, worker_id, tid)
            deleted.append(dict(thread_id=tid, worker_id=worker_id, account_id=account_id, note='deleted'))
            _event(store, 'coworker_old_chat_deleted', worker_id=worker_id, thread_id=tid,
                   message=tid + ': retired Coworker chat deleted')
    return dict(deleted=deleted, skipped=skipped, failed=failed, dry_run=bool(dry_run))
