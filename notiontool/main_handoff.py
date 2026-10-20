"""Main handoff: move the operator Main into a fresh Welcome chat instead of compressing in place.

Pure decision functions plus a persisted, resumable state machine (store kind 'main_handoffs').
All remote work goes through injected ops; nothing here polls or calls Notion directly.
"""
import time

from .cowork_names import is_unnamed_welcome

STATES = ('planned', 'summary_requested', 'summary_done', 'chat_pending', 'chat_created',
          'renamed_main', 'prompt_sent', 'old_archived', 'done', 'failed')
ACTIVE = set(STATES) - {'done', 'failed'}
DELETE_DELAY = 3600
OPEN_RUN_STATES = ('planning', 'executing', 'reviewing', 'fixing', 'integrating', 'queued', 'blocked', 'paused')


def should_rotate(main, settings):
    """True only for an idle Main without a running turn that reached the turn limit or asked for compress."""
    if main.get('turn_running') or not main.get('idle'):
        return False
    limit = int((settings or {}).get('main_rotate_turns') or 60)
    return bool(main.get('compress_requested')) or int(main.get('turns_since_main') or 0) >= limit


def _messages(store, chat_id, max_chars):
    page = getattr(store, 'message_page', None)
    rows = (page(chat_id, limit=200) or {}).get('messages', []) if page else []
    picked, total = [], 0
    for row in reversed(rows):
        size = len(row.get('text') or '')
        if total + size > max_chars:
            break
        picked.append(dict(role=row.get('role', ''), text=row.get('text') or ''))
        total += size
    return list(reversed(picked))


def build_handoff_sources(store, account_id, main_chat_id, settings, memory_tail=''):
    """Durable sources only: memory tail, open runs with tasks, file claims, newest Main messages."""
    max_chars = int((settings or {}).get('main_handoff_chars') or 12000)
    runs = [r for r in store.all('runs') if r.get('state') in OPEN_RUN_STATES
            and str(r.get('main_worker', '')).split(':', 1)[0] == account_id]
    ids = {r['id'] for r in runs}
    tasks = [t for t in store.all('tasks') if t.get('run_id') in ids]
    open_runs = [dict(id=r['id'], state=r.get('state'), goal=(r.get('goal') or '')[:400],
                      tasks=[dict(id=t['id'], role=t.get('role'), state=t.get('state'), title=t.get('title'))
                             for t in tasks if t.get('run_id') == r['id']]) for r in runs]
    claims = [dict(path=c.get('path'), owner=c.get('owner')) for c in store.all('file_claims')]
    return dict(memory_tail=(memory_tail or '')[-4000:], open_runs=open_runs, claims=claims,
                messages=_messages(store, main_chat_id, max_chars))


def archive_title(ts):
    return 'Main (Archive %s)' % time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))


def due_for_delete(handoff, now):
    archived = handoff.get('archived_at')
    return archived is not None and now >= float(archived) + DELETE_DELAY


