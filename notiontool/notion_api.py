"""Clean client for the internal Notion API (/api/v3) of the standalone NotionTool window.

The window (main.py, app_ui_mode 'standalone') shows only NotionTool's own UI (notiontool/web/app). Every
action runs as a background request through NotionAdapter.client(account_id): the saved token_v2 session of
the account (SessionStore, data/sessions), the Edge transport of the selected account (R4) and the shared
NetworkBudget (I11). The endpoints are the ones notion.py already uses:

  loadUserContent {}                                   user and workspaces (NotionClient.identity)
  getInferenceTranscriptsForUser {threadParentPointer, limit, includeWriterChats}   chat list
  syncRecordValuesSpaceInitial {requests: [thread pointers]}  chat titles and times (thread records)
  getThreadTranscript {spaceId, threadId, direction, limit}   chat history (NotionClient.transcript)
  runInferenceTranscript (NotionAdapter.body)          send; NDJSON stream of patch-start / patch-sync / patch
  markInferenceTranscriptSeen {spaceId, threadId}      mark a chat as read
"""
import datetime
import json
import threading
import time
import uuid

from .notion import apply_patch, entity_text, stream_text, text_content, unwrap

CHAT_LIMIT = 50
HISTORY_LIMIT = 80
STREAM_TIMEOUT = 120
QUIET_S = 4.0  # no stream line for this long: ask Notion's thread record whether the turn already finished
MODEL_KEYS = ('opus', 'sonnet', 'haiku')
ROLES = {'user_message': 'user', 'assistant_message': 'assistant'}
TIME_KEYS = ('updatedAt', 'updated_time', 'lastEditedTime', 'last_edited_time', 'latest_user_or_trigger_time',
             'createdAt', 'created_time')


def thread_key(value):
    """Dashed lowercase thread id, '' for anything that is not a UUID."""
    try:
        return str(uuid.UUID(str(value or '').strip()))
    except (TypeError, ValueError, AttributeError):
        return ''


def item_id(item):
    if isinstance(item, dict):
        item = item.get('id') or item.get('threadId') or item.get('thread_id')
    return thread_key(item)


def space_pointer(space_id):
    return {'table': 'space', 'id': space_id, 'spaceId': space_id}


def millis(value):
    """Epoch milliseconds of a Notion time (ms, seconds or ISO text); 0 when unknown."""
    if isinstance(value, bool) or value in (None, ''):
        return 0
    if isinstance(value, (int, float)):
        return int(value if value > 1e11 else value * 1000)
    try:
        return int(datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp() * 1000)
    except ValueError:
        return 0


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def model_key(value, default='opus'):
    """'opus-5.5', 'Opus 5.5' or 'opus' -> 'opus'. Always an explicit model, never Auto."""
    text = str(value or '').strip().lower()
    return next((key for key in MODEL_KEYS if text.startswith(key)), default)


def record(value):
    """One thread record of a recordMap entry ({spaceId, value: {value: record}} is unwrapped too)."""
    rec = unwrap(value) if isinstance(value, dict) else {}
    if isinstance(rec, dict) and isinstance(rec.get('value'), dict) and 'id' in rec['value']:
        rec = rec['value']
    return rec if isinstance(rec, dict) else {}


def chat_title(*sources):
    for src in sources:
        if not isinstance(src, dict):
            continue
        data = src.get('data') if isinstance(src.get('data'), dict) else {}
        for value in (src.get('title'), data.get('title'), src.get('name')):
            text = text_content(value).strip()
            if text:
                return text
    return ''


def chat_time(*sources):
    return max([millis(src.get(key)) for src in sources if isinstance(src, dict) for key in TIME_KEYS] or [0])


