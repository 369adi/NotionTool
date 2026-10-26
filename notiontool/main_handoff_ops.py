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
from .agent_guard import GUARD_RULE
from .cowork_names import is_pool_title

PROTECTED_TITLES = ('NotionTool', 'Sonstiges', 'Studio432', 'Roblox')
ARCHIVE_PREFIX = 'Main (Archive '
LEGACY_OLD_OPERATOR_PREFIX = 'Main (old, '  # titles before 2026-10-25
OLD_OPERATOR_PREFIX = 'Main (expired, '  # main_switch.OLD_TITLE: old operator Main after a switch (never auto-deleted)
PROMPTS = 'main_handoff_prompts'
WELCOMES = 'main_handoff_welcomes'
MAX_STEPS = 24
CREATED_CHAT_PREFIX = 'c_create_main_'  # chat records mains.run_create adds for a new Main
# Operator mode (main_switch.py): the operator Main moves to a fresh Welcome chat.
SWITCHES = 'main_switches'                 # main_switch.KIND
OPERATOR = 'operator'                      # operator_main.OPERATOR
ARCHIVED_OPERATOR = 'archived_operator'    # old operator worker: never in a pool, never watched
OPERATOR_CANDIDATE = 'operator_candidate'  # switch Welcome worker before the ack: never a pool Main
SWITCH_FILE_PREFIX = 'switch_'             # own handoff file per switch; never main_<account>
OPERATOR_FIRST_LINES = ('You are the new Main (operator) of NotionTool.\n'
                        'Read the brief file %s and project-memory\\NotionTool.md with the NotionTool '
                        'read_file tool; answer with the check word (the ack token below).\n' + GUARD_RULE + '\n')


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
    supports_operator = False  # main_switch.advance creates chats only when this is True

    def __init__(self, store, adapter, account_id, old_chat_id='', main_chat_id='', mains=None, clock=None,
                 operator=False, switch_id='', pool=False):
        self.store, self.adapter, self.account_id = store, adapter, account_id
        self.old_chat_id = old_chat_id      # Notion thread of the current Main
        self.main_chat_id = main_chat_id    # local chat record of that Main (messages, repoint target)
        self._mains = mains
        self._clock = clock or time.time
        # Operator mode: the Main moved is the operator Main of a main_switches record, not a pool Main.
        self.operator = bool(operator)
        self.switch_id = str(switch_id or '')
        # Pool-Main switch (main_switch mode 'pool'): the same safe switch flow as the operator (candidate
        # Welcome chat, wait for its welcome turn, ack after the turn ended), but after the ack the new chat
        # becomes a pool Main and the old one 'archived_main' (rule: every Main has every Main feature).
        self.pool = bool(pool)
        if self.operator:
            self.supports_operator = True

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
        if self.operator:
            ident = self.switch_id or str(self.account_id)
            return (SWITCH_FILE_PREFIX + re.sub(r'[^A-Za-z0-9_-]', '_', ident))[:64]
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

    def _old_operator_of_switch(self, chat_id):
        return any(s.get('operator_thread') == chat_id and s.get('account_id') == self.account_id
                   for s in self.store.all(SWITCHES))

    def operator_first_message(self, record):
        """First message of an operator switch: the shared handoff prompt plus who the new chat is."""
        switch = self.store.get(SWITCHES, self.switch_id) if self.switch_id else None
        brief = (switch or {}).get('brief_path') or 'named in resume_brief'
        return OPERATOR_FIRST_LINES % brief + handoff_files.first_message(record)

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
            extra = dict(purpose=OPERATOR_CANDIDATE) if self.operator else {}
            op, _created = mains.request_create(account_id, request_id, predecessor=self.main_chat_id, **extra)
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
        elif self.operator and self._old_operator_of_switch(chat_id) and str(title).startswith((ARCHIVE_PREFIX, OLD_OPERATOR_PREFIX, LEGACY_OLD_OPERATOR_PREFIX)):
            reason = ''
        elif self._our_welcome(chat_id):
            reason = '' if title == 'Main' else 'Welcome chat may only become Main'
        elif self._old_main_of_handoff(chat_id):
            if not str(title).startswith(ARCHIVE_PREFIX):
                reason = 'old Main may only get an archive title'
            elif not (current.startswith('Main') or is_pool_title(current) or current.lower().startswith('welcome')):
                reason = 'old Main has a custom title'
        elif self.operator and self._old_operator_of_switch(chat_id):
            if not str(title).startswith((ARCHIVE_PREFIX, OLD_OPERATOR_PREFIX, LEGACY_OLD_OPERATOR_PREFIX)):
                reason = 'old operator Main may only get an archive title'
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

    def chat_busy(self, chat, transcript=None):
        """True while Notion still runs a turn in the chat (same idle test as notion.py)."""
        if transcript is None:
            transcript = self._client().transcript(chat) or {}
        session = transcript.get('session') or {}
        return bool(transcript.get('session_pending')) or session.get('status') not in (None, 'idle', 'completed')

    def welcome_settled(self, chat):
        """The fresh Welcome chat finished its own onboarding turn ."""
        # Busy test only: Notion reports the running onboarding turn as a pending session.
        return not self.chat_busy(chat)

    def send_prompt(self, chat, text, fingerprint):
        if self.has_prompt(chat, fingerprint):
            return False
        if self.operator and not self.welcome_settled(chat):
            self._event('main_switch_wait_welcome', 'New chat still runs its welcome turn; brief not sent yet',
                        chat=chat)
            return False
        record = handoff_files.write_handoff(self.folder(), self.file_id(), dict(
            kind='main_handoff', account_id=self.account_id, handoff={}, old_thread=self.old_chat_id,
            new_thread=chat), self.now())  # returns the un-acked file from summarize unchanged
        if self.operator:
            text = self.operator_first_message(record)  # same file and token as main_switch wrote
        client = self._client()
        transcript = client.transcript(chat) or {}
        from . import foreign_chat_guard  # RBX review: one age check on every send path (before the record)
        foreign_chat_guard.require_young(self.store, self.adapter, chat, None, str(self.account_id or ''))
        step_id = self.adapter.new_id(client.info['space_id'])
        doc = dict(id=fingerprint, chat_id=chat, account_id=self.account_id, token=record['token'],
                   path=record['path'], step_id=step_id, state='attempted', acked=False,
                   before_seq=(transcript.get('session') or {}).get('sequence', 0), at=self.now())
        self.store.put(PROMPTS, doc)  # recorded before sending: a crash never leads to a resend
        from .notion import user_config
        # Main chats keep the user's own model and reasoning; never Auto, never the model default.
        model, effort = user_config(client, chat)
        body = self.adapter.body(client, chat, step_id, text, model, computer=True, effort=effort)
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
    def _reply_text(self, chat, prompt, transcript=None):
        if transcript is None:
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
        transcript = self._client().transcript(handoff['new_chat_id']) or {}
        if self.operator and self.chat_busy(handoff['new_chat_id'], transcript):
            # The ack counts only after the new Main's turn ended: on 2026-10-20 the switch completed while
            # the new Main was still mid-turn, and that turn never finished.
            self._event('main_handoff_ack_wait', 'New Main is still answering; old Main stays active',
                        handoff_id=handoff['id'])
            return False
        if not handoff_files.verify_ack(self._reply_text(handoff['new_chat_id'], prompt, transcript), prompt['token']):
            self._event('main_handoff_ack_missing', 'New Main did not send the ack token; old Main stays active',
                        handoff_id=handoff['id'])
            return False
        return self._repoint_after_ack(handoff, prompt)

    def _repoint_after_ack(self, handoff, prompt):
        if self.operator:
            if not self.pool:
                return self._repoint_operator(handoff, prompt)
        new_worker = self._worker(handoff['new_chat_id'])
        old_worker = self._worker(handoff['old_chat_id'])
        if not new_worker:
            self._event('main_handoff_repoint_pending', 'New Main not in the worker list yet',
                        handoff_id=handoff['id'])
            return False
        new_worker.update(purpose='main', enabled=True, welcome=True,
                          created_at=new_worker.get('created_at') or self.now())  # chat age starts fresh
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

    def _repoint_operator(self, handoff, prompt):
        """Operator switch after the ack: the new chat becomes the operator Main, never a pool Main."""
        new_worker = self._worker(handoff['new_chat_id'])
        old_worker = self._worker(handoff['old_chat_id'])
        if not new_worker:
            self._event('main_switch_repoint_pending', 'New operator Main not in the worker list yet',
                        handoff_id=handoff['id'])
            return False
        chats = self.store.all('chats')
        mine = self.store.get('chats', self.main_chat_id) if self.main_chat_id else None
        chat = (mine if mine and mine.get('operator') else None) or next(
            (c for c in chats if c.get('operator') and old_worker and c.get('main_worker') == old_worker['id']), None)
        if not chat or chat.get('state') == 'released':
            self._event('main_switch_repoint_pending', 'No active operator chat record to repoint',
                        handoff_id=handoff['id'])
            return False
        new_worker.update(purpose=OPERATOR, enabled=False, state='ready')
        records = [('workers', new_worker)]
        if old_worker and old_worker['id'] != new_worker['id']:
            old_worker.update(purpose=ARCHIVED_OPERATOR, enabled=False)
            records.append(('workers', old_worker))
        if chat.get('main_worker') != new_worker['id']:
            chat.update(main_worker=new_worker['id'], predecessor_worker=chat.get('main_worker', ''))
        chat['operator'] = True
        if 'thread_id' in chat:
            chat['thread_id'] = handoff['new_chat_id']
        records.append(('chats', chat))
        deletes = [('chats', c['id']) for c in chats if c.get('main_worker') == new_worker['id']
                   and c['id'] != chat['id'] and str(c['id']).startswith(CREATED_CHAT_PREFIX)]
        # No resume-heading step for switches: first_task_done keeps sweep_archives off this prompt.
        prompt.update(acked=True, acked_at=self.now(), first_task_done=True, switch=True)
        records.append((PROMPTS, prompt))
        self.store.batch(records, deletes)  # the pool Main ledger (team_threads.main) is left alone
        # Rename the old operator Main thread in Notion so it no longer shows as 'Main'.
        if old_worker and old_worker['id'] != new_worker['id']:
            old_tid = old_worker.get('thread_id')
            if old_tid:
                try:
                    import time as _time
                    from datetime import datetime as _dt
                    date_str = _dt.utcfromtimestamp(_time.time()).strftime('%Y-%m-%d')
                    self.adapter.rename_thread(self._client(), old_tid,
                                              'Main (Archive %s)' % date_str)
                    self._event('main_switch_old_renamed', 'Old operator Main renamed to archive',
                                handoff_id=handoff['id'], thread_id=old_tid)
                except Exception as exc:
                    self._event('main_switch_old_rename_failed', str(exc)[:200],
                                handoff_id=handoff['id'])
        try:
            handoff_files.mark_acked(self.folder(), self.file_id(), self.now())
        except handoff_files.UnknownWorkerError:
            pass
        self._event('main_switch_acked', 'Ack confirmed; operator Main moved to the new chat',
                    handoff_id=handoff['id'])
        return True

    # ---- first resumed reply ---------------------------------------------
    def confirm_first_reply(self, prompt):
        """After the ack, the first Main reply with the resume headings marks the handoff file done. Once only."""
        if not prompt or not prompt.get('acked') or prompt.get('first_task_done'):
            return False
        text = self._reply_text(prompt['chat_id'], prompt) or ''
        if not any(heading in text for heading in RESUME_HEADINGS):
            return False
        prompt.update(first_task_done=True, first_task_done_at=self.now())
        self.store.put(PROMPTS, prompt)  # recorded first: the file is never marked twice
        try:
            handoff_files.mark_first_task_done(self.folder(), self.file_id(), self.now())
        except handoff_files.UnknownWorkerError:
            pass
        self._event('main_handoff_first_reply', 'First resumed Main reply seen; handoff file marked done',
                    handoff_id=str(prompt.get('id', '')).rsplit(':prompt', 1)[0])
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


