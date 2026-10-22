"""Slim archived thread_context records and measure the Main's live context.

Pure functions only: no database access, no file writes and no network use.
The Main wires them into chat_context.py later. A thread_context record is the
dict built in chat_context.py: id, observations, entities, latest, updated,
estimated_tokens and, after a handoff, native_thread, native_messages,
native_tokens and native_archived_at. Entities carry their position in the
thread under the key 'sequence' (chat_context.py:36, :58, :190).
"""
import copy
import hashlib
import json
import math

SLIM_MAX_BYTES = 2 * 1024 * 1024
KEEP_S_DEFAULT = 86400
DROPPED_FIELDS = ('entities', 'latest', 'native_messages', 'native_thread')
SEQUENCE_FIELDS = ('sequence', 'seq')


def _is_number(value):
    """True for finite int or float values; bool does not count as a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def canonical_json(value):
    """Stable JSON text: sorted keys, compact separators, real UTF-8 characters."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)


def estimate_tokens(value):
    """ceil(utf8 bytes / 3) of a string, or of the canonical JSON of any other value."""
    text = value if isinstance(value, str) else canonical_json(value)
    return math.ceil(len(text.encode('utf-8')) / 3)


def record_size(record):
    """Byte length of the canonical JSON of the record."""
    return len(canonical_json(record).encode('utf-8'))


def content_sha256(record):
    """sha256 hex digest of the canonical JSON of the full record."""
    return hashlib.sha256(canonical_json(record).encode('utf-8')).hexdigest()


def slim_reason(record, now, keep_s=KEEP_S_DEFAULT):
    """Return 'retired', 'too_large' or None when the record should stay as it is."""
    if not isinstance(record, dict) or record.get('slimmed') is True:
        return None
    retired_at = record.get('retired_at')
    if _is_number(retired_at) and _is_number(now) and now - retired_at > keep_s:
        return 'retired'
    if record_size(record) > SLIM_MAX_BYTES:
        return 'too_large'
    return None


def slim_record(record, now, keep_s=KEEP_S_DEFAULT):
    """Return a slimmed copy of a thread_context record; the input is never changed.

    The record is slimmed when its thread was retired more than keep_s seconds
    ago (retired_at) or when its canonical JSON is larger than SLIM_MAX_BYTES.
    A slimmed record keeps only id, summary, content_sha256 (of the full
    original record), estimated_tokens, updated and retired_at, plus
    slimmed=True and slim_reason. entities, latest, native_messages and
    native_thread are dropped. Otherwise an unchanged deep copy is returned
    with slimmed=False. A record that is already slimmed is returned as a copy.
    """
    if not isinstance(record, dict):
        raise TypeError('record must be a dict')
    if record.get('slimmed') is True:
        return copy.deepcopy(record)
    reason = slim_reason(record, now, keep_s)
    if reason is None:
        kept = copy.deepcopy(record)
        kept['slimmed'] = False
        return kept
    tokens = record.get('estimated_tokens')
    if not _is_number(tokens):
        tokens = estimate_tokens(record)
    return dict(
        id=record.get('id'),
        summary=copy.deepcopy(record.get('summary')),
        content_sha256=content_sha256(record),
        estimated_tokens=tokens,
        updated=record.get('updated'),
        retired_at=record.get('retired_at'),
        slimmed=True,
        slim_reason=reason,
    )


def entity_sequence(entity):
    """Numeric sequence of an entity ('sequence', then 'seq'), or None."""
    if not isinstance(entity, dict):
        return None
    for field in SEQUENCE_FIELDS:
        value = entity.get(field)
        if _is_number(value):
            return value
    return None


