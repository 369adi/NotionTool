"""Real ops for main_handoff.MainHandoff plus the two entry points server.py calls.

Chat ids handed to MainHandoff are Notion thread ids. All remote work goes through the
injected adapter (notion.NotionAdapter); this module itself opens no connection.

Safety rules:
- The Main chat is repointed in exactly one place, _repoint_after_ack, which only
  confirm_ack calls, and only after handoff_files.verify_ack returned True.
- Without an ack the rotation stops in prompt_sent: the old Main stays active.
- Named chats (NotionTool, Sonstiges, Studio432, Roblox) are never renamed or deleted.
  Renames are limited to our own Welcome chat and to the old Main of a handoff; deletes
  to our own 'Main (Archive ...)' chats after main_handoff.DELETE_DELAY.
"""
import os
import re
import time
import types

from . import handoff_files
from . import main_handoff as mh
from .cowork_names import is_pool_title

PROTECTED_TITLES = ('NotionTool', 'Sonstiges', 'Studio432', 'Roblox')
ARCHIVE_PREFIX = 'Main (Archive '
PROMPTS = 'main_handoff_prompts'
WELCOMES = 'main_handoff_welcomes'
MAX_STEPS = 24
CREATED_CHAT_PREFIX = 'c_create_main_'  # chat records mains.run_create adds for a new Main


def is_protected(title):
    text = str(title or '').strip().lower()
    return any(text == name.lower() for name in PROTECTED_TITLES)


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(_text(x) for x in value)
    if isinstance(value, dict) and value.get('type') == 'text':
        return _text(value.get('content', value.get('text', '')))
    return ''


def _tid(thread):
    return str(thread.get('id') or thread.get('thread_id') or thread.get('threadId') or '')


RESUME_TEXT_LIMIT = 1500
RESUME_GOAL_LIMIT = 120
RESUME_MAX_STEPS = 8
RESUME_STEP_LIMIT = 200
MEMORY_TAIL_LIMIT = 4000
RESUME_HEADINGS = ('Wo wir aufgeh\u00f6rt haben', 'Was ich noch einbauen wollte', 'Was noch offen ist')
RESUME_INSTRUCTIONS = (
    'After you sent the ack token, wait for the user and do not start other work. '
    'When the user writes the first message after the ack, start your answer with a short summary. '
    'Answer the user in German, in plain and simple words, under exactly these three headings: '
    '"' + RESUME_HEADINGS[0] + '" (the last open request from resume_brief.last_user_request, '
    'in one or two sentences), '
    '"' + RESUME_HEADINGS[1] + '" (a short list of the concrete next steps from '
    'resume_brief.next_steps and the last state), '
    '"' + RESUME_HEADINGS[2] + '" (blocked or unfinished things from resume_brief.open_items). '
    'Use no internal ids, hashes, run ids or file:line references in this summary. '
    'Then continue with the last open request.')
_NEXT_LABEL_RE = re.compile('(?<![a-z\u00e4\u00f6\u00fc])(?:offen|open|naechste[rn]? schritt|n\u00e4chste[rn]? schritt|next)')
_BULLET_RE = re.compile(r'^(?:[-*+]|\d+[.)]|\[[ xX]?\])\s+')


def _is_next_label(text):
    return _NEXT_LABEL_RE.search(str(text or '').casefold()) is not None


def _cut(text, limit):
    return str(text or '').strip()[:limit]


def _newest(messages, role):
    for row in reversed(list(messages or [])):
        if isinstance(row, dict) and row.get('role') == role and str(row.get('text') or '').strip():
            return _cut(row.get('text'), RESUME_TEXT_LIMIT)
    return ''


def extract_next_steps(memory_tail, limit=RESUME_MAX_STEPS):
    """Lines under a heading or label naming Offen/Open/Naechster Schritt/Next, up to the next heading."""
    steps, active = [], False
    for raw in str(memory_tail or '').splitlines():
        line = raw.strip().replace('**', '').replace('__', '').strip()
        if not line:
            continue
        if line.startswith('#'):
            active = _is_next_label(line.lstrip('#'))
            continue
        body = _BULLET_RE.sub('', line).strip()
        head, sep, rest = body.partition(':')
        is_label = bool(sep) and 0 < len(head.strip()) <= 40
        if is_label and not rest.strip():
            active = _is_next_label(head)
            continue
        if raw.strip().startswith('**') and raw.strip().endswith('**') and not sep:
            active = _is_next_label(body)
            continue
        if is_label and _is_next_label(head):
            step = rest.strip()
        elif active:
            step = body
        else:
            continue
        if step:
            steps.append(step[:RESUME_STEP_LIMIT])
            if len(steps) >= limit:
                break
    return steps


