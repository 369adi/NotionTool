"""Browser-free adapter built from Refresher's native transport and REA captures."""
from __future__ import annotations
from pathlib import Path
import copy
import datetime
import json
import os
import re
import secrets
import threading
import time
import uuid
import hashlib

from .engine import PendingRemote, RateLimited, ResetNotAccepted
from .vendor.sessions import SessionStore
from .vendor.transport import NativeNotionTransport, NotionHttpError
from .network_budget import NetworkBudget, request_priority
from .model_verification import verify_execution,ModelVerificationError,turn_status
from .telemetry import http_class
from .timeouts import task_limit


def pending_tool_label(entity):
    """Readable name of a tool call that waits for approval (e.g. 'nt_write_file')."""
    payload = entity.get('payload') or {}
    if payload.get('tool_name'):
        return str(payload['tool_name'])[:80]
    text = json.dumps(entity.get('text') or '', ensure_ascii=False)
    match = re.search(r'nt_[a-z_]+', text)
    return match.group(0) if match else str(entity.get('name') or 'a tool call')[:80]


APPROVAL_WAIT_SECONDS = 90  # kept for importers only: no timer decides the approval label any more (H5, approval_evidence)
REMOTE_QUIET_SECONDS = 600  # B46: a running turn without a new step for this long counts as stalled, not working

class ThreadMissing(ValueError):
    """Notion answered HTTP 404 for a worker thread (the chat was deleted)."""
    def __init__(self, thread_id, path=''):
        self.thread_id = str(thread_id or '')
        self.path = str(path or '')
        where = (' on ' + self.path) if self.path else ''
        super().__init__('Notion thread %s is missing (HTTP 404%s)' % (self.thread_id, where))


class ChatListUnavailable(ValueError):
    """The Notion chat list could not be read. Never treat it as an empty list: postpone or report, never create."""


class MainMissing(ValueError):
    """No usable Main or operator Main, and the user did not ask to create one (create_main=False)."""


# Calls that address one worker thread; HTTP 404 there means the thread is gone.
THREAD_PATHS = ('getThreadTranscript', 'runInferenceTranscript')  # stop names: live 404 'Cannot POST' (2026-10-18)


def unwrap(value):
    while isinstance(value, dict) and isinstance(value.get('value'), dict):
        value = value['value']
    return value or {}


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(text_content(x) for x in value)
    if isinstance(value, dict) and value.get('type') == 'text':
        return text_content(value.get('content', value.get('text', '')))
    return ''


# Shared 'turn finished / session idle' rules (false-empty fix, t3ec11e437). recover, the send checks,
# thread_busy/turn_running and turn_watch all use these; no other code decides 'finished' by itself.
FINISHED_STATUSES = ('idle', 'completed')  # a missing status is 'unknown', never 'finished'
EMPTY_PROOF_STATUSES = ('idle',)  # 'completed' is unproven as a session status (H1): it may accept a reply, never prove 'empty'
APPROVAL_STATUSES = ('awaiting_approval', 'needs_approval', 'requires_approval', 'waiting_for_approval',
                     'awaiting_confirmation', 'needs_confirmation', 'requires_confirmation')
PENDING_TOOL_TEXT = 'A tool call in the coworker chat has not finished yet'
# H5: honest labels for a pending tool call; only explicit evidence in the entry says 'approval'.
APPROVAL_WAIT_TEXT = 'waiting for approval'
TOOL_RUNNING_TEXT = 'tool running, no result yet'
TOOL_NOT_STARTED_TEXT = 'tool not started'


def session_state(transcript):
    """'finished', 'running' or 'unknown' for one transcript read."""
    transcript = transcript or {}
    if transcript.get('session_pending'):
        return 'running'
    status = (transcript.get('session') or {}).get('status')
    if status is None:
        return 'unknown'
    return 'finished' if status in FINISHED_STATUSES else 'running'


def record_state(thread):
    """Same vocabulary for a thread record: 'running', 'finished' or 'unknown' (no record)."""
    if not isinstance(thread, dict) or not thread:
        return 'unknown'
    data = thread.get('data') if isinstance(thread.get('data'), dict) else {}
    outcome = data.get('last_turn_outcome') or thread.get('last_turn_outcome') or {}
    try:
        latest = float(thread.get('latest_user_or_trigger_time') or data.get('latest_user_or_trigger_time') or 0)
        done = float(outcome.get('completed_time') or 0)
    except (AttributeError, TypeError, ValueError):
        return 'unknown'  # an unreadable record proves nothing
    return 'running' if latest > done else 'finished'


def entity_text(entity):
    """Reply text of one transcript entry: 'content' first, then 'text'."""
    text = text_content(entity.get('content'))
    return text if text.strip() else text_content(entity.get('text'))


def approval_evidence(entity):
    """True only when the entry itself says it waits for an approval click (H5: never guessed from time)."""
    payload = entity.get('payload') if isinstance(entity.get('payload'), dict) else {}
    if str(entity.get('interaction_status') or '').lower() in APPROVAL_STATUSES:
        return True
    keys = ('requires_approval', 'needs_approval', 'awaiting_approval', 'requires_confirmation')
    return any(src.get(key) is True for src in (entity, payload) for key in keys)


def tool_waiting(entity):
    """True for a tool call of the own turn that has no result yet (pending, or an explicit approval wait)."""
    return str(entity.get('interaction_status') or '').lower() == 'pending' or approval_evidence(entity)


def pending_tool_state(entity):
    """Honest label of a pending tool call (H5): approval only with evidence, never guessed from time."""
    if approval_evidence(entity):
        return APPROVAL_WAIT_TEXT
    payload = entity.get('payload') if isinstance(entity.get('payload'), dict) else {}
    if any(src.get(key) for src in (entity, payload) for key in ('started_at', 'startedAt')):
        return TOOL_RUNNING_TEXT
    return TOOL_NOT_STARTED_TEXT


def turn_state(transcript, task, resumes=()):
    """State of the task's own turn from ONE transcript read.

    done_reply, done_empty, done_error, tool_pending, running, unknown or not_found. Only done_empty may
    lead to a retry: own entries exist, no reply text, no pending tool, no error step, and either a
    turn_completed entry or session status 'idle' whose session sequence covers the own turn.
    """
    transcript = transcript or {}
    entities = [p.get('entity') or {} for p in transcript.get('patches') or [] if p.get('op') == 'put']
    before = task.get('before_seq', 0) or 0
    fresh = [e for e in entities if (e.get('sequence') or 0) > before]
    session = transcript.get('session') or {}
    step = task.get('step_id')
    markers = [e for e in fresh if e.get('kind') == 'user_message' and step and
               (e.get('id') == step or e.get('client_message_id') == step)]
    if not markers and task.get('request_fingerprint'):
        # Text-hash fallback: only one exact match at or after before_seq counts; otherwise reconcile, never resend.
        hashed = [e for e in entities if e.get('kind') == 'user_message' and
                  hashlib.sha256(text_content(e.get('content', e.get('text', []))).encode()).hexdigest() == task['request_fingerprint']]
        if len(hashed) == 1 and (hashed[0].get('sequence') or 0) >= before:
            markers = hashed
    sess = session_state(transcript)
    seq = session.get('sequence')
    known_seq = isinstance(seq, (int, float)) and not isinstance(seq, bool)
    out = dict(state='not_found', reason='own user message is not in this read', text='', fresh=fresh, own_turn=[],
               finished=False, session=sess, status=session.get('status'), session_seq=seq, own_seq=None, last_seq=None)
    if not markers:
        return out
    after = min(e.get('sequence') or 0 for e in markers)
    resumes = set(resumes or ())  # automatic continue messages stay inside this turn
    until = min([e.get('sequence') or 0 for e in fresh if e.get('kind') == 'user_message' and (e.get('sequence') or 0) > after
                 and not ({e.get('id'), e.get('client_message_id')} & resumes)], default=float('inf'))
    own_turn = sorted([e for e in fresh if after < (e.get('sequence') or 0) < until], key=lambda e: e.get('sequence') or 0)
    text = '\n'.join(t for t in (entity_text(e) for e in own_turn if e.get('kind') == 'assistant_message') if t.strip())
    turn_done = any(e.get('kind') == 'turn_completed' for e in own_turn)
    last_seq = max([after] + [e.get('sequence') or 0 for e in own_turn])
    stale = not turn_done and known_seq and seq < last_seq
    finished = turn_done or (sess == 'finished' and not stale)
    out.update(own_turn=own_turn, text=text, own_seq=after, last_seq=last_seq, finished=finished)
    if finished and text:
        return dict(out, state='done_reply', reason='own turn finished with reply text')
    if not finished:
        if stale:
            return dict(out, state='running', reason='session sequence %s is below the own turn (%s)' % (seq, last_seq))
        if sess == 'unknown':
            return dict(out, state='unknown', reason='session status missing')
        return dict(out, state='running', reason='session pending' if transcript.get('session_pending') else
                    'session status %s' % session.get('status'))
    if any(tool_waiting(e) for e in own_turn):
        return dict(out, state='tool_pending', reason=PENDING_TOOL_TEXT)
    if any('error' in str(e.get('kind') or e.get('type') or '').lower() for e in own_turn):
        return dict(out, state='done_error', reason='own turn has an error step and no reply')
    if not own_turn:
        return dict(out, state='unknown', reason='no entries after the own user message yet')
    if not turn_done and (session.get('status') not in EMPTY_PROOF_STATUSES or not known_seq):
        return dict(out, state='unknown', reason='finished status %s / sequence %s does not prove an empty turn'
                    % (session.get('status'), seq))
    return dict(out, state='done_empty', reason='own turn finished without reply text')


def apply_patch(steps, operations):
    root = {'s':steps}
    for op in operations or []:
        path = [x.replace('~1','/').replace('~0','~') for x in op.get('p','').split('/')[1:]]
        if not path:
            continue
        try:
            current = root
            for key in path[:-1]:
                current = current[int(key)] if isinstance(current,list) else current[key]
            key = path[-1]
            if op.get('o') == 'r':
                current.pop(int(key)) if isinstance(current,list) else current.pop(key,None)
            elif key == '-':
                current.append(op['v'])
            elif op.get('o') == 'x':
                if isinstance(current,list):
                    current[int(key)] += op['v']
                else:
                    current[key] += op['v']
            elif isinstance(current,list):
                index = int(key)
                current.append(op['v']) if index == len(current) else current.__setitem__(index,op['v'])
            else:
                current[key] = op['v']
        except (KeyError,IndexError,ValueError,TypeError):
            continue
    return root['s']


def stream_text(steps):
    return '\n\n'.join(text_content(s.get('value',[])) for s in steps
                       if isinstance(s,dict) and s.get('type') in ('agent-inference','assistant_message'))

def not_accepted(task, transcript, fresh, grace=30):
    """True only when Notion completed a later turn without committing this step."""
    session = transcript.get('session') or {}
    sent = task.get('sent_at', 0)
    if transcript.get('session_pending') or session.get('status') not in ('idle', 'completed'):
        return False
    if not sent or time.time() - sent < grace:
        return False

    def stamp(entity):
        value = entity.get('created_at') or entity.get('createdAt') or entity.get('created_time') or 0
        if isinstance(value, (int, float)):
            return value / 1000 if value > 1e11 else value
        try:
            return datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        except ValueError:
            return 0
    return any(e.get('kind') == 'assistant_message' and stamp(e) > sent for e in fresh)


MODEL_CATALOG_TTL = 600  # seconds; a cached model catalog is read again after this or on a miss

# Request field for the reasoning effort inside the workflow config step (transcript[0].value),
# next to model/modelFromUser. Taken from the Notion client's own workflow config builder; evidence
# in work/effort_field.md. Set to None to stop sending it (selection and persistence stay).
EFFORT_FIELD = 'reasoningEffort'

# Model kept in the saved config step and in body() config.model (thread default, like the Notion UI).
# The requested model/effort go only into the 'updated-config' step. A mismatch between the saved
# config and the body config made the server fall back to Opus (live proof 2026-10-20).
BASE_CONFIG_MODEL = 'albuquerque-quinn'


def catalog_entry(client, model_id):
    """getAvailableModels entry for model_id from the cached catalog, or None (no I/O)."""
    try:
        cache = getattr(getattr(client, 'transport', None), 'model_catalog', None) or {}
        entry = cache.get(client.info['space_id']) if isinstance(cache, dict) else None
        for item in (entry[1] if entry else []):
            if isinstance(item, dict) and item.get('model') == model_id:
                return item
    except Exception:
        pass
    return None


def choose_effort(client, model_id, purpose='', task_class='', risk=''):
    """(effort or None, reason) for one request, clamped to the model's supportedReasoningEfforts."""
    from .effort_policy import catalog_efforts, choose_reasoning_effort
    supported, default = catalog_efforts(catalog_entry(client, model_id))
    return choose_reasoning_effort(purpose, task_class, risk, supported, default)


def choose_task_effort(client, model_id, purpose='', task=None):
    """(effort or None, reason) for one task request (Rule I38).

    The effort the Main set on the task (requested_effort, from operator_main.delegate) wins, clamped to
    the model's supportedReasoningEfforts; otherwise the automatic choose_effort. Turns of the Main chat
    itself (purpose 'main') always keep the automatic choice."""
    from .effort_policy import REASONING_ORDER, band_effort, catalog_efforts
    task = task if isinstance(task, dict) else {}
    asked = task.get('requested_effort')
    asked = asked.strip().lower() if isinstance(asked, str) else ''
    if purpose != 'main' and asked in REASONING_ORDER:
        supported, _default = catalog_efforts(catalog_entry(client, model_id))
        # Owner rule 2026-10-22: a Coworker effort set by the Main stays in the band medium..high.
        effort, note = band_effort(asked, supported, purpose)
        if effort is None and note:
            return None, 'set by Main: %s; not sent: %s' % (asked, note)
        if effort is None:
            return None, ('set by Main: %s; not sent: no supportedReasoningEfforts for this model '
                          'in the cached catalog' % asked)
        reason = ('set by Main: ' + asked) if effort == asked else ('set by Main: %s, clamped to %s' % (asked, effort))
        return effort, reason + ('; ' + note if note else '')
    return choose_effort(client, model_id, purpose, task.get('task_class', ''), task.get('risk', ''))


def final_effort(client, model_id, effort, purpose=''):
    """Last guard before an effort is sent for a Coworker (owner rule 2026-10-22): a stored or stale
    value (older tasks, resumes) is put into the band medium..high and clamped to the model.
    An unknown level name becomes COWORKER_MIN_EFFORT ('medium'); a model without any level in the
    band gets None (no effort is sent). Main purposes and empty/non-string values pass through unchanged."""
    from .effort_policy import (COWORKER_MIN_EFFORT, MAIN_PURPOSES, REASONING_ORDER, band_effort,
                                cap_coworker, catalog_efforts)
    if purpose in MAIN_PURPOSES or not isinstance(effort, str) or not effort.strip():
        return effort
    level = effort.strip().lower()
    if level not in REASONING_ORDER:
        level = COWORKER_MIN_EFFORT
    supported, _default = catalog_efforts(catalog_entry(client, model_id))
    if supported:
        return band_effort(level, supported, purpose)[0]
    return cap_coworker(level, purpose)


