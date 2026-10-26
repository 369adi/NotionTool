"""Switch context button -> the Main does the context switch itself (owner 2026-10-25 23:06).

The button (desktop chat_context_switch, Main chats) calls POST /api/main/switch/request. request() stores
one 'main_switch_requests' record; deliver() (run by MainWake and once right after the request) sends it into
the Main's own Notion thread as soon as that chat is idle, with the same sender as the automatic messages
(the user's model and effort stay). The message tells the Main to update its handoff file, write the switch
brief itself, POST /api/main/switch/start with brief_text and drive /api/main/switch/advance until 'switched'.

No coworker brief task (a coworker cannot write below data/: root cause of the stuck switches) and no new
chat is created here. A record is marked 'sending' before the message goes out, so it is never sent twice.
"""

import threading
import time

KIND = 'main_switch_requests'
SWITCH_ACTIVE = ('brief_requested', 'brief_ready', 'prompt_sent')
OPEN_STATES = ('pending', 'sending')
EXPIRE_S = 1800.0  # a request the busy Main never picked up within 30 min expires (press the button again)
BUSY_WINDOW = 5
HEAD = 'NotionTool (automatic): Switch context requested by the user (Switch context button).'
HANDOFF_HINT = 'your handoff file (NotionTool: work/HANDOFF_MAIN_V6.md)'
_LOCK = threading.Lock()


def _now(now=None):
    return float(time.time() if now is None else now)


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def active_switch(store):
    for record in store.all('main_switches'):
        if record.get('state') in SWITCH_ACTIVE:
            return record
    return None


def open_request(store, thread_id, now=None):
    now = _now(now)
    for record in store.all(KIND):
        if (record.get('thread_id') == thread_id and record.get('state') in OPEN_STATES
                and now - _num(record.get('created')) < EXPIRE_S):
            return record
    return None


def build_message(record):
    rid = str(record.get('switch_id') or record.get('id') or '')
    thread = str(record.get('thread_id') or '')
    account = str(record.get('account_id') or '')
    return '\n'.join([
        HEAD,
        'Do the context switch now, without asking the user:',
        '1. Update %s: what you did, what is still open, and the newest user requests word for word.'
        % HANDOFF_HINT,
        '2. Write the switch brief yourself (Goal with the newest user rules; Current status that starts with '
        '"READ FIRST, COMPLETELY: <handoff file>"; Running/open; Next steps; Key ids) and call '
        'POST /api/main/switch/start {"thread_id": "%s", "trigger": "user", "request_id": "%s", '
        '"brief_text": "<the brief>"}.' % (thread, rid),
        '3. Call POST /api/main/switch/advance {"id": "%s"} until the state is "switched". If the reason says '
        '"Connect and select a ready Notion account first", call POST /api/account/refresh '
        '{"account_id": "%s"} and advance again. Never create a second chat and never resend.' % (rid, account),
        '4. Tell the user the switch state and that the work continues in the new chat "Main".',
    ])


def request(store, body, now=None):
    """Queue one switch request for the Main chat of body (chat_id / worker_id / thread_id)."""
    from .operator_main import switch_chat_id
    body = body if isinstance(body, dict) else {}
    active = active_switch(store)
    if active:
        return dict(active, ok=True, duplicate=True, kind='switch')
    chat_id = switch_chat_id(store, body)
    chat = store.get('chats', chat_id) or {}
    worker = store.get('workers', str(chat.get('main_worker') or '')) or {}
    thread = str(worker.get('thread_id') or '')
    if not chat or chat.get('state') == 'released' or not thread or worker.get('purpose') == 'worker':
        raise ValueError('Switch context works for an active Main chat only')
    known = open_request(store, thread, now)
    if known:
        return dict(known, ok=True, duplicate=True, kind='request')
    stamp = _now(now)
    ident = 'btn-%s-%s' % (time.strftime('%Y%m%d-%H%M%S', time.localtime(stamp)), thread.replace('-', '')[:8])
    record = dict(id=ident, switch_id=ident, state='pending', chat_id=chat_id, worker_id=str(worker.get('id') or ''),
                  thread_id=thread, account_id=str(worker.get('account_id') or ''), created=stamp, updated=stamp,
                  error='')
    store.put(KIND, record)
    store.event('main_switch_requested', ident, message='Switch context button: request queued for the Main',
                thread_id=thread)
    return dict(record, ok=True, duplicate=False, kind='request')


def deliver(store, adapter, chat_busy, send, now=None, failed=None):
    """Send pending requests into idle Main chats; returns the request ids sent."""
    with _LOCK:
        now = _now(now)
        sent = []
        for record in store.all(KIND):
            if record.get('state') != 'pending':
                continue
            thread = str(record.get('thread_id') or '')
            if now - _num(record.get('created')) >= EXPIRE_S:
                store.put(KIND, dict(record, state='expired', updated=now,
                                     error='The Main chat stayed busy for 30 minutes; press Switch context again'))
                continue
            if active_switch(store):
                store.put(KIND, dict(record, state='superseded', updated=now))
                continue
            try:
                client = adapter.client(record.get('account_id'))
                if chat_busy(client.transcript(thread, limit=BUSY_WINDOW) or {}):
                    continue  # delivered as soon as the Main chat is idle
            except Exception as exc:
                if failed:
                    failed(thread, exc)
                continue
            store.put(KIND, dict(record, state='sending', updated=now))  # before sending: never a second message
            try:
                send(client, thread, build_message(record))
            except Exception as exc:
                store.put(KIND, dict(record, state='failed', updated=now, error=str(exc)[:200]))
                if failed:
                    failed(thread, exc)
                continue
            store.put(KIND, dict(record, state='sent', updated=now, sent_at=now))
            store.event('main_switch_request_sent', record['id'], message='Switch request sent to the Main',
                        thread_id=thread)
            sent.append(record['id'])
        return sent
