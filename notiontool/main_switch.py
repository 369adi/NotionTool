"""Main context switch for the operator Main: ask the user first, a Coworker writes the brief,
then the harness moves the operator Main to a fresh Welcome chat.

Flow (store kind 'main_switches', one record per request_id):
  check()   -> notice on the operator worker when its chat is long (worker['context_ask']).
  start()   -> after the user said yes: ONE delegated Coworker task writes the brief file.
  advance() -> brief_requested -> brief_ready -> prompt_sent -> switched (or failed).

Safety rules:
- No chat is ever created before the brief file exists and passed its checks.
- Only Welcome chats are created, through ops.create_welcome (mains.Mains Welcome path).
- The operator Main is repointed only by ops.confirm_ack (MainHandoffOps._repoint_after_ack),
  i.e. only after the new chat answered with the handoff check word.
- A switch starts only when the Main needs it (tokens/turns near the limit or measured lag, see
  need_reasons) and never from team setup (ensure_team never calls start()).
- On a switch the NEW chat is renamed 'Main' and, after the ack, the OLD chat is renamed
  'Main (old, YYYY-MM-DD HH:MM)'. The old operator chat is never deleted here.
- advance() never raises; remote errors are kept on the record and retried on the next call.
- Real MainHandoffOps repoints a new chat as a pool Main (purpose 'main', enabled), which is
  wrong for the operator. Until ops declares supports_operator = True, advance() stops at
  brief_ready with OPS_GAP_REASON instead of creating any chat.
"""
import os
import re
import time

from . import handoff_files
from .agent_guard import GUARD_RULE

KIND = 'main_switches'
OPERATOR = 'operator'
POLICIES = ('ask', 'auto')
ASK_TOKENS = 1300000  # ask early: the user sees lag from about 1.5M
ASK_TURNS = 1000
LAG_MS = 20000  # measured Main reply lag that counts as a real need for a switch
NO_SWITCH_TRIGGERS = ('team_setup', 'ensure_team', 'team_grow')
OLD_TITLE = 'Main (old, Archive %s)'
BRIEF_MAX_CHARS = 8000
ASK_TEXT = 'The Main chat is getting long. Ask the user whether to switch to a fresh Main chat.'
ACTIVE = ('brief_requested', 'brief_ready', 'prompt_sent')
RUN_DONE = ('done',)
RUN_FAILED = ('failed', 'cancelled')
OPS_GAP_REASON = ('Brief is ready, but the handoff ops cannot repoint an operator Main yet '
                  '(MainHandoffOps repoints the new chat as a pool Main); no chat was created')
RESUME_INSTRUCTIONS = ('You are now the operator Main of NotionTool. Read resume_brief, keep every fixed '
                       'user rule, and continue from the next steps. Delegate work to the Coworkers. ' + GUARD_RULE)
_SAFE_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')


def _now(now=None):
    return time.time() if now is None else now


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _settings(store):
    try:
        return dict(store.settings() or {})
    except Exception:
        return {}


# ---- 1. check ----------------------------------------------------------------
def need_reasons(usage, settings):
    """Why the Main really needs a switch: 'tokens'/'turns' near the context limit or measured 'lag'."""
    settings, usage = dict(settings or {}), dict(usage or {})
    reasons = []
    if _int(usage.get('estimated_tokens')) >= _int(settings.get('main_context_ask_tokens', ASK_TOKENS)):
        reasons.append('tokens')
    if _int(usage.get('turns')) >= _int(settings.get('main_context_ask_turns', ASK_TURNS)):
        reasons.append('turns')
    if usage.get('lag_ms') is not None and _int(usage.get('lag_ms')) >= _int(settings.get('main_context_lag_ms', LAG_MS)):
        reasons.append('lag')
    return reasons


def needs_switch(worker, settings):
    """(needed, measured) for an operator worker: a stored context_ask or measured usage over a limit."""
    ask = (worker or {}).get('context_ask') or {}
    if ask.get('ask'):
        return True, True
    usage = (worker or {}).get('context_usage')
    if not usage:
        return False, False
    return bool(need_reasons(usage, settings)), True