def chat_rows(listing, records=None, space_id=''):
    """Chat list rows, newest first: the getInferenceTranscriptsForUser listing plus thread records.

    A thread known only from records (Main, Coworkers, own chats of the store) is listed when its record
    belongs to space_id. Trashed threads (alive false) and threads of another workspace are left out."""
    listing = listing if isinstance(listing, dict) else {}
    found = {}
    for source in (((listing.get('recordMap') or {}).get('thread') or {}), records or {}):
        for key, value in source.items():
            rec = record(value)
            if thread_key(key) and rec:
                found[thread_key(key)] = rec
    items = {}
    for item in listing.get('transcripts') or []:
        if item_id(item):
            items.setdefault(item_id(item), item if isinstance(item, dict) else {})
    for tid in listing.get('threadIds') or []:
        if thread_key(tid):
            items.setdefault(thread_key(tid), {})
    for tid, rec in found.items():
        if tid not in items and space_id and rec.get('space_id') == space_id:
            items[tid] = {}
    unread = {thread_key(t) for t in listing.get('unreadThreadIds') or []}
    rows = []
    for tid, item in items.items():
        rec = found.get(tid, {})
        if rec.get('alive') is False or (space_id and rec.get('space_id') and rec.get('space_id') != space_id):
            continue
        rows.append({'id': tid, 'title': (chat_title(item, rec) or 'New chat')[:200],
                     'updated': chat_time(item, rec), 'unread': tid in unread})
    rows.sort(key=lambda row: -row['updated'])
    return rows


def transcript_messages(transcript):
    """User and assistant messages of a getThreadTranscript result, oldest first (a later put of the
    same entry wins)."""
    entries = {}
    patches = transcript.get('patches') if isinstance(transcript, dict) else None
    for index, patch in enumerate(patches or []):
        entity = patch.get('entity') if isinstance(patch, dict) and patch.get('op') == 'put' else None
        role = ROLES.get(str(entity.get('kind') or '').lower()) if isinstance(entity, dict) else None
        if not role:
            continue
        key = str(entity.get('id') or 'm%d' % index)
        entries[key] = (role, entity, entries[key][2] if key in entries else index)
    out = []
    for key, (role, entity, index) in entries.items():
        text = entity_text(entity).strip()
        if text:
            out.append({'id': key, 'role': role, 'text': text, 'index': index,
                        'at': millis(entity.get('created_at') or entity.get('createdAt')),
                        'sequence': number(entity.get('sequence'))})
    out.sort(key=lambda m: (m['sequence'], m['at'], m['index']))
    for message in out:
        message.pop('index')
    return out


class StreamReply:
    """Folds runInferenceTranscript NDJSON lines into the reply (same rules as NotionAdapter's stream loop)."""

    def __init__(self):
        self.steps = []
        self.lines = 0

    def feed(self, line):
        """True when the line changed the steps. Blank keep-alive lines and non-JSON lines are skipped."""
        if isinstance(line, bytes):
            line = line.decode('utf-8', 'replace')
        line = str(line or '').strip()
        if not line:
            return False
        try:
            event = json.loads(line)
        except ValueError:
            return False
        if not isinstance(event, dict):
            return False
        self.lines += 1
        if event.get('type') in ('patch-start', 'patch-sync'):
            steps = (event.get('data') or {}).get('s')
            if isinstance(steps, list):
                self.steps = steps
            return True
        if event.get('type') == 'patch':
            self.steps = apply_patch(self.steps, event.get('v'))
            return True
        return False

    @property
    def text(self):
        return stream_text(self.steps)

    def error(self):
        for step in self.steps:
            if isinstance(step, dict) and step.get('type') == 'error':
                detail = step.get('message') or step.get('error') or step.get('value') or 'Notion reported an error'
                return str(detail if isinstance(detail, str) else json.dumps(detail, default=str))[:300]
        return ''