def _open_items(open_runs):
    items = []
    for run in open_runs or []:
        if not isinstance(run, dict):
            continue
        label = ' '.join(str(run.get('title') or '').split())[:RESUME_GOAL_LIMIT]
        if not label:
            label = str(run.get('goal') or '').strip()[:RESUME_GOAL_LIMIT].replace('\r', ' ').replace('\n', ' ').strip()
        state = str(run.get('state') or 'unknown')
        items.append('%s (%s)' % (label, state) if label else '(%s)' % state)
    return items


def build_resume_brief(sources):
    """Plain resume data for the new Main; every field is always present."""
    sources = sources if isinstance(sources, dict) else {}
    messages = sources.get('messages') or []
    return dict(last_user_request=_newest(messages, 'user'), last_answer=_newest(messages, 'assistant'),
                next_steps=extract_next_steps(sources.get('memory_tail')),
                open_items=_open_items(sources.get('open_runs')))


class _EmitStore:
    """Store view whose event() goes to emit(kind, message, **fields) instead of the store."""

    def __init__(self, store, emit):
        self._store, self._emit = store, emit

    def __getattr__(self, name):
        return getattr(self._store, name)

    def event(self, kind, run_id='', **fields):
        fields.pop('commit', None)
        message = fields.pop('message', '')
        if run_id:
            fields['run_id'] = run_id
        self._emit(kind, message, **fields)