def rollback_switch(store, adapter, switch_id):
    """Undo a 'switched' operator switch whose new Main is broken or gone: the old chat is the Main again."""
    record = store.get(SWITCHES, str(switch_id or ''))
    if not record or record.get('state') != 'switched':
        raise ValueError('Only a switched Main switch can be rolled back')
    account = record.get('account_id', '')
    workers = {w.get('thread_id'): w for w in store.all('workers') if w.get('account_id') == account}
    old, new = workers.get(record.get('operator_thread')), workers.get(record.get('new_chat_id'))
    chat = store.get('chats', record.get('operator_chat_id', ''))
    if not old or not chat or not chat.get('operator'):
        raise ValueError('Old operator worker or operator chat record not found')
    old.update(purpose=OPERATOR, enabled=False, state='ready')
    records = [('workers', old)]
    if new and new['id'] != old['id']:
        new.update(purpose=ARCHIVED_OPERATOR, enabled=False)
        records.append(('workers', new))
    chat.update(main_worker=old['id'], predecessor_worker=chat.get('main_worker', ''), operator=True)
    if 'thread_id' in chat:
        chat['thread_id'] = record['operator_thread']
    records.append(('chats', chat))
    record.update(state='rolled_back', updated=time.time(),
                  reason='rolled back: the old operator Main is active again')
    records.append((SWITCHES, record))
    store.batch(records, [])
    renamed = False
    try:
        adapter.rename_thread(adapter.client(account), record['operator_thread'], 'Main')
        renamed = True
    except Exception as exc:
        store.event('main_switch_rollback_rename_failed', message=str(exc)[:200], switch_id=record['id'])
    store.event('main_switch_rolled_back', message='Old operator Main is active again', switch_id=record['id'])
    return dict(record, ok=True, renamed=renamed)