def check(store, worker, usage, settings, now=None):
    """Notice dict for a long operator chat, else None. worker['context_ask'] is written only on change."""
    if not worker or worker.get('purpose') != OPERATOR:
        return None
    settings, usage = dict(settings or {}), dict(usage or {})
    tokens, turns = _int(usage.get('estimated_tokens')), _int(usage.get('turns'))
    reasons = need_reasons(usage, settings)
    previous = worker.get('context_ask')
    if not reasons:
        if previous:
            worker['context_ask'] = None
            store.put('workers', worker)
        return None
    notice = dict(ask=True, usage=dict(estimated_tokens=tokens, turns=turns), text=ASK_TEXT,
                  reasons=reasons)
    if not previous or not previous.get('ask') or previous.get('reasons') != reasons:
        worker['context_ask'] = dict(notice, at=_now(now))
        store.put('workers', worker)
    return notice


# ---- 2. start ----------------------------------------------------------------
def brief_path(store, request_id):
    return os.path.join(str(store.directory), 'handoffs', 'main-brief-%s.md' % request_id)


def _project_rel(path, project):
    """Brief path relative to the project folder (forward slashes), or '' when outside it."""
    if not project:
        return ''
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(project))
    except ValueError:
        return ''
    return '' if rel.startswith('..') or os.path.isabs(rel) else rel.replace(os.sep, '/')


def brief_task(path, rel):
    prompt = (
        'Prepare the switch of the operator Main to a fresh chat. Write the file %s '
        '(UTF-8, LF line endings, under %d characters, Markdown). If the file already exists, it is '
        'the old Main\'s seed brief: keep every fact from it unless you verified it is wrong, and '
        'complete it. Always include the latest state of the NotionTool desktop app '
        '(GET /api/app/status, docs/DESKTOP_APP.md, data/desktop_app.json). Sections in this order: '
        '## Goal, ## Fixed user rules, ## Current status, ## Running and open tasks, ## Next steps, '
        '## Key ids (run ids, task ids, worker ids, chat ids, file paths).\n'
        'Sources: the tail of project-memory/NotionTool.md, work/audit/*.md, and the harness state '
        'through your NotionTool tools (context, runs, tasks, workers). Report facts only; mark '
        'anything unverified as unverified. Do not edit any other file, do not create or rename '
        'chats, no git, no network.' % (path, BRIEF_MAX_CHARS)
        + '\nUnder ## Fixed user rules always copy this rule (I12) verbatim: ' + GUARD_RULE)
    task = dict(id='main-brief', title='Write the Main switch brief', prompt=prompt,
                acceptance='The brief file exists, uses LF only, is under %d characters and has '
                           'all six sections.' % BRIEF_MAX_CHARS,
                files=[rel] if rel else [])
    return task