MAIN_OWN_CONFIG = ('main', 'operator', 'spare_main', 'external')  # external = the user's own chats (Test chat)
MODEL_NAMES = {'albuquerque-quinn': 'opus', 'achira-donut': 'sonnet', 'amla-jam': 'haiku'}


MAIN_DEFAULT_EFFORT = 'high'
OWN_CONFIG_IDS = set()
OWN_ID_TAIL = '00aa'
OWN_ID_SALT = b'notiontool-own-id-v1'
OWN_ID_LEGACY_LAST_DAY = 1024  # 2026-10-21: older NotionTool ids carried only the 00aa tail
OWN_ID_FILE = [None]
OWN_ID_KEEP = 5000


def own_id_sign(head):
    return hashlib.sha256(OWN_ID_SALT + head.encode('ascii', 'ignore')).hexdigest()[:4]


def load_own_config_ids(path):
    """I46: own 'updated-config' ids survive restarts (legacy spaces have no 00aa tail at all)."""
    OWN_ID_FILE[0] = Path(path)
    try:
        known = OWN_ID_FILE[0].read_text(encoding='utf-8').split()
    except OSError:
        return 0
    OWN_CONFIG_IDS.update(known[-OWN_ID_KEEP:])
    if len(known) > OWN_ID_KEEP:
        try:
            OWN_ID_FILE[0].write_text('\n'.join(known[-OWN_ID_KEEP:]) + '\n', encoding='utf-8')
        except OSError:
            pass
    return len(OWN_CONFIG_IDS)


def remember_own_config_id(ident):
    OWN_CONFIG_IDS.add(ident)
    if OWN_ID_FILE[0] is not None:
        try:
            with open(OWN_ID_FILE[0], 'a', encoding='utf-8') as handle:
                handle.write(ident + '\n')
        except OSError:
            pass


def own_config_step(step):
    """True for 'updated-config' steps NotionTool sent itself.

    I46: the 00aa tail alone is no proof (Notion's IdCreator may emit it too). New ids carry a
    signature in the last 4 hex digits; unsigned 00aa ids only count up to OWN_ID_LEGACY_LAST_DAY.
    Remembered ids (persisted across restarts) always count."""
    ident = str(step.get('id') or '')
    if not ident:
        return False
    if ident in OWN_CONFIG_IDS:
        return True
    last = ident.rsplit('-', 1)[-1]
    if len(last) != 12 or not last.startswith(OWN_ID_TAIL):
        return False
    if last[8:] == own_id_sign(ident[:-4]):
        return True
    try:
        return int(ident[:3], 16) <= OWN_ID_LEGACY_LAST_DAY
    except ValueError:
        return False


def main_default_effort(client, model_id):
    """Main chats never fall back to the automatic (often medium) effort: high, clamped to the model."""
    from .effort_policy import catalog_efforts, clamp_effort
    try:
        supported, _default = catalog_efforts(catalog_entry(client, model_id))
    except Exception:
        supported = ()
    return clamp_effort(MAIN_DEFAULT_EFFORT, supported) or MAIN_DEFAULT_EFFORT


def latest_user_config(steps):
    """(model, effort) of the newest 'updated-config' choice in steps (oldest first); '' when absent.

    Steps NotionTool wrote itself are skipped: they are not a user choice and must not pin an old value."""
    model = effort = ''
    for step in reversed(steps):
        if not isinstance(step, dict) or step.get('type') != 'updated-config':
            continue
        if own_config_step(step):
            continue
        value = step.get('value') or {}
        if not model and value.get('model'):
            model = str(value['model'])
        if not effort and value.get('reasoningEffort'):
            effort = str(value['reasoningEffort']).strip().lower()
        if model and effort:
            break
    return model, effort


def user_effort(client, thread_id):
    """Effort for a turn NotionTool sends into a Main/user chat (handoff, context checkpoint): the user's
    last choice in that chat, else high. Without it Notion falls back to the model default (medium)."""
    try:
        got = inherit_main_config(client, thread_id)
        return (got[2] if got else None) or MAIN_DEFAULT_EFFORT
    except Exception:
        return MAIN_DEFAULT_EFFORT


def user_config(client, thread_id):
    """(model, effort) for a turn NotionTool sends into a Main/user chat (wake notice, handoff):
    the user's own last model and reasoning in that chat. Never the model default and never Auto."""
    try:
        got = inherit_main_config(client, thread_id)
    except Exception:
        got = None
    if got and got[1]:
        return got[1], got[2] or MAIN_DEFAULT_EFFORT
    return client.model('opus'), MAIN_DEFAULT_EFFORT


def inherit_main_config(client, thread_id, limit=60):
    """(selected, model_id, effort, reason) from a Main chat's own newest model/effort pick, or None.

    Main chats keep what the user chose in the Notion menu; only coworkers get the automatic choice."""
    space = client.info['space_id']
    def records(table, ids):
        if not ids:
            return {}
        result = client.call('syncRecordValuesSpaceInitial', {'requests': [
            {'pointer': {'table': table, 'id': ident, 'spaceId': space}, 'version': -1} for ident in ids]})
        return {ident: unwrap(value) for ident, value in result.get('recordMap', {}).get(table, {}).items()}
    try:
        thread = records('thread', [thread_id]).get(thread_id, {})
        ids = list(thread.get('messages') or [])[-int(limit):]
        messages = records('thread_message', ids)
    except Exception:
        ids, messages = [], {}
    steps = [dict((messages.get(ident) or {}).get('step') or {}) for ident in ids if ident in messages]
    model, effort = latest_user_config(steps)
    picked = bool(model or effort)
    if model:
        selected = MODEL_NAMES.get(model, model)
    else:
        selected, model = 'opus', client.model('opus')
    if not effort:
        effort = main_default_effort(client, model)
    if picked:
        return selected, model, effort, 'Main chat: kept the user choice (%s / %s)' % (selected, effort)
    return selected, model, effort, ('Main chat: no user choice found, %s / %s (never the automatic effort)'
                                     % (selected, effort))


def own_turn_steps(records, ids, step_id, chunk=20):
    # Read thread messages newest first until our own user step is loaded. Only the
    # own turn is checked, so older history is skipped; without a match all is read.
    steps, start, read = [], len(ids), 0
    while start > 0:
        part = ids[max(0, start - chunk):start]
        messages = records('thread_message', part)
        steps = [dict((messages[ident] or {}).get('step', {})) for ident in part if ident in messages] + steps
        read += len(part)
        start -= len(part)
        if step_id and any(s.get('type') == 'user' and s.get('id') == step_id for s in steps):
            break
        chunk = min(chunk * 5, 100)
    return steps, read


class VerifiedReply(str):
    """Own committed turn; proof flags cannot be supplied by model JSON."""
    def __new__(cls,text,tool_activity):
        value=super().__new__(cls,text)
        value.verified_terminal_reply=True
        value.tool_activity=bool(tool_activity)
        return value