def switch_ops(store, adapter, record, clock=None, mains=None):
    """Operator-mode ops for one main_switches record (use as main_switch.sweep's ops_for)."""
    record = record or {}
    return MainHandoffOps(store, adapter, record.get('account_id', ''), old_chat_id=record.get('operator_thread', ''),
                          main_chat_id=record.get('operator_chat_id', ''), mains=mains, clock=clock,
                          operator=True, switch_id=record.get('id', ''), pool=record.get('mode') == 'pool')


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
    for prompt in store.all(PROMPTS):
        # Phase I4: mark the Main handoff file done after the first resumed reply (deletes it once acked).
        if not prompt.get('acked') or prompt.get('first_task_done') or not prompt.get('account_id'):
            continue
        ops = MainHandoffOps(store, adapter, prompt['account_id'], clock=clock)
        try:
            if ops.confirm_first_reply(prompt):
                result['first_done'] = result.get('first_done', 0) + 1
        except Exception as exc:
            result['errors'] += 1
            store.event('main_handoff_first_reply_failed', message=str(exc)[:200], account_id=prompt['account_id'])
    return result


# Expired chats (periodic sweep) ------------------------------------------------------------------
# Before this sweep an old Main was renamed only inside a running switch (main_switch._advance, the
# operator_thread of a switch record) or handoff (old_chat_id). A Main whose worker record is gone
# (worker_state 'missing') or that never became the operator Main never got a switch or handoff, so
# its chat record stayed 'active' with its old title forever (bug: 'Main 20', ~47 h old).
EXPIRED_SUFFIX = ' (expired)'
EXPIRED_STATE = 'expired'
EXPIRED_MAX_HOURS = 22  # never later than 22 h, even when coworker_max_age_hours is set higher
_EXPIRED_SKIP_STATES = ('released', 'retired')
_COWORKER_PURPOSES = ('worker', 'retired_worker')  # old Coworker chats: retire/delete flow only
_ARCHIVE_TITLES = (ARCHIVE_PREFIX, LEGACY_OLD_OPERATOR_PREFIX, 'Main (expired')