def write_seed(path, text):
    """Write the Main's own seed brief (LF, trimmed to the limit) so the brief file always exists."""
    text = str(text or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        return False
    if len(text) >= BRIEF_MAX_CHARS:
        text = text[:BRIEF_MAX_CHARS - 40].rstrip() + '\n\n(cut at the size limit)'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write((text + '\n').encode('utf-8'))
    return True


def start(store, operator_chat_id, delegate_fn, request_id, now=None, brief_text=None, trigger=None):
    """Queue the brief task once per request_id and store the 'main_switches' record.

    brief_text: optional seed brief from the operator Main. It is written before the Coworker task,
    so a Coworker that ends 'done' without writing still leaves a usable brief.
    """
    rid = str(request_id or '')
    if not _SAFE_ID.fullmatch(rid):
        return dict(ok=False, state='skipped', reason='request_id must be 1-64 letters, digits, _ or -')
    known = store.get(KIND, rid)
    if known:
        return dict(known, ok=True, duplicate=True)
    policy = _settings(store).get('main_context_policy', 'hint')
    if policy not in POLICIES:
        return dict(ok=False, state='skipped', reason="main_context_policy is '%s'; needs ask or auto" % policy)
    chat = store.get('chats', str(operator_chat_id or '')) or {}
    worker = store.get('workers', chat.get('main_worker') or '') if chat.get('main_worker') else None
    if not chat.get('operator') or chat.get('state') == 'released' or not worker or worker.get('purpose') != OPERATOR:
        return dict(ok=False, state='skipped', reason='Not an active operator Main chat')
    if str(trigger or '') in NO_SWITCH_TRIGGERS:
        return dict(ok=False, state='skipped', reason='A Main switch never starts from team setup')
    needed, measured = needs_switch(worker, _settings(store))
    if measured and not needed:
        return dict(ok=False, state='skipped', reason='The Main does not need a switch (context and lag below the limits)')
    path = brief_path(store, rid)
    seeded = False
    if brief_text:
        try:
            seeded = write_seed(path, brief_text)
        except OSError as exc:
            return dict(ok=False, state='failed', reason='seed brief not written: ' + str(exc)[:200])
    if seeded and not read_brief(path)[1]:
        # The operator Main wrote a complete brief itself: no Coworker run is needed. A Coworker cannot
        # write below data/ anyway (bridge write guard), so a brief run only waited forever on
        # 'RESULT: DONE' (root cause of the stuck switch on 2026-10-19 17:12).
        stamp = _now(now)
        record = dict(id=rid, request_id=rid, state='brief_ready', run_id='',
                      operator_chat_id=chat['id'], operator_worker_id=worker['id'],
                      operator_thread=worker.get('thread_id', ''), account_id=worker.get('account_id', ''),
                      brief_path=path, handoff_id='switch_' + rid, new_chat_id='', reason='brief from the Main',
                      error='', created=stamp, updated=stamp)
        store.put(KIND, record)
        store.event('main_switch_brief_ready', '', message='Main switch brief written by the Main', switch_id=rid)
        return dict(record, ok=True, duplicate=False)
    body = dict(chat_id=chat['id'], goal='Prepare the switch of the operator Main to a fresh Welcome chat',
                request_id='main-switch-' + rid,
                tasks=[brief_task(path, _project_rel(path, chat.get('project_path', '')))])
    try:
        result = delegate_fn(body) or {}
    except Exception as exc:
        return dict(ok=False, state='failed', reason='delegate failed: ' + str(exc)[:200])
    if not result.get('run_id'):
        return dict(ok=False, state='failed', reason='delegate returned no run_id')
    stamp = _now(now)
    record = dict(id=rid, request_id=rid, state='brief_requested', run_id=result['run_id'],
                  operator_chat_id=chat['id'], operator_worker_id=worker['id'],
                  operator_thread=worker.get('thread_id', ''), account_id=worker.get('account_id', ''),
                  brief_path=path, handoff_id='switch_' + rid, new_chat_id='', reason='', error='',
                  created=stamp, updated=stamp)
    store.put(KIND, record)
    store.event('main_switch_brief_requested', record['run_id'], message='Main switch brief requested',
                switch_id=rid)
    return dict(record, ok=True, duplicate=False)


# ---- 3. advance --------------------------------------------------------------
def _move(store, record, state, now, **fields):
    record.update(fields, state=state, updated=now, error='')
    store.put(KIND, record)
    store.event('main_switch_' + state, record.get('run_id', ''), message=record.get('reason', ''),
                switch_id=record['id'])
    return record


def _note(store, record, now, **fields):
    record.update(fields, updated=now)
    store.put(KIND, record)
    return record


def read_brief(path):
    """(text, '') when the brief is usable, else (None, reason)."""
    if not os.path.isfile(path):
        return None, 'brief file missing'
    with open(path, 'rb') as handle:
        raw = handle.read()
    if b'\r' in raw:
        return None, 'brief file has CR line endings'
    text = raw.decode('utf-8', errors='replace')
    if not text.strip():
        return None, 'brief file is empty'
    if len(text) >= BRIEF_MAX_CHARS:
        return None, 'brief file has %d characters (limit %d)' % (len(text), BRIEF_MAX_CHARS)
    return text, ''


def _archive_title(ts):
    """Title of the OLD operator Main after a switch: 'Main (old, YYYY-MM-DD HH:MM)' in local time."""
    return OLD_TITLE % time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))


def advance(store, record, ops, now=None):
    """Move one switch record forward as far as possible. Never raises."""
    try:
        return _advance(store, record, ops, now)
    except Exception as exc:
        try:
            fresh = store.get(KIND, (record or {}).get('id', '')) or dict(record or {})
            fresh['error'] = str(exc)[:200]
            if fresh.get('id'):
                store.put(KIND, fresh)
            return fresh
        except Exception:
            return dict(record or {}, error=str(exc)[:200])