class MainHandoffOps:
    def __init__(self, store, adapter, account_id, old_chat_id='', main_chat_id='', mains=None, clock=None):
        self.store, self.adapter, self.account_id = store, adapter, account_id
        self.old_chat_id = old_chat_id      # Notion thread of the current Main
        self.main_chat_id = main_chat_id    # local chat record of that Main (messages, repoint target)
        self._mains = mains
        self._clock = clock or time.time

    # ---- helpers ------------------------------------------------------
    def now(self):
        return self._clock()

    def _event(self, kind, message='', **fields):
        fields.setdefault('account_id', self.account_id)
        self.store.event(kind, message=message, **fields)

    def _client(self):
        return self.adapter.client(self.account_id)

    def folder(self):
        return os.path.join(str(self.store.directory), 'handoffs')

    def file_id(self):
        return ('main_' + re.sub(r'[^A-Za-z0-9_-]', '_', str(self.account_id)))[:64]

    def _threads(self):
        return {_tid(t): str(t.get('title') or '') for t in (self._client().threads() or []) if _tid(t)}

    def _mains_obj(self):
        if self._mains is None:
            from .mains import Mains
            app = types.SimpleNamespace(store=self.store, adapter=self.adapter,
                                        engine=types.SimpleNamespace(lock=self.store.lock))
            self._mains = Mains(app)
        return self._mains

    def _user_named(self, chat_id):
        if (self.store.get('chats', chat_id) or {}).get('user_named'):
            return True
        worker = self._worker(chat_id)
        return bool(worker and any(c.get('user_named') for c in self.store.all('chats')
                                   if c.get('main_worker') == worker['id']))

    def _worker(self, thread_id):
        return next((w for w in self.store.all('workers') if w.get('account_id') == self.account_id
                     and w.get('thread_id') == thread_id), None)

    def _our_welcome(self, chat_id):
        return any(w.get('thread_id') == chat_id and w.get('account_id') == self.account_id
                   for w in self.store.all(WELCOMES))

    def _old_main_of_handoff(self, chat_id):
        return any(h.get('old_chat_id') == chat_id and h.get('account_id') == self.account_id
                   for h in self.store.all('main_handoffs'))

    def _memory_tail(self):
        """Last MEMORY_TAIL_LIMIT chars of the Main memory: the chat's folder memory, else the chat's own.

        Same lookup as engine.py and chat_context.py: store kind 'memory', id folder_id or chat id.
        """
        if not self.main_chat_id:
            return ''
        chat = self.store.get('chats', self.main_chat_id) or {}
        memory = self.store.get('memory', chat.get('folder_id') or self.main_chat_id) or {}
        return str(memory.get('text') or '')[-MEMORY_TAIL_LIMIT:]

    # ---- ops called by MainHandoff -------------------------------------
    def summarize(self, sources):
        sources = dict(sources or {})
        if not sources.get('messages') and self.main_chat_id:
            settings = self.store.settings() if callable(getattr(self.store, 'settings', None)) else {}
            sources['messages'] = mh._messages(self.store, self.main_chat_id,
                                               int((settings or {}).get('main_handoff_chars') or 12000))
        if not str(sources.get('memory_tail') or '').strip():
            sources['memory_tail'] = self._memory_tail()
        record = handoff_files.write_handoff(self.folder(), self.file_id(), dict(
            kind='main_handoff', account_id=self.account_id, handoff=sources,
            old_thread=self.old_chat_id, new_thread=None, resume_brief=build_resume_brief(sources),
            resume_instructions=RESUME_INSTRUCTIONS), self.now())
        return handoff_files.first_message(record)

    def list_chats(self, account_id):
        ops = [o for o in self.store.all('operations') if o.get('kind') == 'create_main'
               and o.get('account_id') == account_id and o.get('thread_id')]
        by_thread = {o['thread_id']: o for o in ops}
        out = []
        for tid, title in self._threads().items():
            chat = dict(id=tid, title=title)
            op = by_thread.get(tid)
            if op:
                chat['request_id'] = op.get('request_id')
                if op.get('state') != 'done' and str(op.get('request_id', '')).endswith(':create'):
                    try:
                        self._mains_obj().run_create(op['id'])  # resumes; never resends
                    except Exception as exc:
                        self._event('main_handoff_create_unclear', str(exc)[:200])
            out.append(chat)
        return out

    def create_welcome(self, account_id, request_id):
        saved = self.store.get(WELCOMES, request_id)
        if saved and saved.get('thread_id'):
            return dict(status='ok', chat_id=saved['thread_id'], created=False)
        try:
            mains = self._mains_obj()
            op, _created = mains.request_create(account_id, request_id, predecessor=self.main_chat_id)
            if op.get('state') != 'done':
                op = mains.run_create(op['id']) or op
        except Exception as exc:
            self._event('main_handoff_create_unclear', str(exc)[:200])
            return dict(status='unclear', error=str(exc)[:200])
        if op.get('state') == 'done' and op.get('thread_id'):
            self.store.put(WELCOMES, dict(id=request_id, request_id=request_id, account_id=account_id,
                                          thread_id=op['thread_id'], chat_record=op.get('chat_id', ''),
                                          created=self.now()))
            return dict(status='ok', chat_id=op['thread_id'], created=True)
        return dict(status='unclear', state=op.get('state'))

    def rename_chat(self, chat_id, title):
        current = self._threads().get(chat_id)
        reason = ''
        if current is None:
            reason = 'chat not listed'
        elif is_protected(current) or is_protected(title) or self._user_named(chat_id):
            reason = 'named chat'
        elif self._our_welcome(chat_id):
            reason = '' if title == 'Main' else 'Welcome chat may only become Main'
        elif self._old_main_of_handoff(chat_id):
            if not str(title).startswith(ARCHIVE_PREFIX):
                reason = 'old Main may only get an archive title'
            elif not (current.startswith('Main') or is_pool_title(current) or current.lower().startswith('welcome')):
                reason = 'old Main has a custom title'
        else:
            reason = 'not our Welcome chat or Main archive'
        if reason:
            self._event('main_handoff_rename_refused', reason + ': ' + chat_id, chat=chat_id)
            return False
        self.adapter.rename_thread(self._client(), chat_id, title)
        return True

    def has_prompt(self, chat, fingerprint):
        doc = self.store.get(PROMPTS, fingerprint)
        return bool(doc and doc.get('chat_id') == chat)

    def send_prompt(self, chat, text, fingerprint):
        if self.has_prompt(chat, fingerprint):
            return False
        record = handoff_files.write_handoff(self.folder(), self.file_id(), dict(
            kind='main_handoff', account_id=self.account_id, handoff={}, old_thread=self.old_chat_id,
            new_thread=chat), self.now())  # returns the un-acked file from summarize unchanged
        client = self._client()
        transcript = client.transcript(chat) or {}
        step_id = self.adapter.new_id(client.info['space_id'])
        doc = dict(id=fingerprint, chat_id=chat, account_id=self.account_id, token=record['token'],
                   path=record['path'], step_id=step_id, state='attempted', acked=False,
                   before_seq=(transcript.get('session') or {}).get('sequence', 0), at=self.now())
        self.store.put(PROMPTS, doc)  # recorded before sending: a crash never leads to a resend
        body = self.adapter.body(client, chat, step_id, text, client.model(), computer=True)
        try:
            for _ in client.transport.stream('runInferenceTranscript', body, client.headers(), timeout=180):
                pass
        except OSError:
            pass
        doc['state'] = 'sent'
        self.store.put(PROMPTS, doc)
        return True

    def last_user_prompt(self, chat_id):
        local = self.main_chat_id or chat_id
        page = getattr(self.store, 'message_page', None)
        rows = (page(local, limit=200) or {}).get('messages', []) if page else []
        for row in reversed(rows):
            if row.get('role') == 'user' and (row.get('text') or '').strip():
                return row['text'][:4000]
        return ''

    def delete_chat(self, chat_id):
        doc = next((h for h in self.store.all('main_handoffs') if h.get('account_id') == self.account_id
                    and h.get('old_chat_id') == chat_id and h.get('state') == 'done'), None)
        prompt = self.store.get(PROMPTS, doc['id'] + ':prompt') if doc else None
        title = self._threads().get(chat_id)
        reason = ''
        if not doc:
            reason = 'no finished handoff for this chat'
        elif not mh.due_for_delete(doc, self.now()):
            reason = 'DELETE_DELAY (%d s) not reached' % mh.DELETE_DELAY
        elif not (prompt and prompt.get('acked')):
            reason = 'handoff was never acked'
        elif title is None:
            return False  # already gone
        elif is_protected(title) or self._user_named(chat_id):
            reason = 'named chat'
        elif not title.startswith(ARCHIVE_PREFIX):
            reason = 'title is not a Main archive'
        if reason:
            self._event('main_archive_delete_refused', reason + ': ' + chat_id, chat=chat_id)
            raise ValueError('Archive delete refused: ' + reason)
        return self.adapter.delete_thread(self._client(), chat_id)

    # ---- ack gate --------------------------------------------------------
    def _reply_text(self, chat, prompt):
        transcript = self._client().transcript(chat) or {}
        entities = [p.get('entity', {}) for p in transcript.get('patches', []) if p.get('op') == 'put']
        fresh = sorted((e for e in entities if e.get('sequence', 0) > prompt.get('before_seq', 0)),
                       key=lambda e: e.get('sequence', 0))
        return '\n'.join(_text(e.get('content', [])) for e in fresh if e.get('kind') == 'assistant_message')

    def confirm_ack(self, handoff):
        prompt = self.store.get(PROMPTS, handoff['id'] + ':prompt')
        if not prompt:
            self._event('main_handoff_ack_missing', 'No handoff prompt recorded; old Main stays active',
                        handoff_id=handoff['id'])
            return False
        if prompt.get('acked'):
            return True
        if not handoff_files.verify_ack(self._reply_text(handoff['new_chat_id'], prompt), prompt['token']):
            self._event('main_handoff_ack_missing', 'New Main did not send the ack token; old Main stays active',
                        handoff_id=handoff['id'])
            return False
        return self._repoint_after_ack(handoff, prompt)

    def _repoint_after_ack(self, handoff, prompt):
        new_worker = self._worker(handoff['new_chat_id'])
        old_worker = self._worker(handoff['old_chat_id'])
        if not new_worker:
            self._event('main_handoff_repoint_pending', 'New Main not in the worker list yet',
                        handoff_id=handoff['id'])
            return False
        new_worker.update(purpose='main', enabled=True, welcome=True)
        records = [('workers', new_worker)]
        if old_worker and old_worker['id'] != new_worker['id']:
            old_worker.update(purpose='archived_main', enabled=False)
            records.append(('workers', old_worker))
            for chat in self.store.all('chats'):
                if chat.get('main_worker') == old_worker['id']:
                    chat.update(main_worker=new_worker['id'], predecessor_worker=old_worker['id'])
                    records.append(('chats', chat))
        deletes = []
        main_chat = self.store.get('chats', self.main_chat_id) if self.main_chat_id else None
        if main_chat is not None:
            # The operator's existing Main chat record always follows the new Main.
            done = next((doc for kind, doc in records if kind == 'chats' and doc['id'] == main_chat['id']), None)
            if done is None and main_chat.get('main_worker') != new_worker['id']:
                main_chat.update(main_worker=new_worker['id'],
                                 predecessor_worker=main_chat.get('main_worker', ''))
                records.append(('chats', main_chat))
            for chat in self.store.all('chats'):
                if (chat.get('main_worker') == new_worker['id'] and chat['id'] != main_chat['id']
                        and str(chat['id']).startswith(CREATED_CHAT_PREFIX)):
                    deletes.append(('chats', chat['id']))
        account = self.store.get('accounts', self.account_id)
        if account is not None:
            ledger = account.get('team_threads') or {}
            ledger['main'] = {'id': handoff['new_chat_id'], 'pending': False}
            account['team_threads'] = ledger
            records.append(('accounts', account))
        prompt.update(acked=True, acked_at=self.now())
        records.append((PROMPTS, prompt))
        self.store.batch(records, deletes)
        try:
            handoff_files.mark_acked(self.folder(), self.file_id(), self.now())
        except handoff_files.UnknownWorkerError:
            pass
        self._event('main_handoff_acked', 'Ack confirmed; Main repointed to the new chat', handoff_id=handoff['id'])
        return True