class NotionClient:
    def __init__(self, transport, identity=None):
        self.transport = transport
        self.info = identity or {}

    def headers(self):
        # Login endpoints reject unversioned clients even before authentication.
        # Bootstrap version comes from the bundled REA capture; identity/session
        # can override it without changing the adapter.
        origin = getattr(self.transport,'_base','https://app.notion.com')
        result = {'notion-audit-log-platform':'web','Origin':origin,'Referer':origin+'/login',
                  'notion-client-version':self.info.get('client_version') or '23.13.20261017.0942'}
        for key, header in (('space_id','x-notion-space-id'),('user_id','x-notion-active-user-header'),('client_version','notion-client-version')):
            if self.info.get(key):
                result[header] = self.info[key]
        return result

    def call(self, path, body, timeout=45):
        status, text = self.transport.request(path,body,headers=self.headers(),timeout=timeout)
        if status == 429:
            raise RateLimited(getattr(self.transport, 'retry_after', 30))
        if status == 404 and path in THREAD_PATHS and isinstance(body, dict) and body.get('threadId'):
            raise ThreadMissing(body['threadId'], path)
        if status != 200:
            raise ValueError(f'Notion {path}: {text}')
        data = json.loads(text)
        return self._follow_fanout(path, data, timeout)

    def _follow_fanout(self, path, data, timeout):
        """Live 2026-10-22: syncRecordValues* answers with an empty recordMap plus fanoutData that names the
        server cell holding the records (x-notion-cell). Without following it every thread record read was
        empty, so thread_busy never saw a finished turn. Merge the records of every named cell."""
        fanout = data.get('fanoutData') if isinstance(data, dict) else None
        if not fanout or not isinstance(fanout, list):
            return data
        records = dict(data.get('recordMap') or {})
        for item in fanout:
            if not isinstance(item, dict) or not isinstance(item.get('request'), dict):
                continue
            headers = {**self.headers(), **{k: str(v) for k, v in (item.get('headers') or {}).items()}}
            status, text = self.transport.request(path, item['request'], headers=headers, timeout=timeout)
            if status == 429:
                raise RateLimited(getattr(self.transport, 'retry_after', 30))
            if status != 200:
                raise ValueError(f'Notion {path} (cell {item.get("headers")}): {text}')
            for table, rows in (json.loads(text).get('recordMap') or {}).items():
                if isinstance(rows, dict):
                    records.setdefault(table, {}).update(rows)
        result = {k: v for k, v in data.items() if k != 'fanoutData'}
        result['recordMap'] = records
        return result

    def identity(self):
        data = self.call('loadUserContent',{})
        records = data.get('recordMap',{})
        users = records.get('notion_user',{})
        if not users:
            raise ValueError('Notion login required')
        user_id = next(iter(users))
        user = unwrap(users[user_id])
        spaces = [dict(id=ident, name=unwrap(value).get('name','Workspace'))
                  for ident,value in records.get('space',{}).items() if unwrap(value).get('alive',True)]
        self.info = dict(user_id=user_id, email=user.get('email',''), user_name=user.get('name',''),
                         client_version=self.info.get('client_version') or '23.13.20261017.0942',
                         space_id=self.info.get('space_id') or (spaces[0]['id'] if spaces else ''),
                         spaces=spaces, onboarded=user.get('onboarding_completed'), records=records)
        return self.info

    def model(self, selected='opus'):
        from .model_routing import MODELS, choice
        label = MODELS[choice(selected)]
        def pick(models):
            return next((m for m in models if m.get('modelProvider')=='anthropic' and not m.get('isDisabled')
                         and re.sub(r'\s+',' ',m.get('modelMessage','')).strip().lower()==label.lower()), None)
        # The catalog is cached per workspace on the long-lived transport of the account.
        space = self.info['space_id']
        cache = getattr(self.transport, 'model_catalog', None)
        if not isinstance(cache, dict):
            cache = {}
            try:
                self.transport.model_catalog = cache
            except AttributeError:
                pass
        entry = cache.get(space)
        model = pick(entry[1]) if entry and time.monotonic() - entry[0] < MODEL_CATALOG_TTL else None
        if not model or not model.get('model'):
            # Missing, expired or label not in the cached list: read the catalog once.
            models = self.call('getAvailableModels',{'spaceId':space,'surface':'workspace_model_settings'}).get('models',[])
            cache[space] = (time.monotonic(), models)
            model = pick(models)
        if not model or not model.get('model'):
            raise ValueError(label+' is not available for this account. No model substitution was made.')
        return model['model']

    def threads(self):
        # getInferenceTranscriptsForUser was removed in Notion v23.13.20261017.
        # includeWorkflowThreads / includeWriterChats params also gone.
        # Fallback: thread ids from the space record, marked guessed; an unreadable list raises ChatListUnavailable.
        # TODO: find confirmed replacement endpoint for thread listing.
        try:
            return self.call('getInferenceTranscriptsForUser', {'threadParentPointer': {
                'table': 'space', 'id': self.info['space_id'], 'spaceId': self.info['space_id']}
            }).get('transcripts', [])
        except ValueError:
            # Server removed endpoint: load thread list from space record
            try:
                result = self.call('syncRecordValuesSpaceInitial', {'requests': [
                    {'pointer': {'table': 'space', 'id': self.info['space_id'],
                                 'spaceId': self.info['space_id']}, 'version': -1}]})
                space = {}
                for v in result.get('recordMap', {}).get('space', {}).values():
                    while isinstance(v, dict) and isinstance(v.get('value'), dict):
                        v = v['value']
                    space = v
                    break
                thread_ids = list((space.get('data') or {}).get('thread_ids') or []) if space else None
            except Exception as exc:
                raise ChatListUnavailable('Notion chat list unavailable: %s' % type(exc).__name__) from exc
            if thread_ids is None:
                raise ChatListUnavailable('Notion chat list unavailable: space record missing')
            # Guessed entries: refresh() may update existing workers from them, never create new ones.
            return [{'id': tid, 'guessed': True} for tid in thread_ids]

    def transcript(self, thread, limit=100):
        # getThreadTranscript returns the committed transcript (live check 2026-10-18: HTTP 200).
        # getInferenceTranscriptInferences is a debug endpoint (HTTP 400 'Transcript debugging is not enabled').
        # Errors reach the caller: recover() reconciles them, a 404 becomes ThreadMissing.
        # Live 2026-10-22 04:10: a long chat with big tool outputs exceeds the 32 MB Edge cap ('Response too
        # large'). That is a size problem of one chat, not an outage: read a smaller window instead of failing,
        # otherwise five such reads open the getThreadTranscript circuit for every chat (restart scan, wake).
        limit = max(1, int(limit))
        while True:
            try:
                return self.call('getThreadTranscript',{'spaceId':self.info['space_id'],'threadId':thread,
                                                     'direction':'backward','limit':limit})
            except Exception as exc:
                if 'Response too large' not in str(exc) or limit <= 1:
                    raise
                limit = max(1, limit // 4)


HANDOFF_EXCERPT_DEFAULT = 1500


def handoff_excerpt_limit(settings):
    """Setting handoff_excerpt_chars (default 1500; 0 = old behavior, the full checkpoint text)."""
    try:
        return max(0, int(settings.get('handoff_excerpt_chars', HANDOFF_EXCERPT_DEFAULT)))
    except (TypeError, ValueError):
        return HANDOFF_EXCERPT_DEFAULT


def handoff_excerpt(text, limit):
    """At most limit characters of a checkpoint, cut at a line end.

    limit <= 0 or a text that already fits is returned unchanged. Without a line end inside
    the limit the cut falls back to the last space, then to exactly limit characters."""
    text = text or ''
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text.rfind('\n', 0, limit + 1)
    if cut <= 0:
        cut = text.rfind(' ', 0, limit + 1)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip()


def handoff_excerpt_block(text, limit, path):
    """Coworker checkpoint: excerpt, the path of the full handoff file and one 'read only if needed' line."""
    excerpt = handoff_excerpt(text, limit)
    return (excerpt + '\n[Checkpoint excerpt: %d of %d characters. Full checkpoint ("handoff" field): %s]\n'
            'Read that file with the NotionTool read_file tool only if you need more than this excerpt.'
            % (len(excerpt), len(text or ''), path))


class NotionAdapter:
    single_account_harness = True
    semantic_context = True
    response_mode='plain'
    task_model_routing=True
    def __init__(self, store):
        self.store = store
        try:
            load_own_config_ids(Path(store.directory) / 'own_config_ids.txt')
        except Exception:
            pass
        self.sessions = SessionStore(data_dir=str(store.directory / 'sessions'))
        self.transports = {}
        self.edge_transports = {}
        self.lock = threading.RLock()
        self.setup_locks = {}
        self.template = json.loads((Path(__file__).parent / 'template.json').read_text(encoding='utf-8'))
        self.welcome = json.loads((Path(__file__).parent / 'welcome.json').read_text(encoding='utf-8'))
        self.network_budget = NetworkBudget(store)
        self.clear_account_pause()
        self.confirmed_configs = {}  # thread_id -> (thread_id, generation, model) after a confirmed config check

    def clear_account_pause(self, authenticated_account_id='', verified=False):
        """Release an orphaned pause, the freshly authenticated account's pause, or - after a verified
        sign-in (Notion just answered the identity call) of a DIFFERENT account - the other account's pause.

        In the last case the blocked account itself stays out: a ready one is set to 'signin' (the engine only
        uses ready accounts) and its paused tasks stay blocked until it signs in again or is resumed by hand."""
        pause = self.network_budget.status().get('pause') or {}
        blocked = pause.get('account_id')
        if blocked and (blocked == authenticated_account_id or not self.store.get('accounts', blocked)):
            return self.network_budget.resume()
        if blocked and verified and authenticated_account_id and self.store.get('accounts', authenticated_account_id):
            other = self.store.get('accounts', blocked)
            if other.get('state') == 'ready':
                other.update(state='signin', error='Notion blocked this account; sign in again.')
                self.store.put('accounts', other)
            self.store.event('account_pause_released', message='Remote calls resumed after a sign-in of another '
                             'account; the blocked account %s stays out until it signs in again' % other.get('email', blocked),
                             account_id=authenticated_account_id, blocked_account=blocked)
            return self.network_budget.resume()
        return False

    def client(self, account_id, anonymous=False):
        with self.lock:
            if account_id not in self.transports:
                session = self.sessions.load(account_id) or {}
                if not anonymous and not session.get('cookies'):
                    raise ValueError('Sign in to this Notion account first')
                self.transports[account_id] = NativeNotionTransport(session,session_store=self.sessions,session_key=account_id,budget=self.network_budget)
            account = self.store.get('accounts',account_id,{})
            transport = self.transports[account_id]
            from .harness_account import selected_account
            if (not anonymous and account.get('identity', {}).get('user_id')
                    and selected_account(self.store) == account_id
                    and self.store.settings().get('notion_transport', 'edge') == 'edge'):
                transport = self.edge_transport(account_id)
            return NotionClient(transport,account.get('identity'))

    def edge_transport(self, account_id):
        with self.lock:
            if account_id not in self.edge_transports:
                from .edge_transport import EdgeTransport
                self.edge_transports[account_id] = EdgeTransport(self.store, self.sessions, account_id, self.network_budget)
            return self.edge_transports[account_id]

    def create_account(self, email, slot=None):
        email = email.strip().lower()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):
            raise ValueError('Enter a valid email address')
        with self.lock:
            existing = next((a for a in self.store.all('accounts') if a['email']==email),None)
            if existing:
                return existing
            used = {int(a['slot']) for a in self.store.all('accounts')}
            slot = slot or next(n for n in range(1,1001) if n not in used)
            account = dict(id=uuid.uuid4().hex,slot=slot,email=email,label=f'Slot {slot}',
                           state='login',identity={},model='',bridge_connected=False,cooldown_until=0)
            self.store.put('accounts',account)
            settings = self.store.settings()
            settings['harness_account_id'] = account['id']
            self.store.put('settings', settings)
            return account

    def onboard(self, account_id):
        client = self.client(account_id)
        info = client.identity()
        if not info['spaces']:
            client.call('recordCookieConsent',{'permission':{'necessary':True,'performance':False,
                'preference':False,'targeting':False},'document_version':'v10','userId':info['user_id']})
            result = client.call('createSpace',dict(name='NotionTool Workspace',icon='💼',planType='team',
                planSelection='team',initialPersona='unfilled',domainType='professional',deviceId=str(uuid.uuid4()),
                deviceType='web-desktop',source='handle_root_redirect',createSpaceView=True))
            info['space_id'] = result['spaceId']
        if not info['onboarded']:
            client.call('completeCurrentUserOnboarding',{'completion':'standard'})
        account = self.store.get('accounts',account_id)
        account['identity'] = client.identity()
        self.store.put('accounts',account)
        return self.refresh(account_id)

    @staticmethod
    def workspace_ai_access(info):
        space = unwrap(info.get('records', {}).get('space', {}).get(info.get('space_id'), {}))
        settings = space.get('settings', {})
        flags = {key: settings.get(key) for key in (
            'enable_ai_feature', 'disable_ai_feature', 'notion_admin_disabled_ai_feature')}
        disabled = flags['disable_ai_feature'] is True or flags['notion_admin_disabled_ai_feature'] is True
        return dict(state='disabled' if disabled else 'enabled' if flags['enable_ai_feature'] is True else 'unknown', **flags)

    def require_workspace_ai(self, account_id, info):
        access = self.workspace_ai_access(info)
        if access['state'] == 'disabled':
            account = self.store.get('accounts', account_id)
            account.update(state='ai_disabled', ai_access=access,
                           error='Notion AI is disabled for this workspace.')
            self.store.put('accounts', account)
            raise ValueError(account['error'])

    def refresh(self, account_id):
        client = self.client(account_id)
        info = client.identity()
        account = self.store.get('accounts',account_id)
        if info['email'].lower()!=account['email'].lower():
            raise ValueError('Account identity mismatch')
        account.update(identity=info,state='ready',error='')
        account['ai_access'] = self.workspace_ai_access(info)
        if account['ai_access']['state'] == 'disabled':
            account.update(state='ai_disabled', model='',
                           error='Notion AI is disabled for this workspace.')
        else:
            try:
                account['model'] = client.model()
            except ValueError as exc:
                account.update(state='model_unavailable',error=str(exc),model='')
        self.store.put('accounts',account)
        existing = {w['thread_id']:w for w in self.store.all('workers') if w['account_id']==account_id}
        gens = [g for g in self.store.all('thread_generations') if g.get('account_id') == account_id]
        # Fix S: chats retired by a new-Welcome context switch never re-enter the pool.
        archived = {g['id'] for g in gens} | {g['thread_id'] for g in gens if g.get('mode') == 'new_welcome'}
        # Chats the app created itself (team_threads ledger: Main and Coworker roles) may be listed only as
        # guessed ids; they are registered with their ledger role. Any other guessed id may be the user's own
        # chat and stays skipped.
        managed = {}
        for role, item in (account.get('team_threads') or {}).items():
            try:
                managed.setdefault(str(uuid.UUID(str((item or {}).get('id') or ''))), role)
            except (ValueError, TypeError, AttributeError):
                pass
        # Chats the app is creating right now (open create_main operations, e.g. a private 'Project notes' chat)
        # are not in team_threads and may be listed only as guessed ids; they get the operation's purpose.
        created = {}
        for op in self.store.all('operations'):
            if (op.get('kind') == 'create_main' and op.get('account_id') == account_id and op.get('thread_id')
                    and op.get('state') not in ('done', 'failed')):
                try:
                    created[str(uuid.UUID(str(op['thread_id'])))] = op.get('purpose') or 'main'
                except (ValueError, TypeError, AttributeError):
                    pass
        seen = set()
        for thread in client.threads():
            tid = thread.get('id') or thread.get('thread_id') or thread.get('threadId')
            if not tid:
                continue
            tid = str(uuid.UUID(tid))
            seen.add(tid)
            if tid in archived:
                continue
            if thread.get('guessed') and tid not in existing and tid not in managed and tid not in created:
                continue  # a guessed id outside the team ledger may be the user's own chat: never a worker
            with self.store.lock:
                # Re-read under the lock: client.threads() is slow, and a context switch may have moved this
                # worker to a new chat meanwhile. Writing the old snapshot back reverted Coworker 9 to its
                # deleted chat (live bug 22 Oct 04:42) and left the new chat as an unnamed Welcome chat.
                worker = existing.get(tid)
                if worker:
                    worker = self.store.get('workers', worker['id']) or worker
                    if worker.get('thread_id') != tid:
                        continue
                else:
                    # A worker moved to this chat after the snapshot (context switch) owns it; never add a
                    # second, unnamed 'Welcome to Notion' worker record for the same chat.
                    worker = next((w for w in self.store.all('workers') if w.get('account_id') == account_id
                                   and w.get('thread_id') == tid), None)
                worker = worker or dict(id=account_id+':'+tid,account_id=account_id,thread_id=tid,
                    purpose=created.get(tid) or ('main' if managed.get(tid) == 'main' else 'worker'),
                    enabled=False,state='ready',lease='',last_used=0)
                worker['label'] = thread.get('title') or (worker.get('label') if thread.get('guessed') else '') or 'Untitled chat'
                if worker['state'] in ('missing','error') and not worker.get('lease'):
                    worker['state'] = 'ready'
                self.store.put('workers',worker)
        for tid,worker in existing.items():
            if tid in seen:
                continue
            with self.store.lock:
                worker = self.store.get('workers', worker['id'])
                # Same stale-snapshot guard; a retired worker stays retired (no missing/retire loop every 3 min).
                if (not worker or worker.get('thread_id') != tid or worker.get('state') in ('busy','recovering','retired')
                        or worker.get('retired')):
                    continue
                worker.update(state='missing',enabled=False)
                self.store.put('workers',worker)
        try:
            pool = self.apply_chat_pool(account_id, client)
        except Exception as exc:
            pool = {}
            self.store.event('chat_pool_error', message=str(exc)[:200])
        return dict(account_id=account_id,threads=len(seen),state=account['state'],coworkers=pool.get('coworkers',0))

    NOTION_COOKIE_DOMAINS=('notion.so','notion.com')

    def browser_login(self, timeout=1200):
        """Open the Notion login page in a plain Edge window and keep the session the user signs in with.

        No DevTools port on the visible window: Google refuses or breaks sign-in in a browser started
        with --remote-debugging-port, so the Google login never reached the app. The cookies are read
        afterwards from the closed profile (window_login.capture_browser_cookies).
        """
        from .window_login import capture_browser_cookies
        if getattr(self,'_browser_login_active',False):raise ValueError('A Notion sign-in window is already open')
        self._browser_login_active=True
        try:
            cookies=capture_browser_cookies(self.store, timeout=timeout)
        finally:
            self._browser_login_active=False
        if not cookies:raise ValueError('No Notion sign-in was detected. Sign in completely before closing the window.')
        return self.adopt_session_cookies(cookies,'browser_login')

    def adopt_session_cookies(self, cookies, source='browser_login', select=False):
        """Save a signed-in Notion cookie set as an account session (browser login or NotionTool window login)."""
        if not any(c.get('name')=='token_v2' and c.get('value') for c in cookies or []):
            raise ValueError('No Notion sign-in was detected')
        session={'cookies':cookies,'source':source,'created':time.time()}
        info=NotionClient(NativeNotionTransport(session)).identity()
        email=info['email'].lower()
        existing=next((a for a in self.store.all('accounts') if a['email'].lower()==email),None)
        had_accounts=bool(self.store.all('accounts'))
        account=existing or self.create_account(email)
        self.sessions.save(account['id'],session)
        self.transports.pop(account['id'],None)
        edge = self.edge_transports.get(account['id'])
        if edge is not None:
            # Reinstall the new cookies before the next request on an existing Edge process.
            edge.ready_process = None
        account=self.store.get('accounts',account['id'])
        account.update(identity=info,state='signed_in',error='')
        self.store.put('accounts',account)
        self.clear_account_pause(account['id'], verified=True)  # identity() above just succeeded
        try:self.refresh(account['id'])
        except Exception as exc:
            account=self.store.get('accounts',account['id']);account['error']=str(exc)[:200];self.store.put('accounts',account)
        from .harness_account import select_account
        if select or not had_accounts or existing:
            try:select_account(self.store,account['id'])
            except Exception:pass
        return {'account_id':account['id'],'email':email,'state':self.store.get('accounts',account['id']).get('state')}

    def remove_account(self, account_id):
        """Forget an account locally: saved sign-in, workers and local chat list. Nothing is deleted in Notion."""
        with self.lock:
            account=self.store.get('accounts',account_id)
            if not account:raise ValueError('Account not found')
            workers=[w for w in self.store.all('workers') if w.get('account_id')==account_id]
            if any(w.get('state')=='busy' for w in workers):raise ValueError('A task is still running on this account; stop it first')
            ids={w['id'] for w in workers}
            for chat in list(self.store.all('chats')):
                if chat.get('main_worker') in ids:self.store.delete('chats',chat['id'])
            for worker in workers:self.store.delete('workers',worker['id'])
            transport=self.transports.pop(account_id,None)
            try:
                if transport is not None and hasattr(transport,'close'):transport.close()
            except Exception:pass
            for key in (account_id,'login_'+account_id):
                try:self.sessions.delete(key)
                except Exception:pass
            self.store.delete('accounts',account_id)
            self.clear_account_pause()
            settings=self.store.settings()
            if settings.get('harness_account_id')==account_id:
                settings['harness_account_id']='';self.store.put('settings',settings)
        return {'removed':account_id,'email':account['email']}

    def body(self, client, thread_id, step_id, prompt, model, create=False, computer=False, effort=None):
        body = copy.deepcopy(self.template)
        info = client.info
        config = body['transcript'][0]
        config['id'] = self.new_id(info['space_id'],'00aa')
        # Notion AI reaches MCP connections (NotionTool, Screen, Roblox, Blender) only with the
        # tool environment enabled; plain text turns stay without it to keep latency low.
        # Do NOT put the requested model/effort into the config step: the server then falls back to the
        # thread default (Opus). Like the Notion UI, they go only into the 'updated-config' step below.
        config['value'].update(model=BASE_CONFIG_MODEL,modelFromUser=True,enableComputer=bool(computer),
                              agentMemorySettings={'useMemories':True,'excludeChatFromMemories':True})
        if EFFORT_FIELD:
            config['value'].pop(EFFORT_FIELD, None)
        # Only the real Welcome chat creation (welcome_body) may start onboarding.
        config['value'].pop('agentMessageKey', None)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        views = info.get('records',{}).get('space_view',{})
        view = next((unwrap(v).get('id',k) for k,v in views.items() if unwrap(v).get('space_id')==info['space_id']), '')
        body['transcript'] += [dict(id=self.new_id(info['space_id'],'00aa'),type='context',value=dict(
            userId=info['user_id'],spaceId=info['space_id'],surface='full_page_chat',timezone='Europe/Berlin',
            userName=info.get('user_name',''),userEmail=info['email'],spaceViewId=view,currentDatetime=now)),
            dict(id=step_id,type='user',userId=info['user_id'],value=[[prompt]],createdAt=now)]
        # The Notion UI (CDP capture 2026-10-20) keeps the config step and sends model/effort as an
        # 'updated-config' step right before the user step; config.model alone is ignored by the server.
        if model or effort:
            update = dict(model=model) if model else {}
            if effort:
                update['reasoningEffort'] = effort
            config_id = self.new_id(info['space_id'],'00aa')
            remember_own_config_id(config_id)  # never read back as a user choice (persisted, I46)
            body['transcript'].insert(len(body['transcript'])-1,
                dict(id=config_id,type='updated-config',value=update))
        body.update(spaceId=info['space_id'],threadId=thread_id,traceId=str(uuid.uuid4()),submittedUserStepId=step_id,
                    createThread=create,generateTitle=create)
        if create:
            body['threadParentPointer'] = {'table':'space','id':info['space_id'],'spaceId':info['space_id']}
        return body

    @staticmethod
    def new_id(space_id, tail='00aa'):
        space = uuid.UUID(space_id)
        # Legacy spaces do not encode a space short ID. The public client
        # falls back to UUIDv4 when no encoded short ID is available.
        if space.version != 8:
            return str(uuid.uuid4())
        # Notion IdCreator (module 817569) encodes UTC days since 2024-01-01
        # in the first 12 bits. A captured example is not a reusable prefix.
        day = int((time.time()-1704067200)//86400)
        if not 0 <= day < 4096:
            raise ValueError('Current time is outside the supported Notion ID date range')
        parts = str(space).split('-')
        head = f'{day:03x}{parts[0][3:]}-{parts[1]}-80{secrets.token_hex(1)}-{secrets.choice("89ab")}{secrets.token_hex(2)[:3]}-{tail}{secrets.token_hex(2)}'
        return head + (own_id_sign(head) if tail == OWN_ID_TAIL else secrets.token_hex(2))

    @staticmethod
    def thread_key(thread):
        tid = thread.get('id') or thread.get('thread_id') or thread.get('threadId')
        return str(uuid.UUID(tid)) if tid else ''

    def thread_records(self, client, ids):
        out = {}
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), 50):
            result = client.call('syncRecordValuesSpaceInitial', {'requests': [
                {'pointer': {'table': 'thread', 'id': t, 'spaceId': client.info['space_id']}, 'version': -1} for t in ids[i:i+50]]})
            out.update({k: unwrap(v) for k, v in result.get('recordMap', {}).get('thread', {}).items()})
        return out

    def apply_chat_pool(self, account_id, client):
        """Register the Main and apply the Coworker rule; Welcome chats stay unnamed, joined ones become Coworkers."""
        from .chat_pool import COWORK_TITLE, MAIN_TITLES, WELCOME_TITLE, pool_decision, pick_main
        from .cowork_names import is_pool_title
        account = self.store.get('accounts', account_id)
        workers = [w for w in self.store.all('workers') if w['account_id'] == account_id and w.get('state') != 'missing']
        ledger = account.get('team_threads') or {}
        # A reachable operator Main is the account's single Main: no pool chat is promoted next to it.
        main = None if self.operator_main(account_id) else pick_main(workers, (ledger.get('main') or {}).get('id', ''))
        if main and (main.get('purpose') != 'main' or not main.get('enabled')):
            main.update(purpose='main', enabled=True, welcome=True)
            self.store.put('workers', main)
            self.store.event('main_registered', message='Main chat registered: ' + main.get('label', ''), slot=account.get('slot'))
        if main and not (ledger.get('main') or {}).get('id'):
            ledger['main'] = {'id': main['thread_id'], 'pending': False}
            account['team_threads'] = ledger
            self.store.put('accounts', account)
        if main:
            from .main_repoint import repoint
            try:
                repoint(self.store, account_id, main)
            except Exception as exc:
                self.store.event('main_repoint_failed', message=str(exc)[:200], slot=account.get('slot'))
        pool = [w for w in workers if w.get('purpose', 'worker') == 'worker' and (not main or w['id'] != main['id'])]
        records = self.thread_records(client, [w['thread_id'] for w in pool
            if w.get('label') in (COWORK_TITLE, WELCOME_TITLE) or w.get('join_state') in ('pending', 'joined')])
        usable = 0
        for worker in pool:
            if worker.get('label') in MAIN_TITLES or not is_pool_title(worker.get('label') or ''):
                # Named chats (anything not titled like the Welcome chat) are never coworkers.
                changes, rename, event = dict(enabled=False, named=True), None, None
                label = (worker.get('label') or '').strip()
                if (label and label not in MAIN_TITLES and 'retired' not in label.lower() and not worker.get('retired')
                        and worker.get('state') not in ('missing', 'retired')):
                    # A chat the user renamed himself is his own chat: listed in NotionTool's Chats.
                    changes.update(purpose='private', user_set_title=True, app_chat=True)
            else:
                changes, rename, event = pool_decision(worker, records.get(worker['thread_id']))
            if rename:
                try:
                    self.rename_thread(client, worker['thread_id'], rename)
                    changes['label'] = rename
                except Exception:
                    changes.update(enabled=False, join_state=worker.get('join_state') or 'pending')
                    event = 'coworker_rename_failed'
            if any(worker.get(k) != v for k, v in changes.items()):
                worker.update(changes)
                self.store.put('workers', worker)
                if event:
                    self.store.event(event, message=worker.get('label', '') + ' ' + worker['thread_id'], slot=account.get('slot'))
                if event == 'coworker_joined':
                    try:
                        cb = getattr(self, 'on_coworker_joined', None)
                        if callable(cb):
                            cb()
                    except Exception:
                        pass
            usable += bool(worker.get('enabled') and worker.get('join_state') == 'joined')
        return {'main': main['id'] if main else '', 'coworkers': usable}

    def ensure_main_chat(self, account_id):
        """Local chat record for the account's Main; existing history is kept, never deleted."""
        from .chat_pool import pick_main
        from .store import uid
        account = self.store.get('accounts', account_id)
        workers = [w for w in self.store.all('workers') if w['account_id'] == account_id]
        main = pick_main(workers, ((account.get('team_threads') or {}).get('main') or {}).get('id', ''))
        if not main or main.get('purpose') != 'main':
            return None
        chat = next((c for c in self.store.all('chats') if c.get('main_worker') == main['id']), None)
        if not chat:
            chat = dict(id=uid('c'), title=account.get('label', account.get('email', '')) + ' · Main', main_worker=main['id'],
                        folder_id='', project_path='', created=time.time())
            self.store.put('chats', chat)
            self.store.event('main_chat_registered', message=chat['title'], slot=account.get('slot'))
        return chat

    def create_welcome(self, client, tid, model):
        """Create one Welcome chat. Returns (listed, clean). No retry: a timeout may still have created it."""
        steps, failed = [], False
        try:
            for line in client.transport.stream('runInferenceTranscript', self.welcome_body(client, tid, model), client.headers(), timeout=180):
                event = json.loads(line)
                if event.get('type') in ('patch-start', 'patch-sync'):
                    steps = (event.get('data') or {}).get('s') or steps
                elif event.get('type') == 'patch':
                    steps = apply_patch(steps, event.get('v'))
        except (OSError, json.JSONDecodeError):
            failed = True
        # HTTP 200 does not confirm a persisted chat; only Notion's chat list does.
        try:
            listed = any(self.thread_key(t) == str(uuid.UUID(tid)) for t in client.threads())
        except ChatListUnavailable:
            listed = False  # unreadable list: creation stays unconfirmed and is never resent
        clean = not failed and not any(isinstance(s, dict) and s.get('type') == 'error' for s in steps)
        return listed, clean

    def regreet_welcome(self, account_id, tid):
        """Send the Welcome greeting again into an existing Welcome Coworker that never joined.

        Never creates a chat: the thread must already be in Notion's chat list. Counts regreets."""
        worker = next((w for w in self.store.all('workers') if w.get('account_id') == account_id
                       and w.get('thread_id') == tid), None)
        if not worker or worker.get('join_state') != 'pending':
            return False
        client = self.client(account_id)
        known = {self.thread_key(t) for t in client.threads() if self.thread_key(t)}
        if str(uuid.UUID(tid)) not in known:
            raise ValueError('Welcome chat is not in the chat list; nothing was sent')
        worker['regreets'] = int(worker.get('regreets') or 0) + 1
        worker['regreet_at'] = time.time() + 300
        self.store.put('workers', worker)
        _listed, clean = self.create_welcome(client, tid, client.model())
        slot = (self.store.get('accounts', account_id) or {}).get('slot')
        self.store.event('welcome_resent' if clean else 'welcome_warning',
                         message=('Welcome greeting resent ' if clean else 'Resent greeting returned an upstream error ') + tid,
                         slot=slot)
        return clean

    def ensure_main(self, account_id, create=True):
        """Load the account's Main (title Main or Main Chat); create one Welcome chat named Main only if none exists.

        create=False never creates: without a usable Main it raises (team setup passes the user's create_main)."""
        from .chat_pool import MAIN_TITLE
        self.refresh(account_id)
        chat = self.ensure_main_chat(account_id)
        if chat:
            # Recovery path: if the ledger Main was pending (created but not yet renamed),
            # rename it now so it becomes 'Main' instead of staying 'Welcome to Notion'.
            account = self.store.get('accounts', account_id)
            ledger = account.get('team_threads') or {}
            item = ledger.get('main') or {}
            if item.get('pending') and item.get('id'):
                client = self.client(account_id)
                self.rename_thread(client, item['id'], MAIN_TITLE)
                account['team_threads']['main'] = {'id': item['id'], 'pending': False}
                self.store.put('accounts', account)
                self.store.event('team_setup', message='Pending Main renamed to Main (recovery)', slot=account.get('slot'))
            return {'account_id': account_id, 'chat_id': chat['id'], 'created': False}
        account = self.store.get('accounts', account_id)
        if account.get('state') == 'ai_disabled':
            raise ValueError(account.get('error') or 'Notion AI is disabled for this workspace.')
        if account.get('state') != 'ready':
            raise ValueError('Sign in to this account again before loading its Main chat')
        client = self.client(account_id)
        self.require_workspace_ai(account_id, client.identity())
        ledger = account.get('team_threads') or {}
        item = ledger.get('main') or {}
        known = {w['thread_id'] for w in self.store.all('workers') if w['account_id'] == account_id and w.get('state') != 'missing'}
        if item.get('pending') and item.get('id') in known:
            tid = item['id']
        elif item.get('pending'):
            raise ValueError('A previous Main creation is unconfirmed. Refresh the account before creating again.')
        elif not create:
            raise MainMissing('No usable Main or operator Main on this account; no Main was created. '
                              'Create one only on purpose (create_main=true).')
        else:
            model = client.model()
            tid = self.new_id(client.info['space_id'], '00a9')
            ledger['main'] = {'id': tid, 'pending': True}
            account['team_threads'] = ledger
            self.store.put('accounts', account)
            listed, clean = self.create_welcome(client, tid, model)
            if not listed:
                raise ValueError('Main creation is unconfirmed; refresh to reconcile before creating again')
            if not clean:
                self.store.event('welcome_warning', message='Main exists, but its greeting returned an upstream error', slot=account.get('slot'))
        self.rename_thread(client, tid, MAIN_TITLE)
        account = self.store.get('accounts', account_id)
        account['team_threads']['main'] = {'id': tid, 'pending': False}
        self.store.put('accounts', account)
        self.transaction(client, 'newUserHomeActions.savePostOnboardingAgentConfig', [{
            'pointer': {'table': 'user_settings', 'id': client.info['user_id']}, 'path': ['settings'], 'command': 'update',
            'args': {'post_onboarding_agent_config': {'thread_id': tid, 'has_clicked_all_set': False, 'space_id': client.info['space_id']}}}])
        self.refresh(account_id)
        chat = self.ensure_main_chat(account_id)
        if not chat:
            raise ValueError('Created Main chat is absent from the Notion chat list')
        self.store.event('team_setup', message='Main Welcome chat created', slot=account.get('slot'))
        return {'account_id': account_id, 'chat_id': chat['id'], 'created': True}

    def bridge_alive(self, account_id, url):
        """True while the account still has an enabled, live NotionTool MCP module for this URL."""
        client = self.client(account_id)
        client.identity()
        view = next((unwrap(v) for v in client.info.get('records', {}).get('space_view', {}).values()
                     if unwrap(v).get('space_id') == client.info['space_id']), None)
        if not view:
            return False
        ids = [m['pointer']['id'] for m in ((view.get('settings') or {}).get('agent_chat_modules') or [])
               if (m.get('pointer') or {}).get('table') == 'workflow_module' and m.get('defaultEnabled') is not False]
        if not ids:
            return False
        synced = client.call('syncRecordValuesSpaceInitial', {'requests': [
            {'pointer': {'table': 'workflow_module', 'id': i, 'spaceId': client.info['space_id']}, 'version': -1} for i in ids]})
        return any(value.get('alive', True) and ((value.get('data') or {}).get('serverUrl') or value.get('server_url')) == url
                   for value in (unwrap(v) for v in synced.get('recordMap', {}).get('workflow_module', {}).values()))

    def note_worker_mcp_failure(self, worker):
        """A worker chat could not use the MCP. Never reconnect from here: a reconnect could disturb the
        other Coworkers. Only schedule a read-only check of the account's MCP module for the next minute."""
        account = self.store.get('accounts', worker['account_id'])
        if not account:
            return
        account.update(bridge_checked_at=0, bridge_suspect_at=time.time())
        self.store.put('accounts', account)
        self.store.event('worker_mcp_unavailable', message='NotionTool MCP was unavailable in ' + str(worker.get('label') or 'a worker chat')
                         + '; checking the account connection without interrupting other Coworkers', slot=account.get('slot'))

    def provision(self, account_id, count):
        count = int(count)
        if not 1<=count<=40:
            raise ValueError('Choose between 1 and 40 new worker chats')
        client = self.client(account_id)
        self.require_workspace_ai(account_id, client.identity())
        model = client.model()
        created, new_ids = 0, []
        for _ in range(count):
            tid = self.new_id(client.info['space_id'],'00a9')
            # No automatic retry: a creation timeout could still have created the thread.
            listed, clean = self.create_welcome(client, tid, model)
            if not listed:
                self.store.event('welcome_warning', message='Coworker creation is unconfirmed; refresh before creating again')
                break
            if not clean:
                self.store.event('welcome_warning', message='Coworker exists, but its greeting returned an upstream error')
            created += 1
            new_ids.append(str(uuid.UUID(tid)))
        self.refresh(account_id)
        # New Welcome chats keep their name and stay unused until Notion confirms the join.
        for worker in self.store.all('workers'):
            if worker['account_id'] == account_id and worker['thread_id'] in new_ids:
                worker.update(join_state='pending', enabled=False, welcome=True, purpose='worker')
                self.store.put('workers', worker)
        self.apply_chat_pool(account_id, client)
        joined = sum(1 for w in self.store.all('workers') if w['account_id'] == account_id
                     and w['thread_id'] in new_ids and w.get('join_state') == 'joined')
        return {'created': created, 'joined': joined, 'pending': created - joined}

    def welcome_body(self,client,tid,model):
        """Exactly the captured Refresher Welcome protocol; no browser onboarding clicks."""
        info=client.info
        config=copy.deepcopy(self.welcome['config'])
        config.update(agentMessageKey='unboxing_m5',model=model,modelFromUser=True,enableComputer=False,
                      agentMemorySettings={'useMemories':True,'excludeChatFromMemories':True})
        view=next((unwrap(v).get('id',k) for k,v in info.get('records',{}).get('space_view',{}).items()
                   if unwrap(v).get('space_id')==info['space_id']),'')
        body=copy.deepcopy(self.welcome['body'])
        body.update(traceId=str(uuid.uuid4()),spaceId=info['space_id'],threadId=tid,agentMessageKey='unboxing_m5',
            threadParentPointer={'table':'space','id':info['space_id'],'spaceId':info['space_id']},transcript=[
                dict(id=self.new_id(info['space_id']),type='config',value=config),
                dict(id=self.new_id(info['space_id']),type='context',value=dict(timezone='Europe/Berlin',
                    userName=info.get('user_name',''),userId=info['user_id'],userEmail=info['email'],
                    spaceName=next((s['name'] for s in info['spaces'] if s['id']==info['space_id']),''),
                    spaceId=info['space_id'],spaceViewId=view,currentDatetime=datetime.datetime.now().astimezone().isoformat(),
                    surface='post_onboarding_agent',agentAccessory='pencil')),
                dict(id=self.new_id(info['space_id']),type='title',value='Welcome to Notion'),
                dict(id=self.new_id(info['space_id']),type='user-injected',actualMessage=[[
                    'Start the onboarding conversation now. Send your first onboarding message. The user locale is en-US.']],
                    userId=info['user_id'],createdAt=datetime.datetime.now().astimezone().isoformat())])
        return body

    def transaction(self,client,action,operations):
        return client.call('saveTransactionsFanout',{'requestId':str(uuid.uuid4()),'transactions':[{
            'id':str(uuid.uuid4()),'spaceId':client.info['space_id'],
            'debug':{'userAction':action,'clientCommitTimeMs':int(time.time()*1000)},'operations':operations}]})

    def rename_thread(self,client,tid,title):
        self.transaction(client,'agentThreads.rename',[{'pointer':{'table':'thread','id':tid,'spaceId':client.info['space_id']},
            'path':['data','title'],'command':'set','args':title}])

    def delete_thread(self, client, tid):
        """Same as the chat menu ... > Delete (verified on hg 2026-10-16): thread alive=false, then gone from the list."""
        self.transaction(client, 'agentThreads.delete', [{'pointer': {'table': 'thread', 'id': tid, 'spaceId': client.info['space_id']},
            'path': [], 'command': 'update', 'args': {'alive': False}}])
        record = self.thread_records(client, [tid]).get(tid) or {}
        if record.get('alive') is not False:
            raise ValueError('Notion did not confirm the deletion of chat ' + tid)
        return True

    def ensure_thread_model(self, client, thread_id, model):
        """Verify persisted legacy config; a request's new config can be ignored."""
        space = client.info['space_id']
        def records(table, ids):
            if not ids:
                return {}
            result = client.call('syncRecordValuesSpaceInitial', {'requests': [
                {'pointer': {'table': table, 'id': ident, 'spaceId': space}, 'version': -1}
                for ident in ids]})
            return {ident: unwrap(value) for ident, value in result.get('recordMap', {}).get(table, {}).items()}
        thread = records('thread', [thread_id]).get(thread_id, {})
        if thread.get('space_id') != space or thread.get('type') != 'workflow' or thread.get('alive') is False:
            raise ValueError('Cannot verify the model of this owned workflow chat')
        messages = records('thread_message', thread.get('messages', []))
        configs = {ident: value['step']['value'] for ident, value in messages.items()
                   if value.get('parent_id') == thread_id and value.get('step', {}).get('type') == 'config'
                   and value['step'].get('value', {}).get('type') == 'workflow'}
        if not configs:
            raise ValueError('Missing saved workflow model configuration; no inference submitted')
        changed = [ident for ident, config in configs.items()
                   if config.get('model') != model or config.get('modelFromUser') is not True
                   or config.get('agentMemorySettings', {}).get('useMemories') is not True]
        if changed:
            self.transaction(client, 'NotionTool.selectExplicitModel', [
                {'pointer': {'table': 'thread_message', 'id': ident, 'spaceId': space},
                 'path': ['step', 'value'], 'command': 'update',
                 'args': {'model': model, 'modelFromUser': True,
                          'agentMemorySettings': {'useMemories': True, 'excludeChatFromMemories': True}}} for ident in changed])
            confirmed = records('thread_message', changed)
            if any(confirmed.get(ident, {}).get('step', {}).get('value', {}).get('model') != model
                   or confirmed.get(ident, {}).get('step', {}).get('value', {}).get('modelFromUser') is not True
                   or confirmed.get(ident, {}).get('step', {}).get('value', {}).get('agentMemorySettings', {}).get('useMemories') is not True
                   for ident in changed):
                raise ValueError('Notion did not confirm the selected explicit model; no inference submitted')
        return {'model': model, 'explicit': True, 'corrected': len(changed)}

    def operator_main(self, account_id, known=None):
        """The account's reachable operator Main worker, else None.

        Operator Main = purpose 'operator', or a 'main' worker whose thread is the operator chat's thread.
        known: thread ids in Notion's chat list (reachability); None skips that check."""
        workers = [w for w in self.store.all('workers') if w.get('account_id') == account_id
                   and w.get('state') != 'missing' and w.get('join_state') != 'released']
        operator_threads = set()
        for chat in self.store.all('chats'):
            if chat.get('operator') and chat.get('state') != 'released' and chat.get('main_worker'):
                owner = self.store.get('workers', chat['main_worker']) or {}
                if owner.get('account_id') == account_id and owner.get('thread_id'):
                    operator_threads.add(owner['thread_id'])
        for worker in workers:
            if worker.get('purpose') == 'operator' or (worker.get('purpose') == 'main' and worker.get('thread_id') in operator_threads):
                if known is None or worker.get('thread_id') in known:
                    return worker
        return None

    def ensure_team(self,account_id,create_main=False):
        """Idempotent desired team: one Main and N Welcome Coworkers (team_size.py, from the settings).

        A reachable operator Main counts as the Main: no Main is created and the ledger 'main' points at
        the operator thread. Without any usable Main, a new one is created only when create_main=True.
        Spare joined/pending pool chats fill empty roles first; nothing is created twice. Roles above N
        stay in the ledger untouched (never deleted). Coworkers are renamed only after joining."""
        from .team_size import team_roles
        client=self.client(account_id)
        known={self.thread_key(t) for t in client.threads() if self.thread_key(t)}
        operator=self.operator_main(account_id,known)
        if operator:
            # A reachable operator Main is the Main: never create one, point the ledger at it.
            account=self.store.get('accounts',account_id)
            ledger=account.get('team_threads') or {}
            if (ledger.get('main') or {}).get('id')!=operator['thread_id'] or (ledger.get('main') or {}).get('pending'):
                ledger['main']={'id':operator['thread_id'],'pending':False}
                account['team_threads']=ledger
                self.store.put('accounts',account)
                self.store.event('team_setup',message='Ledger Main points at the operator Main; no Main created',slot=account.get('slot'))
        else:
            try:
                self.ensure_main(account_id,create=bool(create_main))
            except MainMissing as exc:
                # No Main without the user's explicit create_main=true; Coworkers are still filled.
                self.store.event('team_setup',message=str(exc),slot=(self.store.get('accounts',account_id) or {}).get('slot'))
        model=client.model()
        account=self.store.get('accounts',account_id)
        ledger=account.get('team_threads') or {}
        roles=team_roles(self.store.settings())
        # Existing pool chats fill empty slots first; nothing is created twice.
        taken={(item or {}).get('id') for item in ledger.values()}
        spare=[w for w in self.store.all('workers') if w['account_id']==account_id and w.get('purpose','worker')=='worker'
               and w.get('state')!='missing' and w.get('join_state') in ('joined','pending') and w['thread_id'] not in taken]
        for role in roles:
            if not ledger.get(role) and spare:
                ledger[role]={'id':spare.pop(0)['thread_id'],'pending':False}
        account['team_threads']=ledger
        self.store.put('accounts',account)
        new_ids=[]
        for role in roles:
            item=ledger.get(role)
            if item and item['id'] not in known:
                if item.get('pending'):
                    raise ValueError('A previous Welcome creation is unconfirmed. Refresh the account before creating again.')
                raise ValueError('A managed Welcome chat is missing; existing chats were preserved')
            if item:
                continue
            if new_ids:
                time.sleep(float(getattr(self,'welcome_gap_s',3.0)))  # paced: rapid creations lose greetings
            tid=self.new_id(client.info['space_id'],'00a9')
            item={'id':tid,'pending':True}
            ledger[role]=item
            account['team_threads']=ledger
            self.store.put('accounts',account)
            listed,clean=self.create_welcome(client,tid,model)
            if not listed:
                raise ValueError('Welcome creation is unconfirmed; refresh to reconcile before creating again')
            if not clean:
                self.store.event('welcome_warning',message=role+' exists, but its initial greeting returned an upstream error',slot=account['slot'])
            item['pending']=False
            account['team_threads']=ledger
            self.store.put('accounts',account)
            new_ids.append(str(uuid.UUID(tid)))
            self.store.event('team_setup',message=role+' Welcome chat created; waiting for its join',slot=account['slot'])
        self.refresh(account_id)
        for worker in self.store.all('workers'):
            if worker['account_id']==account_id and worker['thread_id'] in new_ids:
                worker.update(join_state='pending',enabled=False,welcome=True,purpose='worker',pool_admitted=True,
                              welcome_at=time.time())
                self.store.put('workers',worker)
        self.apply_chat_pool(account_id,client)
        managed={ledger[r]['id'] for r in roles}
        team=[w for w in self.store.all('workers') if w['account_id']==account_id and w['thread_id'] in managed]
        if len(team)<len(managed):
            raise ValueError('Created Welcome chat is absent from the Notion chat list')
        joined=sum(1 for w in team if w.get('join_state')=='joined' and w.get('enabled'))
        from .setup_status import account_setup
        status=account_setup(self.store.get('accounts',account_id),self.store.all('workers'),size=len(roles))
        return {'account_id':account_id,'welcome_chats':len(managed)+1,'workers':joined,'pending_workers':len(managed)-joined,
                'main_chats':1,'verified':status['complete'],'chat_verified':True,'setup':status,'team_size':len(roles)}

    def _config_saved(self, thread_id, config_key):
        # A config check confirmed before a restart stays valid: only our own confirmed write changes it.
        saved = (self.store.get('thread_configs', thread_id) if hasattr(self.store, 'get') else None) or {}
        if list(saved.get('key') or []) == list(config_key):
            self.__dict__.setdefault('confirmed_configs', {})[thread_id] = config_key
            return True
        return False

    def _inline_handoff_text(self, worker, task):
        """Checkpoint text for an inline handoff (token saving, setting handoff_excerpt_chars).

        Coworkers (purpose 'worker') get at most handoff_excerpt_chars characters, cut at a line end,
        plus the path of a local file with the full checkpoint. 0, a short checkpoint or any failure
        sends the full text as before. Main and all other chats always get the full text unchanged.
        The file is separate from the ack-token handoff file, so handoff_token/handoff_delivered stay as they are."""
        text = worker['handoff']
        if worker.get('purpose') != 'worker':
            return text
        limit = handoff_excerpt_limit(self.store.settings())
        if limit <= 0 or len(text) <= limit:
            return text
        run_id = task.get('run_id', '')
        try:
            from . import handoff_files
            folder = str(self.store.directory / 'handoffs')
            key = handoff_files.file_key(worker['id'] + ':excerpt')
            path = handoff_files.handoff_path(folder, key)
            try:
                with open(path, encoding='utf-8') as fh:
                    old = json.load(fh)
                if old.get('handoff') != handoff_files.redact_text(text) or old.get('new_thread') != worker['thread_id']:
                    raise ValueError('stale')
            except FileNotFoundError:
                pass
            except Exception:
                os.remove(path)  # an older checkpoint: the current one replaces it
            record = handoff_files.write_handoff(folder, key, dict(handoff=text, purpose='handoff_excerpt_source',
                old_thread=(worker.get('retired_threads') or [''])[-1], new_thread=worker['thread_id'],
                run_id=run_id, task_id=task.get('id', '')), time.time())
            block = handoff_excerpt_block(text, limit, record['path'])
        except Exception as exc:
            self.store.event('handoff_excerpt_failed', run_id, worker_id=worker.get('id', ''),
                             message=str(exc)[:160] + '; full checkpoint is sent inline')
            return text
        if len(block) >= len(text):
            return text
        self.store.event('handoff_excerpt', run_id, worker_id=worker['id'], message=record['path'],
                         chars=len(block), full_chars=len(text))
        return block

    def run(self, worker, task, prompt, cancel, update):
        account = self.store.get('accounts',worker['account_id'])
        if not account.get('model'):
            raise ValueError('Opus 5.5 is unavailable; refresh the account')
        _t0 = time.monotonic()
        _phase = {}
        client = self.client(account['id'])
        _phase['client'] = time.monotonic() - _t0
        # Fetch head before sending so recovery cannot mistake old responses for this attempt.
        import concurrent.futures as _cf
        # The busy check is an independent read: run it while the head loads instead of after it.
        with _cf.ThreadPoolExecutor(1) as _pool:
            _busy_future = _pool.submit(self.thread_busy, client, worker['thread_id'])
            # 30 entries are enough for the head (session + sequence); 100 made multi-MB reads per send.
            transcript = client.transcript(worker['thread_id'], limit=30)
            _phase['transcript'] = time.monotonic() - _t0
            _busy_now = _busy_future.result()
        session = transcript.get('session') or {}
        if session.get('status') not in (None,'idle','completed') or transcript.get('session_pending'):
            # Nothing was submitted. An externally busy thread (e.g. the operator's own turn in this chat) is not a
            # rate limit: no attempt is used and the account is not cooled down (Phase 4 fix).
            raise RateLimited(10, 'This chat is busy with another turn (session ' + str(session.get('status') or 'pending') +
                              '); nothing was sent yet', free=True, scope='thread')
        if _busy_now:
            raise RateLimited(5, 'Notion is still finishing the previous turn in this chat; nothing was sent yet',
                              free=True, scope='thread')
        _phase['busy'] = time.monotonic() - _t0
        from .chat_context import ChatContext
        original_thread, original_generation = worker['thread_id'], worker.get('generation', 0)
        if worker.get('id'):
            worker = ChatContext(self.store).rotate(self, client, worker, task, transcript, cancel)
        _phase['rotate'] = time.monotonic() - _t0
        # A new generation may have run a checkpoint turn. Read the current head before sending.
        if worker['thread_id'] != original_thread or worker.get('generation', 0) != original_generation:
            transcript = client.transcript(worker['thread_id'])
            session = transcript.get('session') or {}
            if transcript.get('session_pending') or session.get('status') not in (None, 'idle', 'completed'):
                raise RateLimited(10, 'This chat is busy after the context checkpoint; the assignment was not sent yet',
                                  free=True, scope='thread')
            if self.thread_busy(client, worker['thread_id']):
                raise RateLimited(5, 'Notion is still finishing the context checkpoint in this chat; the assignment was not sent yet',
                                  free=True, scope='thread')
        handoff_included = bool(worker.get('handoff') and not worker.get('handoff_delivered'))
        handoff_token = ''
        if handoff_included and worker.get('purpose') == 'worker' and self.store.settings().get('handoff_mode', 'file') == 'file':
            # Phase 5b: the checkpoint goes to a local file; the chat reads it with read_file and acks with a token.
            try:
                import os as _os
                from . import handoff_files
                folder = str(self.store.directory / 'handoffs')
                handoff_files.gc(folder, time.time())
                path = handoff_files.handoff_path(folder, handoff_files.file_key(worker['id']))
                try:
                    with open(path, encoding='utf-8') as fh:
                        if json.load(fh).get('new_thread') != worker['thread_id']:
                            raise ValueError('stale')
                except FileNotFoundError:
                    pass
                except Exception:
                    _os.remove(path)  # an older, unconfirmed handoff for another chat is replaced
                record = handoff_files.write_handoff(folder, handoff_files.file_key(worker['id']), dict(handoff=worker['handoff'],
                    old_thread=(worker.get('retired_threads') or [''])[-1], new_thread=worker['thread_id'],
                    run_id=task.get('run_id', ''), task_id=task['id']), time.time())
                prompt = (handoff_files.first_message(record) +
                          '\n\nAfter the token line, do the current assignment:\n' + prompt)
                handoff_token = record['token']
                self.store.event('handoff_file_written', task.get('run_id', ''), worker_id=worker['id'],
                                 message=record['path'], chars=len(worker['handoff']))
            except Exception as exc:
                self.store.event('handoff_file_failed', task.get('run_id', ''), worker_id=worker['id'],
                                 message=str(exc)[:160] + '; checkpoint is sent inline')
                handoff_token = ''
        if handoff_included and not handoff_token:
            # Without a server-side reset the earlier history stays visible; mark it as superseded.
            note = '' if worker.get('context_reset') else ('\n\nEarlier messages in this chat are superseded by '
                                                          'this checkpoint; use them only as reference history.')
            prompt = self._inline_handoff_text(worker, task) + note + '\n\nCurrent assignment (continue from the checkpoint):\n' + prompt
        inherited = (inherit_main_config(client, worker['thread_id'])
                     if worker.get('purpose') in MAIN_OWN_CONFIG else None)
        if inherited is not None:
            # Main chats keep the model + reasoning the user picked in the Notion menu (no override).
            selected, model, effort, effort_reason = inherited
        else:
            selected = 'opus' if worker.get('purpose') == 'main' else task.get('model', 'opus')
            model = client.model(selected)
            # Rule I38: a per-task effort set by the Main wins (clamped to the model); else the automatic choice.
            effort, effort_reason = choose_task_effort(client, model, worker.get('purpose', ''),
                                                       task)
        # The saved chat config changes only through our own confirmed write: check once per chat generation.
        config_key = (worker['thread_id'], worker.get('generation', 0), model)
        if getattr(self, 'confirmed_configs', {}).get(worker['thread_id']) != config_key and not self._config_saved(worker['thread_id'], config_key):
            # Saved config keeps the thread default model (same as body() config.model); the requested
            # model/effort travel only in the 'updated-config' step (live proof 2026-10-20).
            self.ensure_thread_model(client, worker['thread_id'],
                                     BASE_CONFIG_MODEL)
            self.__dict__.setdefault('confirmed_configs', {})[worker['thread_id']] = config_key
            if hasattr(self.store, 'put'):
                self.store.put('thread_configs', dict(id=worker['thread_id'], key=list(config_key), at=time.time()))
        _phase['config'] = time.monotonic() - _t0
        # 6-hour token budget (token_budget.py): nothing is sent while the window is used up.
        from .token_budget import refuse_if_exhausted, refuse_if_burst, charge_send, charge_reply
        refuse_if_exhausted(self.store)
        refuse_if_burst(self.store, worker, task=task)  # burst guard + step budget (estimate) for coworkers
        with self.store.lock:
            saved = self.store.get('tasks',task['id'])
            saved['before_seq'] = session.get('sequence',0)
            saved['thread_id'] = worker['thread_id']
            saved['submitted_prompt'] = prompt
            saved['handoff_included'] = handoff_included
            saved['handoff_token'] = handoff_token
            saved['step_id'] = self.new_id(client.info['space_id'])
            saved['sent_at'] = time.time()
            saved['request_fingerprint'] = hashlib.sha256(prompt.encode()).hexdigest()
            saved.update(model=selected, assigned_model_id=model, effort=effort, effort_reason=effort_reason)
            if inherited is not None:
                saved['model_reason'] = effort_reason
            elif worker.get('purpose') == 'main':
                saved['model_reason'] = 'Main chat: Opus 5.5 is the default model for the Main chat'
            saved['submission_phase'] = 'inference'
            self.store.put('tasks',saved)
            task = saved
        if cancel.is_set():
            raise ValueError('Assignment cancelled before submission')
        # The assignment token is only offered when tools are needed (project, files, visual work).
        body = self.body(client,worker['thread_id'],task['step_id'],prompt,model,computer='assignment_token' in prompt,
                         effort=final_effort(client, model, task.get('effort'), worker.get('purpose', '')))
        if worker.get('context_reset'):
            body['isPartialTranscript'] = False  # replace the context inside the same chat
            with self.store.lock:
                saved = self.store.get('tasks',task['id'])
                saved['context_reset'] = True
                self.store.put('tasks',saved)
                task = saved
        steps = []
        started,last_update = time.monotonic(),0
        trace = dict(task_id=task['id'], worker_id=worker.get('id', ''), attempt=task.get('attempts', 0))
        run_id = task.get('run_id', '')
        _phase['body'] = time.monotonic() - _t0
        self.store.event('presend_timing', run_id, commit=False, **trace, **{k: round(v, 2) for k, v in _phase.items()})
        self.store.event('remote_sent', run_id, prompt_chars=len(prompt), **trace)
        charge_send(self.store, worker, prompt)  # counted even if the chat is deleted later
        from . import step_guard
        step_guard.note_start(self.store, worker)  # start spacing / starts per 5 min
        counted = 0  # steps of this turn already counted by the step guard (live, not only at the end)
        seen_output, lines, reported = False, 0, []
        def remote_done(outcome, error_class='', detail=''):
            if not reported:
                reported.append(outcome)
                extra = dict(detail=str(detail)[:200]) if detail else {}
                self.store.event('remote_done', run_id, outcome=outcome, error_class=error_class, lines=lines,
                                 seconds=round(time.monotonic()-started, 3), **extra, **trace)
        # The stream gets its own cancel: the stall watch may end a frozen stream without cancelling the task.
        stream_cancel, stream_alive, last_chunk = threading.Event(), [True], [time.monotonic()]
        threading.Thread(target=self._stall_watch, name='stream-stall-watch', daemon=True,
                         args=(client, worker, cancel, stream_cancel, stream_alive, last_chunk, run_id, trace)).start()
        try:
            for line in client.transport.stream('runInferenceTranscript',body,client.headers(),timeout=60,cancel=stream_cancel):
                last_chunk[0] = time.monotonic()
                if cancel.is_set() or time.monotonic()-started>task_limit(self.store.settings(), task):
                    raise PendingRemote()
                if not str(line).strip():
                    continue  # blank keep-alive lines are not output
                event = json.loads(line)
                lines += 1
                if event.get('type') in ('patch-start','patch-sync'):
                    steps = (event.get('data') or {}).get('s') or steps
                elif event.get('type')=='patch':
                    steps = apply_patch(steps,event.get('v'))
                if not seen_output and stream_text(steps).strip():
                    seen_output = True
                    self.mark_task(task['id'], first_output_at=time.time())
                    self.store.event('first_output', run_id, seconds=round(time.monotonic()-started, 3), **trace)
                if time.monotonic()-last_update>0.6:
                    update(stream_text(steps))
                    last_update = time.monotonic()
                    counted = step_guard.count_new(self.store, worker, steps, counted)
            errors = [s for s in steps if isinstance(s,dict) and s.get('type')=='error']
            counted = step_guard.count_new(self.store, worker, steps, counted)
            if worker.get('purpose') == 'worker' and not errors:
                step_guard.note_turn_steps(self.store, task, counted)  # learns the steps per turn
            for err in errors:
                # Notion's fair-use limit (UserRateLimitResponse / AI disabled): every chat of the account pauses.
                detail = json.dumps(err, default=str)
                if step_guard.is_provider_limit(detail):
                    step_guard.note_provider_limit(self.store, worker, detail, key='%s:%s' % (
                        worker.get('thread_id', ''), err.get('id') or task.get('step_id') or task['id']))
            text = stream_text(steps)
            charge_reply(self.store, worker, text)
            from .chat_pool import script_mode_failure
            if script_mode_failure(steps):
                self.note_worker_mcp_failure(worker)
            self.mark_task(task['id'], remote_done_at=time.time())
            remote_done('stream_end')
            # Streaming steps can contain stale text or provider placeholders.
            # Only the committed answer to our own turn can finish an assignment.
            return self.recover(worker,task,cancel,prefetch=True)
        except NotionHttpError as exc:
            remote_done('http_'+str(exc.status), http_class(exc.status))
            if exc.status==429:
                raise RateLimited(exc.retry_after) from None
            if exc.status==404:
                raise ThreadMissing(worker.get('thread_id', ''), 'runInferenceTranscript') from None
            raise ValueError(f'Notion inference: HTTP {exc.status}') from None
        except (OSError,TimeoutError,json.JSONDecodeError) as exc:
            # Keep the cause visible (empty stream, interrupted Edge request, timeout).
            remote_done('unknown', 'REMOTE_UNKNOWN', f'{type(exc).__name__}: {exc}')
            if lines == 0:
                # Empty stream: the turn may still have run. Reconcile with this task's own transcript; never resend.
                return self.reconcile_empty_stream(worker, task, cancel, run_id, trace)
            raise PendingRemote() from None
        except PendingRemote:
            remote_done('unconfirmed', 'REMOTE_UNKNOWN')
            raise
        finally:
            stream_alive[0] = False

    def turn_outcome(self, client, thread_id):
        """Notion's own record of the latest turn: (latest user/trigger ms, completed ms, outcome status)."""
        result = client.call('syncRecordValuesSpaceInitial', {'requests': [
            {'pointer': {'table': 'thread', 'id': thread_id, 'spaceId': client.info['space_id']}, 'version': -1}]})
        found = result.get('recordMap', {}).get('thread', {})
        thread = unwrap(found[thread_id]) if thread_id in found else {}
        if isinstance(thread.get('value'), dict) and 'id' in thread['value']:
            thread = thread['value']  # live 2026-10-22: {spaceId, value: {value: record}}
        if not thread:
            raise LookupError('thread record not returned')
        outcome = (thread.get('data') or {}).get('last_turn_outcome') or {}
        return (float(thread.get('latest_user_or_trigger_time') or 0), float(outcome.get('completed_time') or 0),
                outcome.get('status') or '')

    def _stall_watch(self, client, worker, cancel, stream_cancel, alive, last_chunk, run_id='', trace=None):
        """Live 22 Oct 2026: Notion keeps the runInferenceTranscript response open after the turn is finished
        (Coworker 4: turn completed 05:46:42, stream still open at 06:11; 6 of 7 streams since 04:54 ended only
        by our own 900 s abort). The stream end is therefore no longer the signal: Notion writes the end of a turn
        to the thread record (last_turn_outcome). While the stream is silent, that record is read; once it shows
        our turn as completed the stream is closed and the normal reconcile reads our committed reply.
        Never resends, never ends a running turn."""
        trace = trace or {}
        poll = float(getattr(self, 'stream_turn_poll_s', 5.0))
        quiet = float(getattr(self, 'stream_turn_quiet_s', 3.0))  # only read the record while no bytes arrive
        sent_ms = time.time() * 1000 - 5000
        failures = 0
        while alive[0] and not stream_cancel.is_set():
            if cancel is not None and cancel.wait(poll):
                stream_cancel.set()  # the task itself was cancelled: pass it on to the stream
                return
            if cancel is None:
                time.sleep(poll)
            if not alive[0] or time.monotonic() - last_chunk[0] < quiet:
                continue
            try:
                latest, completed, status = self.turn_outcome(client, worker['thread_id'])
                failures = 0
            except Exception as exc:
                failures += 1
                if failures in (1, 10, 100):
                    self.store.event('stream_turn_check_failed', run_id, failures=failures,
                                     message=(type(exc).__name__ + ': ' + str(exc))[:200], **trace)
                continue
            if completed >= sent_ms and completed >= latest and alive[0]:
                self.store.event('stream_turn_done', run_id, outcome=status,
                                 idle_s=round(time.monotonic() - last_chunk[0], 1),
                                 message='Notion finished the turn but kept the stream open; reading the reply from the chat',
                                 **trace)
                stream_cancel.set()
                return

    def mark_task(self, task_id, **fields):
        # Persist telemetry timestamps on the task without touching its state.
        with self.store.lock:
            saved = self.store.get('tasks', task_id)
            if saved:
                saved.update(fields)
                self.store.put('tasks', saved)

    def reconcile_empty_stream(self, worker, task, cancel, run_id='', trace=None):
        """An empty reply stream is no proof of failure: accept only our own committed reply, else stay unconfirmed."""
        try:
            reply = self.recover(worker, task, cancel)
        except ResetNotAccepted:
            raise PendingRemote() from None  # undecided after an empty stream: reconcile later, never resend
        self.mark_task(task['id'], remote_done_at=time.time())
        self.store.event('empty_stream_reconciled', run_id, **(trace or {}))
        return reply

    RECOVER_WINDOW = 12  # newest entries read first after stream end; then 30, then the full 100, only if our marker is not inside

    def _prefetch_thread(self, worker):
        # Stream end only: read the thread record for the model check while the transcript loads.
        client = self.client(worker['account_id'])
        thread_id = worker['thread_id']
        def load():
            try:
                with request_priority('verify'):
                    result = client.call('syncRecordValuesSpaceInitial', {'requests': [
                        {'pointer': {'table': 'thread', 'id': thread_id, 'spaceId': client.info['space_id']}, 'version': -1}]})
                value = result.get('recordMap', {}).get('thread', {}).get(thread_id)
                return unwrap(value) if value else None
            except Exception:
                return None  # the check then reads the record itself
        pool = getattr(self, '_prefetch_pool', None)
        if pool is None:
            from concurrent.futures import ThreadPoolExecutor
            pool = self._prefetch_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='verify-prefetch')
        return pool.submit(load)

    def recover(self, worker, task, cancel, limit=None, prefetch=None):
        # Only the chat that ran this task may confirm it; never read another chat's transcript.
        thread_id = str(task.get('thread_id') or '')
        if not thread_id:
            raise PendingRemote(state='unknown', reason='task has no chat id')
        if prefetch is True:
            prefetch = self._prefetch_thread(worker) if worker.get('thread_id') == thread_id and worker.get('account_id') else None
        _rt_wall, _rt0 = time.time(), time.monotonic()
        try:
            with request_priority('verify'):
                transcript = self.client(worker['account_id']).transcript(thread_id, limit=limit or self.RECOVER_WINDOW)
        except ThreadMissing:
            raise  # HTTP 404 for the task's own chat: the worker is missing
        except Exception as exc:
            # B46: a failed read was silently swallowed, so a finished coworker could stay 'uncertain' forever.
            transcript = self.recover_native(worker, thread_id, limit or self.RECOVER_WINDOW, task, exc)
            if transcript is None:
                raise PendingRemote(state='unknown', reason='transcript read failed') from None
        from .chat_context import ChatContext
        observed_worker = dict(worker, thread_id=thread_id)
        _rt1 = time.monotonic()
        pool = getattr(self, '_prefetch_pool', None)
        if prefetch is not None and pool is not None:
            # Stream end: token bookkeeping only, so it must not delay the finish.
            def _observe(store=self.store, w=observed_worker, t=transcript, run=task.get('run_id', '')):
                try:
                    ChatContext(store).observe(w, t)
                except Exception as exc:
                    store.event('context_observe_failed', run, message=str(exc)[:160])
            pool.submit(_observe)
        else:
            ChatContext(self.store).observe(observed_worker, transcript)
        try:
            self.store.event('recover_timing', task.get('run_id', ''), task_id=task.get('id', ''), started=round(_rt_wall, 3),
                             transcript_s=round(_rt1 - _rt0, 3), observe_s=round(time.monotonic() - _rt1, 3), window=limit or self.RECOVER_WINDOW)
        except Exception:
            pass
        entities = [p.get('entity',{}) for p in transcript.get('patches',[]) if p.get('op')=='put']
        fresh = [e for e in entities if e.get('sequence',0)>task.get('before_seq',0)]
        session=transcript.get('session') or {}
        users=[e for e in fresh if e.get('kind')=='user_message']
        markers=[e for e in users if task.get('step_id') and
                 (e.get('id')==task['step_id'] or e.get('client_message_id')==task['step_id'])]
        if not markers and task.get('request_fingerprint'):
            # Text-hash fallback: only one exact match at or after before_seq counts; otherwise reconcile, never resend.
            hashed=[e for e in entities if e.get('kind')=='user_message' and
                hashlib.sha256(text_content(e.get('content',e.get('text',[]))).encode()).hexdigest()==task['request_fingerprint']]
            if len(hashed)==1 and hashed[0].get('sequence',0)>=task.get('before_seq',0):
                markers=hashed
        if not markers and (limit or self.RECOVER_WINDOW) < 100:
            wider = 30 if (limit or self.RECOVER_WINDOW) < 30 else 100
            return self.recover(worker, task, cancel, limit=wider, prefetch=prefetch)  # long turn: the marker is older than the small window
        if not markers:
            if task.get('context_reset') and not_accepted(task, transcript, fresh):
                raise ResetNotAccepted()
            raise PendingRemote()
        marker=min(markers,key=lambda e:e.get('sequence',0))
        after=marker.get('sequence',0)
        resumes=set((self.store.get('tasks',task.get('id',''),{}) or {}).get('resume_steps') or [])  # automatic continue messages stay inside this turn
        until=min([e.get('sequence',0) for e in fresh if e.get('kind')=='user_message' and e.get('sequence',0)>after
                   and not ({e.get('id'),e.get('client_message_id')} & resumes)],default=float('inf'))
        own_turn=[e for e in fresh if after<e.get('sequence',0)<until]
        completed = any(e.get('kind')=='turn_completed' for e in own_turn) or (session.get('status')=='idle' and not transcript.get('session_pending'))
        text = '\n'.join(text_content(e.get('content',[])) for e in sorted(own_turn,key=lambda e:e.get('sequence',0))
                         if e.get('kind')=='assistant_message')
        if completed and text:
            with request_priority('verify'):
                self.verify_response_model(worker,task,**({'prefetch':prefetch} if prefetch else {}))
            if (task.get('handoff_included') or task.get('context_reset')) and worker.get('id'):
                with self.store.lock:
                    fresh_worker = self.store.get('workers', worker['id'])
                    if fresh_worker and fresh_worker['thread_id'] == task.get('thread_id'):
                        fresh_worker['handoff_delivered'] = True
                        fresh_worker['context_reset'] = False
                        self.store.put('workers', fresh_worker)
            token = task.get('handoff_token') or (self.store.get('tasks', task.get('id', ''), {}) or {}).get('handoff_token')
            if token and worker.get('id'):
                from . import handoff_files
                folder = str(self.store.directory / 'handoffs')
                acked = handoff_files.verify_ack(text, token)
                try:
                    if acked:
                        handoff_files.mark_acked(folder, handoff_files.file_key(worker['id']), time.time())
                    handoff_files.mark_first_task_done(folder, handoff_files.file_key(worker['id']), time.time())
                except Exception as exc:
                    self.store.event('handoff_file_failed', task.get('run_id', ''), worker_id=worker['id'], message=str(exc)[:160])
                self.store.event('handoff_acked' if acked else 'handoff_ack_missing', task.get('run_id', ''),
                                 worker_id=worker['id'], message=('token confirmed; file removed' if acked else
                                 'reply has no ack token; file kept for review (deleted after 24 h)'))
                text = '\n'.join(line for line in text.splitlines() if line.strip() != token).strip()
            tool_activity=any(any(word in str(e.get('kind',e.get('type',''))).lower() for word in ('tool','execution','computer')) or e.get('tool_use_id') for e in own_turn)
            return VerifiedReply(text,tool_activity)
        if completed and not text:
            # Notion may have ended this turn with an error step and no reply (confirmed retry).
            self.confirm_turn_error(worker,task)
        self.note_pending_interaction(task, own_turn)
        # B46: liveness from the same read. A turn Notion still runs and that keeps adding steps is working, not hung.
        live = self.note_remote_progress(task, own_turn, session, transcript)
        # A timeout is not evidence that the server didn't accept the POST.
        raise PendingRemote(state='running' if live else '', reason='remote_working' if live else '')

    def recover_native(self, worker, thread_id, limit, task, exc):
        """B46: the Edge tab can fail on large transcripts; read the same chat once, read-only, via the native transport.
        Logs both failures so a stuck check is visible instead of silent. Returns None if that read fails too."""
        run_id = task.get('run_id', '')
        try:
            self.store.event('recover_read_failed', run_id, task_id=task.get('id', ''),
                             message=('%s: %s' % (type(exc).__name__, exc))[:200])
        except Exception:
            pass
        try:
            with self.lock:
                native = self.transports.get(worker.get('account_id'))
                identity = (self.store.get('accounts', worker.get('account_id'), {}) or {}).get('identity')
            if native is None:
                return None
            with request_priority('verify'):
                return NotionClient(native, identity).transcript(thread_id, limit=limit)
        except ThreadMissing:
            raise
        except Exception as exc2:
            try:
                self.store.event('recover_native_failed', run_id, task_id=task.get('id', ''),
                                 message=('%s: %s' % (type(exc2).__name__, exc2))[:200])
            except Exception:
                pass
            return None

    def note_remote_progress(self, task, own_turn, session, transcript):
        """B46: record the newest step of our own turn. True while Notion still runs the turn and a new
        step arrived within REMOTE_QUIET_SECONDS; then the coworker is working, not hung."""
        running = str((session or {}).get('status') or '') == 'running' or bool((transcript or {}).get('session_pending'))
        newest = max([float(e.get('sequence', 0) or 0) for e in own_turn], default=0.0)
        now = time.time()
        stored = self.store.get('tasks', task.get('id', ''), {}) or {}
        fields = {'remote_running': running, 'remote_steps': len(own_turn), 'remote_checked_at': now}
        if newest and (newest != float(stored.get('remote_newest_seq') or 0)
                       or len(own_turn) != int(stored.get('remote_steps') or 0)):
            fields.update(remote_newest_seq=newest, remote_progress_at=now)
        progress_at = float(fields.get('remote_progress_at') or stored.get('remote_progress_at') or 0)
        live = bool(running and progress_at and now - progress_at <= REMOTE_QUIET_SECONDS)
        fields['remote_working'] = live
        try:
            self.mark_task(task['id'], **fields)
        except Exception:
            pass
        return live

    def note_pending_interaction(self, task, own_turn):
        """Record when the turn is paused on a tool call that waits for an approval click in the chat.

        Notion ends the stream and keeps the turn open until someone approves; without this the task
        looked like an unconfirmed completion and silently blocked its run.
        """
        pending = [e for e in own_turn if tool_waiting(e)]
        stored = self.store.get('tasks', task.get('id', ''), {}) or {}
        if not pending:
            if stored.get('awaiting_approval') or stored.get('approval_seen') or stored.get('pending_tool'):
                self.mark_task(task['id'], awaiting_approval='', approval_seen=None, approval_evidence=False,
                               pending_tool='', pending_tool_state='')  # the turn moved on
            return None
        entity = max(pending, key=lambda e: e.get('sequence', 0))
        label = pending_tool_label(entity)
        # H5: the honest state (approval only with evidence) is stored and shown; the engine treats a task as
        # an approval wait only when approval_evidence is True.
        evidence, state = approval_evidence(entity), pending_tool_state(entity)
        key = str(entity.get('id') or entity.get('sequence') or label)
        seen = stored.get('approval_seen') or {}
        if seen.get('key') != key:
            self.mark_task(task['id'], approval_seen={'key': key, 'at': time.time()}, pending_tool=label,
                           pending_tool_state=state, approval_evidence=evidence)
            self.store.event('turn_tool_pending', task.get('run_id', ''), task_id=task.get('id', ''), tool=label,
                             state=state, message=PENDING_TOOL_TEXT + ' (' + label + ': ' + state +
                             '); the request is not resent.')
            if not evidence:
                return None
        elif stored.get('pending_tool_state') != state or bool(stored.get('approval_evidence')) != evidence:
            self.mark_task(task['id'], pending_tool=label, pending_tool_state=state, approval_evidence=evidence)
        # Auto-run tools are 'pending' for a moment too: without evidence only a call that stays pending gets the label.
        if not evidence and time.time() - float(seen.get('at') or 0) < APPROVAL_WAIT_SECONDS:
            return None
        if stored.get('awaiting_approval') != label:
            self.mark_task(task['id'], awaiting_approval=label, awaiting_approval_at=time.time())
            self.store.event('turn_awaiting_approval', task.get('run_id', ''), task_id=task.get('id', ''),
                             tool=label, message='The coworker chat waits for an approval of ' + label +
                             '; approve it in that Notion chat. The request is not resent.')
        return label

    def turn_failed(self, task, error):
        """Notion recorded our turn as failed before any output: record it and retry after a cooldown."""
        error = error or {}
        sub_type = str(error.get('subType') or 'error')
        message = ('Notion ended this turn with an error (' + sub_type + '): ' + str(error.get('message') or 'no reply') +
                   ' No reply was produced; a new turn follows after a cooldown.')
        saved = self.store.get('tasks', task['id'], dict(task))
        saved.update(model_verification='turn_error', reported_model_ids=[], remote_error=sub_type)
        self.store.put('tasks', saved)
        self.store.event('remote_turn_failed', task.get('run_id', ''), task_id=task['id'],
                         trace_id=error.get('traceId', ''), sub_type=sub_type, error_class='TRANSIENT', message=message)
        budget = getattr(self, 'network_budget', None)
        if isinstance(budget, NetworkBudget):
            budget.turn_failed(source='inference', sub_type=sub_type)
        from .account_health import account_of, turn_error_cooldown
        seconds = turn_error_cooldown(self.store, account_of(self.store, saved.get('worker_id') or task.get('worker_id')))
        raise RateLimited(seconds, message, confirmed=True)

    def confirm_turn_error(self, worker, task):
        """Best effort for a finished turn without a reply; unreadable records keep it pending."""
        client = self.client(worker['account_id'])
        thread_id = task.get('thread_id') or worker['thread_id']
        def records(table, ids):
            result = client.call('syncRecordValuesSpaceInitial', {'requests': [
                {'pointer': {'table': table, 'id': ident, 'spaceId': client.info['space_id']}, 'version': -1} for ident in ids]})
            return {ident: unwrap(value) for ident, value in result.get('recordMap', {}).get(table, {}).items()}
        try:
            thread = records('thread', [thread_id]).get(thread_id) or {}
            steps, _ = own_turn_steps(records, thread.get('messages', []), task.get('step_id'))
            state, error = turn_status(steps, task.get('step_id'), list(((thread.get('data') or {}).get('turn_outcomes') or {}).values()))
        except Exception:
            return
        if state == 'error':
            self.turn_failed(task, error)

    def continue_turn(self, worker, text, step_id, task=None):
        """Auto-resume: send one plain continue message into an idle harness chat and drain its stream.

        Returns False when the chat is busy again (nothing was sent). The tool environment stays enabled
        because interrupted turns are usually tool turns (NotionTool MCP). With task, the resume uses the
        task's own model; without task it uses the account model as before.
        """
        client = self.client(worker['account_id'])
        transcript = client.transcript(worker['thread_id'], limit=10)
        if session_state(transcript) == 'running':
            return False  # shared rule; a missing status stays allowed here as before (the chat is read again below)
        account = self.store.get('accounts', worker['account_id']) or {}
        model = (task or {}).get('assigned_model_id')
        if not model and (task or {}).get('model'):
            try:
                model = client.model(task['model'])
            except Exception:
                model = None
        model = model or account.get('model') or self.template['transcript'][0]['value'].get('model')
        # The resume message can change the chat's saved model (modelFromUser): make the next send in this
        # chat check and set its model again instead of trusting the in-memory config entry.
        getattr(self, 'confirmed_configs', {}).pop(worker['thread_id'], None)
        if hasattr(self.store, 'delete'):
            self.store.delete('thread_configs', worker['thread_id'])
        effort = (task or {}).get('effort')
        if not effort and task:
            effort = choose_effort(client, model, worker.get('purpose', ''), task.get('task_class', ''),
                                   task.get('risk', ''))[0]
        if worker.get('purpose') in MAIN_OWN_CONFIG:
            inherited = inherit_main_config(client, worker['thread_id'])
            if inherited is not None:
                model, effort = inherited[1], inherited[2]
        # Owner rule 2026-10-22: a stored Coworker effort (older task, stale value) stays in medium..high.
        effort = final_effort(client, model, effort, worker.get('purpose', ''))
        body = self.body(client, worker['thread_id'], step_id, text, model, computer=True, effort=effort)
        if text is None:
            # Owner 2026-10-24: the same request as Notion's own 'Continue' button (runFromStep with promptKey
            # max_iterations.continue, Notion 7.40): no new user step, so nothing is written into the chat;
            # the server goes on from the chat it already has.
            body['transcript'] = [s for s in body.get('transcript') or [] if s.get('type') != 'user']
            body.pop('submittedUserStepId', None)
            body['isPartialTranscript'] = True
            body['createThread'] = False
            body['generateTitle'] = False
        timeout = float(self.store.settings().get('task_timeout') or 1800)
        try:
            for _ in client.transport.stream('runInferenceTranscript', body, client.headers(), timeout=timeout):
                pass
        except NotionHttpError as exc:
            if exc.status == 404:
                raise ThreadMissing(worker['thread_id'], 'runInferenceTranscript') from None
            raise
        return True

    def click_continue(self, worker, step_id, task=None):
        """Auto-continue like a click on Notion's 'Continue' button: no message appears in the chat.

        Works for every chat, also in the background (not only the one open in the app window).
        """
        return self.continue_turn(worker, None, step_id, task=task)

    def thread_busy(self, client, thread_id, window=None):
        """True while Notion has not written the outcome of the latest turn in this chat."""
        try:
            latest, done, _status = self.turn_outcome(client, thread_id)
        except Exception:
            return None  # unknown: only the empty-reply check (turn_running -> engine) treats None as busy; the two pre-send checks in send (_busy_now, after the checkpoint) treat it as not busy, as before
        # A turn without an outcome for longer than the task timeout counts as abandoned.
        limit = window if window is not None else self.store.settings()['task_timeout']
        return latest > done and time.time() * 1000 - latest < limit * 1000

    def turn_running(self, worker):
        """True while Notion has not finished the latest turn in this worker chat (up to 1 h).

        Effort max/xhigh turns think far longer than task_timeout (live: 819 s and 1157 s), and
        Notion may close the stream early. Used before an 'empty reply' is resent.
        """
        if not worker.get('account_id') or not worker.get('thread_id'):
            return False
        return self.thread_busy(self.client(worker['account_id']), worker['thread_id'], window=3600)

    def stop_turn(self, thread_id, worker=None):
        """Ask Notion to stop the running turn in a worker chat (used by stall_watch).

        Returns dict(ok, reason). Setting stop_turn=False switches it off. A missing
        thread raises ThreadMissing; every other failure is reported, never raised.
        """
        settings = self.store.settings()
        if not settings.get('stop_turn', True):
            return dict(ok=False, reason='disabled')
        if worker is None:
            worker = next((w for w in self.store.all('workers') if w.get('thread_id') == thread_id), None)
        if not worker or not worker.get('account_id'):
            return dict(ok=False, reason='unknown_thread')
        client = self.client(worker['account_id'])
        # No verified stop endpoint: stopInferenceTranscript and stopInference both answer 'Cannot POST' 404
        # (live check 2026-10-18). Send only when an endpoint is configured explicitly.
        endpoint = str(settings.get('stop_turn_endpoint') or '')
        if not endpoint:
            return dict(ok=False, reason='no_verified_endpoint')
        try:
            client.call(endpoint, {'spaceId': client.info.get('space_id', ''), 'threadId': thread_id})
        except ThreadMissing:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed stop must not break the stall loop
            self.store.event('stop_turn_failed', message=type(exc).__name__, thread_id=thread_id)
            return dict(ok=False, reason='error', error=str(exc)[:300])
        self.store.event('stop_turn_sent', thread_id=thread_id)
        return dict(ok=True, reason='sent')

    def verify_response_model(self,worker,task,prefetch=None):
        # Check the committed own turn, framed by verify_started / verify_done events.
        trace=dict(task_id=task['id'],worker_id=worker.get('id',''),attempt=task.get('attempts',0))
        began=time.monotonic();outcome='error'
        self.store.event('verify_started',task.get('run_id',''),**trace)
        try:
            result=self.check_response_model(worker,task,**({'prefetch':prefetch} if prefetch else {}))
            outcome=(self.store.get('tasks',task['id']) or {}).get('model_verification') or 'verified'
            return result
        except PendingRemote:
            outcome='pending';raise
        except RateLimited:
            outcome='turn_error';raise
        except ModelVerificationError:
            outcome='unverified';raise
        finally:
            read=(self.store.get('tasks',task['id']) or {}).get('verify_messages_read',0)
            self.store.event('verify_done',task.get('run_id',''),outcome=outcome,seconds=round(time.monotonic()-began,3),
                             messages_read=read,**trace)

    @staticmethod
    def model_labels(client,ids):
        """Map model ids to 'Label (id)' from the cached catalog; unknown ids are left out."""
        found={}
        try:
            cache=getattr(getattr(client,'transport',None),'model_catalog',None) or {}
            entry=cache.get(client.info['space_id']) if isinstance(cache,dict) else None
            for item in (entry[1] if entry else []):
                ident=item.get('model');label=re.sub(r'\s+',' ',str(item.get('modelMessage') or '')).strip()
                if ident in ids and label:
                    found[ident]=label+' ('+ident+')'
        except Exception:
            pass
        return found

    def check_response_model(self,worker,task,prefetch=None):
        client=self.client(worker['account_id'])
        def records(table,ids):
            result=client.call('syncRecordValuesSpaceInitial',{'requests':[
                {'pointer':{'table':table,'id':ident,'spaceId':client.info['space_id']},'version':-1} for ident in ids]})
            return {ident:unwrap(value) for ident,value in result.get('recordMap',{}).get(table,{}).items()}
        steps=None;thread={}
        try:
            thread=(prefetch.result(timeout=30) if prefetch else None) or records('thread',[worker['thread_id']]).get(worker['thread_id'],{})
            if thread.get('space_id')!=client.info['space_id'] or thread.get('alive') is False:
                raise ModelVerificationError(task.get('assigned_model_id'),[],'missing')
            steps,read=own_turn_steps(records,thread.get('messages',[]),task.get('step_id'))
            self.mark_task(task['id'],verify_messages_read=read)
            reported=verify_execution(steps,task.get('step_id'),task.get('assigned_model_id'))
        except ModelVerificationError as exc:
            saved=self.store.get('tasks',task['id'],dict(task))
            if exc.reason=='mismatch' and exc.reported:
                # Notion ran a different model for this plan: continue and show it.
                first=not saved.get('fallback_reported')
                saved.update(model_verification='fallback',reported_model_ids=exc.reported,fallback_reported=True)
                self.store.put('tasks',saved)
                labels=self.model_labels(client,[exc.expected]+list(exc.reported))
                note=('Requested model '+labels.get(exc.expected,str(exc.expected))+' is not used by Notion for this account; '
                      'chats run on '+', '.join(labels.get(r,str(r)) for r in exc.reported)+'.')
                with self.store.lock:
                    account=self.store.get('accounts',worker.get('account_id'))
                    if account and account.get('model_fallback')!=note:
                        account['model_fallback']=note
                        self.store.put('accounts',account)
                if first:
                    # Once per task, not once per verification.
                    self.store.event('model_fallback',task.get('run_id',''),task_id=task['id'],
                                     expected_model_id=exc.expected,reported_model_ids=exc.reported,message=note)
                return exc.reported
            if exc.reason=='missing' and steps is not None:
                state,error=turn_status(steps,task.get('step_id'),list(((thread.get('data') or {}).get('turn_outcomes') or {}).values()))
                if state=='error':
                    self.turn_failed(task,error)
                if state in ('running','missing'):
                    # Notion has not written the outcome of this turn yet: check again later, never resend.
                    raise PendingRemote() from None
            saved.update(model_verification='unverified',reported_model_ids=exc.reported)
            self.store.put('tasks',saved)
            raise
        except (OSError,TimeoutError,ValueError):
            raise PendingRemote() from None
        saved=self.store.get('tasks',task['id'],dict(task))
        saved.update(model_verification='verified',reported_model_ids=reported)
        self.store.put('tasks',saved)
        return reported

    def disconnect_bridge(self, account_id):
        """Remove this app's stale default MCP registrations, preserving other servers."""
        account = self.store.get('accounts', account_id) or {}
        if not account:
            raise ValueError('Account not found')
        client = self.client(account_id)
        client.identity()
        known_id = account.get('bridge_integration')
        known_url = account.get('bridge_url')
        removed = []
        for wrapped in client.info.get('records', {}).get('space_view', {}).values():
            view = unwrap(wrapped)
            if view.get('space_id') != client.info['space_id']:
                continue
            modules = list((view.get('settings') or {}).get('agent_chat_modules') or [])
            ids = [m['pointer']['id'] for m in modules
                   if (m.get('pointer') or {}).get('table') == 'workflow_module']
            records = {}
            if ids:
                synced = client.call('syncRecordValuesSpaceInitial', {'requests': [
                    {'pointer': {'table': 'workflow_module', 'id': ident, 'spaceId': client.info['space_id']},
                     'version': -1} for ident in ids]})
                records = {ident: unwrap(value) for ident, value in
                           synced.get('recordMap', {}).get('workflow_module', {}).items()}
            stale = set()
            for ident in ids:
                record = records.get(ident) or {}
                data = record.get('data') or {}
                url = data.get('serverUrl') or record.get('server_url')
                name = data.get('name') or record.get('name')
                if ident == known_id or (known_url and url == known_url) or name == 'NotionTool':
                    stale.add(ident)
            if stale:
                kept = [m for m in modules if (m.get('pointer') or {}).get('id') not in stale]
                self.transaction(client, 'mcpServerSettings.removeNotionToolDefaults', [{
                    'pointer': {'table': 'space_view', 'id': view['id'], 'spaceId': client.info['space_id']},
                    'path': ['settings'], 'command': 'update', 'args': {'agent_chat_modules': kept}}])
                removed.extend(sorted(stale))
        account.update(bridge_connected=False, bridge_integration='', bridge_url='', bridge_error='')
        self.store.put('accounts', account)
        return {'account_id': account_id, 'removed_defaults': removed}

    BACKUP_MCP_NAMES = ('MCP Test',)

    def ensure_backup_autorun(self, client, records, skip=''):
        """Backup MCP servers (used when a NotionTool request times out) run read and write tools automatically.

        A backup left on 'always ask' pauses the Coworker turn on an approval click that never comes: the stream
        ends, the turn stays open and the task ends up unconfirmed (root cause found 2026-10-19).
        Idempotent: only modules whose permissions differ are changed. Returns the changed module ids."""
        names = {n.lower() for n in ((self.store.get('settings', 'app') or {}).get('backup_mcp_names') or self.BACKUP_MCP_NAMES)}
        changed = []
        for ident, value in (records or {}).items():
            data = value.get('data') or {}
            label = str(data.get('name') or value.get('name') or '').strip().lower()
            if ident == skip or not value.get('alive', True) or label not in names:
                continue
            if data.get('runReadToolsAutomatically') is True and data.get('runWriteToolsAutomatically') is True:
                continue
            self.transaction(client, 'mcpServerSettings.updateToolPermissions', [
                {'pointer': {'table': 'workflow_module', 'id': ident, 'spaceId': client.info['space_id']},
                 'path': ['data', key], 'command': 'set', 'args': True}
                for key in ('runReadToolsAutomatically', 'runWriteToolsAutomatically')])
            changed.append(ident)
        if changed:
            self.store.event('backup_mcp_autorun_set', modules=len(changed),
                             message='Backup MCP tools now run automatically (no approval pause)')
        return changed

    def connect_bridge(self, account_id, url, token, name='NotionTool', read_auto=True, write_auto=True, registry_id=''):
        if not url.startswith('https://'):
            raise ValueError('Notion requires a reachable public HTTPS MCP URL')
        client = self.client(account_id)
        client.identity()
        from .mcp_policy import ensure_mcp_allowed
        ensure_mcp_allowed(self,client,url)
        view=next((unwrap(v) for v in client.info.get('records',{}).get('space_view',{}).values()
                   if unwrap(v).get('space_id')==client.info['space_id']),None)
        if not view or not view.get('id'):raise ValueError('Notion workspace view is unavailable for MCP connection')
        modules=list((view.get('settings') or {}).get('agent_chat_modules') or [])
        ids=[m['pointer']['id'] for m in modules if (m.get('pointer') or {}).get('table')=='workflow_module']
        records={}
        if ids:
            synced=client.call('syncRecordValuesSpaceInitial',{'requests':[{'pointer':{'table':'workflow_module','id':ident,'spaceId':client.info['space_id']},'version':-1} for ident in ids]})
            records={ident:unwrap(value) for ident,value in synced.get('recordMap',{}).get('workflow_module',{}).items()}
        integration=next((ident for ident,value in records.items() if value.get('alive',True) and
                          ((value.get('data') or {}).get('serverUrl') or value.get('server_url'))==url),None)
        auth = [{'name':'Authorization','value':'Bearer '+token}] if token else []
        validation = client.call('validateMcpConnection',dict(serverUrl=url,spaceId=client.info['space_id'],
            authHeaders=auth,initiationContext='connect'),timeout=90)
        if not validation.get('success'):
            raise ValueError('Notion cannot reach this MCP server')
        if not integration:
            integration=self.new_id(client.info['space_id'],'00b6')
            result = client.call('postWorkflowsMcpServerConnect',dict(integrationId=integration,spaceId=client.info['space_id'],
                authHeaders=auth,moduleDefinition={'name':name,'serverUrl':url,
                'runReadToolsAutomatically':read_auto,'runWriteToolsAutomatically':write_auto},initiationContext='connect'),timeout=90)
            if not result.get('success'):
                raise ValueError('Notion refused the MCP connection')
            modules.append({'pointer':{'table':'workflow_module','id':integration,'spaceId':client.info['space_id']},'defaultEnabled':True})
        # Idempotent: an existing, enabled module with the right permissions is left untouched, so a
        # reconnect never rewrites the account's chat modules while Coworkers are working.
        changed = integration not in records
        for module in modules:
            if (module.get('pointer') or {}).get('id')==integration and module.get('defaultEnabled') is not True:
                module['defaultEnabled']=True;changed=True
        if changed:
            self.transaction(client,'agentPersistenceHelpers.addAgentChatModule',[
                {'pointer':{'table':'space_view','id':view['id'],'spaceId':client.info['space_id']},'path':['settings'],
                 'command':'update','args':{'agent_chat_modules':modules}}])
        current=(records.get(integration) or {}).get('data') or {}
        if integration not in records or current.get('runReadToolsAutomatically')!=read_auto or current.get('runWriteToolsAutomatically')!=write_auto:
            self.transaction(client,'mcpServerSettings.updateToolPermissions',[
                {'pointer':{'table':'workflow_module','id':integration,'spaceId':client.info['space_id']},
                 'path':['data',key],'command':'set','args':value} for key,value in
                 (('runReadToolsAutomatically',read_auto),('runWriteToolsAutomatically',write_auto))])
        self.ensure_backup_autorun(client, records, skip=integration)
        account = self.store.get('accounts',account_id)
        if registry_id:
            connections=account.setdefault('mcp_connections',{})
            connections[registry_id]={'connected':True,'integration_id':integration}
        else:
            account.update(bridge_connected=True,bridge_url=url,bridge_integration=integration,bridge_error='',bridge_blocked=False,bridge_next_at=0)
        self.store.put('accounts',account)
        return {'connected':True}