def expired_title(title):
    """'<title> (expired)' exactly once; archive titles of earlier switches stay as they are."""
    text = ' '.join(str(title or '').split()) or 'Main'
    if text.lower().endswith('(expired)') or text.startswith(_ARCHIVE_TITLES):
        return text
    return text + EXPIRED_SUFFIX


def _created_op(store, chat):
    """The create_main operation behind a 'c_create_main_<request>' chat record ({} for other chats)."""
    ident = str(chat.get('id') or '')
    if not ident.startswith(CREATED_CHAT_PREFIX):
        return {}
    return store.get('operations', 'create_main:' + ident[len(CREATED_CHAT_PREFIX):]) or {}


def _expiry_exempt(store):
    """(chat ids, thread ids) that never expire here: each account's current Main, the live operator
    Main record, and chats an open Main switch or handoff is still moving (that flow renames them)."""
    from . import main_switch
    chats, threads = set(), set()
    workers = store.all('workers')
    accounts = {w.get('account_id') for w in workers if w.get('account_id')}
    accounts.update(a.get('id') for a in store.all('accounts') if a.get('id'))
    for account in accounts:
        main, chat = _current_main(store, account)
        if chat:
            chats.add(chat.get('id'))
        if main:
            threads.add(main.get('thread_id'))
    by_id = {w.get('id'): w for w in workers}
    for chat in store.all('chats'):
        worker = by_id.get(chat.get('main_worker')) or {}
        if chat.get('operator') and worker.get('purpose') == OPERATOR:
            chats.add(chat.get('id'))
            threads.add(worker.get('thread_id'))
    for record in store.all(SWITCHES):
        if record.get('state') in main_switch.ACTIVE:
            chats.add(record.get('operator_chat_id'))
            threads.add(record.get('operator_thread'))
    for doc in store.all('main_handoffs'):
        if doc.get('state') in mh.ACTIVE:
            threads.add(doc.get('old_chat_id'))
    return chats - {None, ''}, threads - {None, ''}