def _current_main(store, account_id):
    account = store.get('accounts', account_id) or {}
    ledger_id = ((account.get('team_threads') or {}).get('main') or {}).get('id', '')
    workers = [w for w in store.all('workers') if w.get('account_id') == account_id]
    chats = store.all('chats')
    pointed = {c.get('main_worker') for c in chats}
    live = [w for w in workers if w.get('purpose') == 'main' and w.get('enabled')]
    # The Main is the enabled Main a chat record points to; the ledger only breaks ties (it can be stale).
    main = (next((w for w in live if w['id'] in pointed and w.get('thread_id') == ledger_id), None)
            or next((w for w in live if w['id'] in pointed and not _created_only(chats, w['id'])), None)
            or next((w for w in live if w['id'] in pointed), None)
            or next((w for w in workers if ledger_id and w.get('thread_id') == ledger_id), None)
            or next(iter(live), None))
    if not main:
        return None, None
    mine = [c for c in chats if c.get('main_worker') == main['id']]
    chat = next((c for c in mine if not str(c.get('id', '')).startswith(CREATED_CHAT_PREFIX)), None) or next(iter(mine), None)
    return main, chat


def _created_only(chats, worker_id):
    mine = [c for c in chats if c.get('main_worker') == worker_id]
    return bool(mine) and all(str(c.get('id', '')).startswith(CREATED_CHAT_PREFIX) for c in mine)