def _advance(store, record, ops, now):
    record = store.get(KIND, (record or {}).get('id', '')) or record
    if record and record.get('state') == 'switched' and not record.get('archive_renamed') and record.get('operator_thread'):
        # Retry the archive title of the old operator Main (the first try can be refused or fail).
        stamp = _now(now)
        try:
            if ops.rename_chat(record['operator_thread'], _archive_title(stamp)):
                record = _note(store, record, stamp, archive_renamed=True)
        except Exception:
            pass
        return record
    if not record or record.get('state') not in ACTIVE:
        return record
    stamp = _now(now)
    if record['state'] == 'brief_requested':
        run = store.get('runs', record.get('run_id', ''))
        if not run:
            return _move(store, record, 'failed', stamp, reason='brief run not found')
        if run.get('state') in RUN_FAILED:
            return _move(store, record, 'failed', stamp, reason='brief run ' + run['state'])
        if run.get('state') not in RUN_DONE:
            # A run that only waits for the Main (e.g. answer without 'RESULT: DONE') must not block
            # the switch when the brief file itself is usable.
            if run.get('state') == 'awaiting_main' and not read_brief(record['brief_path'])[1]:
                record = _move(store, record, 'brief_ready', stamp, reason='brief usable; run awaits the Main')
            else:
                return _note(store, record, stamp, reason='waiting for the brief run')
        if record['state'] == 'brief_requested':
            _text, problem = read_brief(record['brief_path'])
            if problem:
                return _move(store, record, 'failed', stamp, reason=problem)
            record = _move(store, record, 'brief_ready', stamp, reason='brief ready')
    if record['state'] == 'brief_ready':
        if not getattr(ops, 'supports_operator', False):
            return _note(store, record, stamp, reason=OPS_GAP_REASON)
        text, problem = read_brief(record['brief_path'])
        if problem:
            return _move(store, record, 'failed', stamp, reason=problem)
        created = ops.create_welcome(record['account_id'], 'main-switch-' + record['id']) or {}
        if created.get('status') != 'ok' or not created.get('chat_id'):
            return _note(store, record, stamp, reason='Welcome chat not confirmed yet: ' + str(created)[:150])
        chat = created['chat_id']
        # The fresh Welcome chat (never a Coworker) is named 'Main' before the brief goes in.
        if not record.get('new_renamed'):
            try:
                record['new_renamed'] = bool(ops.rename_chat(chat, 'Main'))
            except Exception:
                record['new_renamed'] = False
        # Same handoff file and token that send_prompt reuses (un-acked files are returned unchanged).
        handoff = handoff_files.write_handoff(ops.folder(), ops.file_id(), dict(
            kind='main_switch', account_id=record['account_id'], handoff={},
            old_thread=record.get('operator_thread', ''), new_thread=chat, resume_brief=text,
            resume_instructions=RESUME_INSTRUCTIONS), ops.now())
        ops.send_prompt(chat, handoff_files.first_message(handoff), record['handoff_id'] + ':prompt')
        record = _move(store, record, 'prompt_sent', stamp, new_chat_id=chat, reason='brief sent; waiting for ack')
    if record['state'] == 'prompt_sent':
        acked = ops.confirm_ack(dict(id=record['handoff_id'], account_id=record['account_id'],
                                     old_chat_id=record.get('operator_thread', ''),
                                     new_chat_id=record['new_chat_id']))
        if not acked:
            return _note(store, record, stamp, reason='waiting for the ack; old operator Main stays active')
        renamed = False
        if record.get('operator_thread'):
            try:
                renamed = bool(ops.rename_chat(record['operator_thread'], _archive_title(stamp)))
            except Exception:
                renamed = False
        record = _move(store, record, 'switched', stamp, archive_renamed=renamed,
                       reason='operator Main moved to the new Welcome chat; old chat kept')
    if record['state'] == 'switched' and not record.get('archive_renamed') and record.get('operator_thread'):
        try:
            if ops.rename_chat(record['operator_thread'], _archive_title(stamp)):
                record = _note(store, record, stamp, archive_renamed=True)
        except Exception:
            pass
    return record


def sweep(store, ops_for, now=None):
    """Advance every active switch; ops_for(record) returns the ops object. Never raises."""
    out = []
    for record in list(store.all(KIND)):
        if record.get('state') in ACTIVE:
            try:
                ops = ops_for(record)
            except Exception as exc:
                out.append(dict(record, error=str(exc)[:200]))
                continue
            out.append(advance(store, record, ops, now))
    return out