def _remote_threads(store, adapter, account_id, thread_ids):
    """(listed {thread: title} or None when unreadable, thread records, client) for one account."""
    if (store.get('accounts', account_id) or {}).get('state') != 'ready':
        return None, {}, None
    try:
        client = adapter.client(account_id)
        listed = {_tid(t): str(t.get('title') or '') for t in (client.threads() or []) if _tid(t)}
    except Exception:
        return None, {}, None
    try:
        records = adapter.thread_records(client, list(thread_ids)) or {}
    except Exception:
        records = {}
    return listed, records, client


def sweep_expired(store, adapter, now=None, settings=None):
    """Expire every Main or user-named chat whose Notion thread is >= 22 h old and that is not the
    current active Main: record title '<title> (expired)', state 'expired', and the Notion thread title
    too while the thread still exists. Coworker chats are left to the retire/delete flow. Idempotent;
    a failed Notion rename is retried at the next sweep. Never raises for a single chat.
    """
    from .team_rotation import epoch, max_age_hours
    now = time.time() if now is None else float(now)
    if settings is None:
        settings = store.settings() if callable(getattr(store, 'settings', None)) else {}
    limit = min(max_age_hours(settings), EXPIRED_MAX_HOURS) * 3600
    result = dict(checked=0, renamed=0, threads_renamed=0, errors=0)
    workers = {w.get('id'): w for w in store.all('workers')}
    exempt_chats, exempt_threads = _expiry_exempt(store)
    todo = []
    for chat in store.all('chats'):
        if chat.get('id') in exempt_chats or chat.get('state') in _EXPIRED_SKIP_STATES:
            continue
        if chat.get('state') == EXPIRED_STATE and chat.get('expired_thread_done'):
            continue
        worker = workers.get(chat.get('main_worker')) or {}
        if worker.get('purpose') in _COWORKER_PURPOSES and not chat.get('user_named'):
            continue
        if is_protected(chat.get('title')):
            continue
        op = _created_op(store, chat)
        thread = worker.get('thread_id') or chat.get('thread_id') or op.get('thread_id') or ''
        if thread and thread in exempt_threads:
            continue
        account = worker.get('account_id') or chat.get('account_id') or op.get('account_id') or ''
        todo.append((chat, worker, thread, account))
    if not todo:
        return result
    remote = {}
    for account in {a for _c, _w, t, a in todo if a and t}:
        remote[account] = _remote_threads(store, adapter, account, [t for _c, _w, t, a in todo if a == account and t])
    for chat, worker, thread, account in todo:
        result['checked'] += 1
        try:
            listed, records, client = remote.get(account) or (None, {}, None)
            if chat.get('state') != EXPIRED_STATE:
                record = records.get(thread) or {}
                created = (epoch(record.get('created_time') or record.get('createdTime') or record.get('created_at'))
                           or epoch(worker.get('created_at') or worker.get('thread_created_at'))
                           or epoch(chat.get('created')))
                if not created or now - created < limit:
                    continue
            notion_title = listed.get(thread) if (listed is not None and thread) else None
            if notion_title is not None and is_protected(notion_title):
                continue
            title = expired_title(chat.get('title'))
            changed = chat.get('state') != EXPIRED_STATE or chat.get('title') != title
            if not thread:
                done = True  # no Notion thread known: only the record can expire
            elif listed is None:
                done = False  # chat list unreadable now: rename the thread at the next sweep
            elif notion_title is None:
                done = True  # thread no longer listed in Notion: nothing to rename there
            else:
                wanted = expired_title(notion_title or chat.get('title'))
                done = True
                if wanted != notion_title:
                    try:
                        adapter.rename_thread(client, thread, wanted)
                        result['threads_renamed'] += 1
                    except Exception as exc:
                        done = False
                        store.event('chat_expired_rename_failed', message=str(exc)[:200], chat_id=chat.get('id', ''),
                                    account_id=account)
            if changed or done != bool(chat.get('expired_thread_done')):
                chat.update(title=title, state=EXPIRED_STATE, expired_at=chat.get('expired_at') or now,
                            expired_thread_done=done)
                store.put('chats', chat)
            if changed:
                result['renamed'] += 1
                store.event('chat_expired', message='Chat older than %d h renamed to %s' % (limit // 3600, title),
                            chat_id=chat.get('id', ''), account_id=account)
        except Exception as exc:
            result['errors'] += 1
            store.event('chat_expire_failed', message=str(exc)[:200], chat_id=chat.get('id', ''), account_id=account)
    return result