class MainHandoff:
    def __init__(self, store, ops):
        self.store = store
        self.ops = ops

    def _now(self):
        now = getattr(self.ops, 'now', None)
        return now() if now else time.time()

    def _settings(self):
        settings = getattr(self.store, 'settings', None)
        return settings() if callable(settings) else {}

    def _move(self, doc, state, **fields):
        doc.update(fields, state=state, updated=self._now())
        self.store.put('main_handoffs', doc)
        self.store.event('main_handoff_' + state, message='Main handoff: ' + state,
                         account_id=doc['account_id'], handoff_id=doc['id'])
        return doc

    def _user_named(self, chat_id):
        return bool((self.store.get('chats', chat_id) or {}).get('user_named'))

    def start(self, account_id, main_chat_id):
        if any(h.get('account_id') == account_id and h.get('state') in ACTIVE for h in self.store.all('main_handoffs')):
            return None
        if self._user_named(main_chat_id):
            return None
        now = self._now()
        doc = dict(id='mh_%s_%d' % (account_id, int(now * 1000)), account_id=account_id,
                   old_chat_id=main_chat_id, created=now)
        return self._move(doc, 'planned')

    def archived_chat_ids(self, account_id):
        return [h['old_chat_id'] for h in self.store.all('main_handoffs')
                if h.get('account_id') == account_id and h.get('state') in ('old_archived', 'done')]

    def _find_new_chat(self, doc):
        known = set(doc.get('known_chat_ids') or ())
        chats = self.ops.list_chats(doc['account_id']) or []
        for chat in chats:
            if chat.get('request_id') == doc['create_request_id']:
                return chat['id']
        fresh = [c for c in chats if c['id'] not in known and is_unnamed_welcome(str(c.get('title', '')))]
        return fresh[0]['id'] if len(fresh) == 1 else None

    def step(self, handoff_id):
        doc = self.store.get('main_handoffs', handoff_id)
        if not doc:
            return None
        state = doc.get('state')
        if state == 'planned':
            return self._move(doc, 'summary_requested')
        if state == 'summary_requested':
            sources = build_handoff_sources(self.store, doc['account_id'], doc['old_chat_id'], self._settings())
            return self._move(doc, 'summary_done', summary=self.ops.summarize(sources) or '')
        if state == 'summary_done':
            known = [c['id'] for c in (self.ops.list_chats(doc['account_id']) or [])]
            return self._move(doc, 'chat_pending', known_chat_ids=known, create_attempted=False,
                              create_request_id=doc['id'] + ':create')
        if state == 'chat_pending':
            chat_id = None
            if not doc.get('create_attempted'):
                doc['create_attempted'] = True
                self.store.put('main_handoffs', doc)
                result = self.ops.create_welcome(doc['account_id'], doc['create_request_id']) or {}
                if result.get('status') == 'ok' and result.get('chat_id'):
                    chat_id = result['chat_id']
            if not chat_id:
                chat_id = self._find_new_chat(doc)
            if not chat_id:
                return doc
            return self._move(doc, 'chat_created', new_chat_id=chat_id)
        if state == 'chat_created':
            self.ops.rename_chat(doc['new_chat_id'], 'Main')
            return self._move(doc, 'renamed_main')
        if state == 'renamed_main':
            fingerprint = doc['id'] + ':prompt'
            has_prompt = getattr(self.ops, 'has_prompt', None)
            delivered = bool(has_prompt and has_prompt(doc['new_chat_id'], fingerprint))
            if not delivered and doc.get('prompt_attempted'):
                if has_prompt:
                    return self._move(doc, 'failed', error='Handoff prompt delivery is unclear; not resent')
                delivered = True
            if not delivered:
                doc['prompt_attempted'] = True
                self.store.put('main_handoffs', doc)
                last = self.ops.last_user_prompt(doc['old_chat_id']) or ''
                text = (doc.get('summary') or '') + ('\n\nLast open request:\n' + last if last else '')
                self.ops.send_prompt(doc['new_chat_id'], text, fingerprint)
            return self._move(doc, 'prompt_sent', prompt_fingerprint=fingerprint)
        if state == 'prompt_sent':
            now = self._now()
            if not self._user_named(doc['old_chat_id']):
                self.ops.rename_chat(doc['old_chat_id'], archive_title(now))
            chat = self.store.get('chats', doc['old_chat_id'])
            if chat:
                chat.update(archived=True, archived_at=now)
                self.store.put('chats', chat)
            return self._move(doc, 'old_archived', archived_at=now, delete_after=now + DELETE_DELAY)
        if state == 'old_archived':
            return self._move(doc, 'done')
        if state == 'done' and due_for_delete(doc, self._now()) and not doc.get('deleted') and not doc.get('delete_unavailable'):
            delete = getattr(self.ops, 'delete_chat', None)
            if self._user_named(doc['old_chat_id']):
                doc['delete_unavailable'] = True
            elif callable(delete):
                doc['deleted'] = True
                self.store.put('main_handoffs', doc)
                delete(doc['old_chat_id'])
                self.store.event('main_archive_deleted', message='Archived Main deleted',
                                 account_id=doc['account_id'], handoff_id=doc['id'])
                return doc
            else:
                doc['delete_unavailable'] = True
                self.store.event('main_archive_delete_unavailable', message='No delete op; archived Main kept',
                                 account_id=doc['account_id'], handoff_id=doc['id'])
            self.store.put('main_handoffs', doc)
        return doc