def run_rotation(store, adapter, account_id, emit, clock=None, mains=None):
    """Start or resume the account's Main handoff; emit sends main_handoff_<state> for every state."""
    emit = emit or (lambda kind, message='', **fields: None)
    view = _EmitStore(store, emit)
    active = next((h for h in store.all('main_handoffs') if h.get('account_id') == account_id
                   and h.get('state') in mh.ACTIVE), None)
    main, chat = _current_main(store, account_id)
    old = active['old_chat_id'] if active else (main or {}).get('thread_id', '')
    ops = MainHandoffOps(view, adapter, account_id, old_chat_id=old, main_chat_id=(chat or {}).get('id', ''),
                         mains=mains, clock=clock)
    machine = mh.MainHandoff(view, ops)
    if active:
        doc = active
    else:
        if not old:
            return dict(state='skipped', reason='no Main chat for this account')
        if (chat or {}).get('user_named') or is_protected(ops._threads().get(old)):
            return dict(state='skipped', reason='Main is a named chat')
        doc = machine.start(account_id, old)
        if not doc:
            return dict(state='skipped', reason='handoff not started')
    acked = False
    for _ in range(MAX_STEPS):
        doc = store.get('main_handoffs', doc['id'])
        state = doc.get('state')
        if state in ('done', 'failed'):
            break
        try:
            if state == 'prompt_sent':
                acked = ops.confirm_ack(doc)
                if not acked:
                    break
            nxt = machine.step(doc['id'])
        except Exception as exc:
            emit('main_handoff_error', str(exc)[:200], account_id=account_id, handoff_id=doc['id'])
            return dict(handoff_id=doc['id'], state=state, error=str(exc)[:200])
        if not nxt or nxt.get('state') == state:
            break
    doc = store.get('main_handoffs', doc['id'])
    return dict(handoff_id=doc['id'], state=doc.get('state'), acked=acked or doc.get('state') in ('old_archived', 'done'),
                old_chat_id=doc.get('old_chat_id'), new_chat_id=doc.get('new_chat_id', ''))


def sweep_archives(store, adapter, clock=None):
    """Delete archived Mains whose DELETE_DELAY passed; everything else is left alone."""
    now = (clock or time.time)()
    result = dict(checked=0, deleted=0, errors=0)
    for doc in store.all('main_handoffs'):
        if doc.get('state') != 'done' or doc.get('deleted') or doc.get('delete_unavailable'):
            continue
        if not mh.due_for_delete(doc, now):
            continue
        result['checked'] += 1
        ops = MainHandoffOps(store, adapter, doc['account_id'], old_chat_id=doc['old_chat_id'], clock=clock)
        try:
            after = mh.MainHandoff(store, ops).step(doc['id'])
            result['deleted'] += bool(after and after.get('deleted'))
        except Exception as exc:
            result['errors'] += 1
            store.event('main_archive_delete_failed', message=str(exc)[:200], account_id=doc['account_id'],
                        handoff_id=doc['id'])
    return result