def main_usage(entities, compacted_seq, turns_since):
    """Estimate the Main's live context from entities seen after the last compaction.

    Bug this fixes: the old Main estimate counted every entity ever observed in
    the thread_context record, so right after a compaction it still reported
    about 1.76M tokens at 1 turn. Only entities whose sequence is greater than
    compacted_seq are counted now. Entities without a numeric sequence
    ('sequence', falling back to 'seq') are not counted. turns is turns_since.
    """
    base = compacted_seq if _is_number(compacted_seq) else 0
    counted = {}
    for key, entity in (entities or {}).items():
        sequence = entity_sequence(entity)
        if sequence is not None and sequence > base:
            counted[key] = entity
    return dict(estimated_tokens=estimate_tokens(counted) if counted else 0, turns=turns_since)


def prune_plan(records, now, keep_s=KEEP_S_DEFAULT):
    """Dry run: list of (id, action) sorted by id; action is 'slim', 'keep' or 'skip_invalid'.

    Nothing is written anywhere and the records are only read. records may be
    a list of records or a dict of id to record. A record that is not a dict or
    has no usable id gets 'skip_invalid', with the dict key or '' as its id.
    """
    if isinstance(records, dict):
        items = list(records.items())
    else:
        items = [(None, record) for record in (records or ())]
    plan = []
    for key, record in items:
        ident = record.get('id') if isinstance(record, dict) else None
        if not isinstance(ident, str) or not ident:
            plan.append(('' if key is None else str(key), 'skip_invalid'))
            continue
        plan.append((ident, 'slim' if slim_reason(record, now, keep_s) else 'keep'))
    plan.sort()
    return plan


PRUNE_KEEP_S = 6 * 3600
OPEN_TASK_STATES = ('queued', 'retry', 'running', 'uncertain', 'cancelling', 'awaiting_main')
GONE_WORKER_STATES = ('missing', 'retired', 'disabled')


def _thread_key(worker):
    account, thread = worker.get('account_id'), worker.get('thread_id')
    return '%s:%s' % (account, thread) if account and thread else ''


def prune_ids(records, workers, chats, tasks, now, keep_s=PRUNE_KEEP_S):
    """Sorted ids of thread_context records that nobody needs any more (pure, nothing is written).

    A record is kept when its thread belongs to an enabled worker of any purpose that is not
    missing, retired or disabled, to the main_worker of any chat, or to a task in an open state
    (by thread_id or by worker_id), or when it was updated less than keep_s seconds ago. Every
    other record is deleted, including those of retired, missing or disabled workers and of
    threads no worker knows. records is a list of records or a dict of id to record; only id and
    updated are read. A record without a numeric updated counts as old. When now is not a
    number nothing is deleted.
    """
    if not _is_number(now):
        return []
    if not _is_number(keep_s) or keep_s < 0:
        keep_s = PRUNE_KEEP_S
    workers = [w for w in (workers or ()) if isinstance(w, dict)]
    by_id = {str(w['id']): w for w in workers if w.get('id')}
    owners = [w for w in workers if w.get('enabled') and w.get('state') not in GONE_WORKER_STATES]
    task_threads = set()
    for chat in chats or ():
        owner = by_id.get(str(chat.get('main_worker') or '')) if isinstance(chat, dict) else None
        if owner:
            owners.append(owner)
    for task in tasks or ():
        if not isinstance(task, dict) or task.get('state') not in OPEN_TASK_STATES:
            continue
        if task.get('thread_id'):
            task_threads.add(str(task['thread_id']))
        owner = by_id.get(str(task.get('worker_id') or ''))
        if owner:
            owners.append(owner)
    keep = set()
    for owner in owners:
        keep.add(_thread_key(owner))
        keep.add(str(owner.get('id')))
    keep.discard('')
    items = list(records.values()) if isinstance(records, dict) else list(records or ())
    doomed = []
    for record in items:
        ident = record.get('id') if isinstance(record, dict) else None
        if not isinstance(ident, str) or not ident:
            continue
        if ident in keep or ident.split(':', 1)[-1] in task_threads:
            continue
        updated = record.get('updated')
        if _is_number(updated) and now - updated < keep_s:
            continue
        doomed.append(ident)
    return sorted(set(doomed))