class NotionApi:
    """Background Notion requests of one account (token_v2 session, Edge transport, network budget)."""

    def __init__(self, adapter, account_id):
        self.adapter = adapter
        self.account_id = account_id

    def client(self):
        client = self.adapter.client(self.account_id)
        if not client.info.get('space_id'):
            raise ValueError('Choose a workspace for this account first.')
        return client

    def spaces(self):
        """(current space id, [{id, name}]) live from loadUserContent."""
        info = self.adapter.client(self.account_id).identity()
        return info.get('space_id') or '', [s for s in info.get('spaces') or [] if s.get('id')]

    def chats(self, known=(), limit=CHAT_LIMIT):
        """(space id, chat rows) of the current workspace. known = thread ids NotionTool already knows
        (Main, Coworkers); they are listed when their record belongs to this workspace."""
        client = self.client()
        space = client.info['space_id']
        try:
            listing = client.call('getInferenceTranscriptsForUser', {
                'threadParentPointer': space_pointer(space), 'limit': int(limit), 'includeWriterChats': False})
        except ValueError:
            listing = {}
        wanted = [item_id(item) for item in listing.get('transcripts') or []
                  if not (isinstance(item, dict) and chat_title(item) and chat_time(item))]
        wanted += [thread_key(t) for t in listing.get('threadIds') or []] + [thread_key(t) for t in known]
        wanted = [t for t in dict.fromkeys(wanted) if t][:150]
        try:
            records = self.adapter.thread_records(client, wanted) if wanted else {}
        except ValueError:
            records = {}
        return space, chat_rows(listing, records, space)

    def chat(self, thread_id, limit=HISTORY_LIMIT):
        client = self.client()
        transcript = client.transcript(thread_key(thread_id), limit=int(limit)) or {}
        session = transcript.get('session') if isinstance(transcript.get('session'), dict) else {}
        return {'messages': transcript_messages(transcript), 'status': str(session.get('status') or ''),
                'pending': bool(transcript.get('session_pending'))}

    def send(self, thread_id, text, create=False, model='opus', effort=None, on_text=None, cancel=None):
        """Send one user message and stream the reply: {thread_id, step_id, text, error, lines}.

        create=True starts a new chat with this id (createThread). Notion keeps the stream open after the
        turn finished (live 22 Oct 2026), so a quiet stream ends once the thread record shows a newer user
        turn than before this send and a completed time after it (evidence, never a time guess)."""
        thread, prompt = thread_key(thread_id), str(text or '').strip()
        if not thread:
            raise ValueError('Choose a chat first.')
        if not prompt:
            raise ValueError('Write a message first.')
        client = self.client()
        before = None if create else self._latest(client, thread)
        step_id = self.adapter.new_id(client.info['space_id'])
        body = self.adapter.body(client, thread, step_id, prompt, client.model(model_key(model)), create=create,
                                 computer=True, effort=effort)
        stop = cancel or threading.Event()
        reply, last, alive = StreamReply(), [time.monotonic()], threading.Event()
        alive.set()
        threading.Thread(target=self._quiet_watch, args=(client, thread, before or 0, last, alive, stop),
                         daemon=True, name='app-send-watch').start()
        try:
            for line in client.transport.stream('runInferenceTranscript', body, client.headers(),
                                                timeout=STREAM_TIMEOUT, cancel=stop):
                last[0] = time.monotonic()
                if reply.feed(line) and on_text:
                    on_text(reply.text)
                if stop.is_set():
                    break
        except Exception:
            if not (stop.is_set() and reply.text.strip()):
                raise  # a stream ended by the finished-turn check is a normal end
        finally:
            alive.clear()
        return {'thread_id': thread, 'step_id': step_id, 'text': reply.text, 'error': reply.error(),
                'lines': reply.lines}

    def mark_seen(self, thread_id):
        client = self.client()
        return client.call('markInferenceTranscriptSeen', {'spaceId': client.info['space_id'],
                                                           'threadId': thread_key(thread_id)})

    def _latest(self, client, thread):
        try:
            return self.adapter.turn_outcome(client, thread)[0]
        except Exception:  # noqa: BLE001 - unknown baseline: the watch then needs a turn after 0
            return 0

    def _quiet_watch(self, client, thread, before, last, alive, stop):
        while alive.is_set() and not stop.is_set():
            time.sleep(1.0)
            if time.monotonic() - last[0] < QUIET_S:
                continue
            last[0] = time.monotonic()
            try:
                latest, completed, _status = self.adapter.turn_outcome(client, thread)
            except Exception:  # noqa: BLE001 - check again after the next quiet period
                continue
            if latest > before and completed >= latest:
                stop.set()
                return
