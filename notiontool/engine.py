"""Persistent, fair coordinator. Workers never join themselves or own scheduling."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import re
import secrets
import threading
import time

from .protocol import ContractError, json_object, validate_plan, prompt_for
from .store import uid
from . import plain_protocol
from . import prompt_once
from . import file_registry
from . import coworker_age_guard
from .allocation import allocation, parallel_width, validate_allocation
from .team_size import coworker_capacity
from .model_routing import DEFAULT_MODEL, MODELS, validate_models, with_reason
from .model_verification import ModelVerificationError
from .network_budget import CircuitOpen, NetworkBudget, RemotePaused, backoff, run_with_priority
from . import proof as task_proof
from . import patch_mode as patch_mod
import subprocess
from .timeouts import task_limit

# Late done detection (done_dig / crit_done): Notion often closes or breaks the reply stream minutes before
# the coworker turn really ends. With the old backoff (5, 10, 20, 40, 60, 60 s) the real end was seen
# 60-80 s late. After this attempt's stream has closed or broken, a task is now re-checked at least every
# DONE_POLL_SECONDS. The old backoff stays for approval waits, restart recovery (no stream in this attempt),
# cancelled or over-limit attempts, overdue tasks (300 s) and tasks beyond the slot cap.
# Rate limit: at most DONE_POLL_MAX_TASKS x 60/DONE_POLL_SECONDS = 24 checks per minute (1-3 transcript
# reads each, so <= 72 requests/min) inside network_budget.HARD_LIMIT_PER_MINUTE = 180, which still caps all.
DONE_POLL_SECONDS = 15
DONE_POLL_MAX_TASKS = 6
DONE_POLL_WINDOW_SECONDS = 1200  # turns of 14-19 min exist (see _empty_remote_reply); later the old backoff applies


def _setting_number(settings, key, default, low, high):
    try:
        value = float((settings or {}).get(key, default))
    except (TypeError, ValueError, AttributeError):
        return default
    return max(low, min(high, value))


def reconcile_backoff(checks):
    """The unchanged backoff: 5, 10, 20, 40, 60, 60 ... seconds for check 1, 2, 3 ..."""
    return min(60, 5 * 2 ** min(max(int(checks), 1) - 1, 4))


def reconcile_delay(checks, now, done_poll_since=0, approval=False, slot_free=True, settings=None):
    """Seconds until the next reconcile check of an unconfirmed task (never longer than the old backoff)."""
    base = reconcile_backoff(checks)
    window = _setting_number(settings, 'done_poll_window_seconds', DONE_POLL_WINDOW_SECONDS, 0, 7200)
    if approval or not done_poll_since or not slot_free or now - float(done_poll_since) > window:
        return base
    return min(base, _setting_number(settings, 'done_poll_seconds', DONE_POLL_SECONDS, 5, 60))

# Patch mode (patch_mode.py): on by default for git projects with a main branch; the setting turns it off.
PATCH_MODE_DEFAULT = True
# Merge results that are worth one more try later; anything else stays 'failed' for the operator.
# 'main_red': the same tests also fail on clean main, so the task is not to blame. It waits until main
# moves (a fix landed) and is then merged again; these waits never use up PATCH_RETRY_LIMIT.
PATCH_RETRY = ('main_moved', 'main_busy', 'main_red')
PATCH_RETRY_LIMIT = 3
# A transport error before anything was sent moves the task to another coworker at most this often.
PRESEND_RETRY_LIMIT = 2
# Failed merges that /api/task/remerge may put back to 'waiting' (only with an existing worktree).
REMERGE_STATUSES = ('red', 'main_red')
PATCH_NOTE = ('\n\nWork only inside this folder: %s\nIt is your own copy of the project (a git worktree on branch %s). '
              'Read and edit files only there, never in the main project folder. Do not commit, merge or push: '
              'NotionTool commits, tests and merges your change when you are done.')
REVIEW_TEST_NOTE = ('\nTo run tests, use exactly this form (nothing else is allowed for this role): '
                    'python -m unittest tests.test_name [tests.test_other ...]')


def review_note(reviewed):
    """Tell a reviewer where the reviewed change lives right now (own copy or already merged)."""
    if not reviewed:
        return REVIEW_TEST_NOTE
    result = reviewed.get('patch_result') or {}
    folder = reviewed.get('patch_worktree') or ''
    if reviewed.get('patch_state') == 'merged':
        commit = str(result.get('main_after') or '')[:12]
        where = ('\n\nThe change you review is already merged into the main project folder'
                 + (' (commit %s)' % commit if commit else '')
                 + '. Its own copy was removed after the merge, so check the files in the main project folder.')
    elif folder and Path(folder).is_dir():
        where = '\n\nThe change you review is in its own copy: %s. Read files there, not in the main project folder.' % folder
    else:
        where = ''
    return where + REVIEW_TEST_NOTE


# Plain labels for the Live view, per task patch_state.
PATCH_LABELS = dict(working='Working in own copy', waiting='Waiting to merge', merged='Merged',
                    conflict='Conflict - being resolved', failed='Not merged')

# add_task: marks 'no explicit model' (Phase 4 change 1); resolves to DEFAULT_MODEL as before.
_UNSET_MODEL = object()

ACTIVE = {'approach', 'planning', 'executing', 'reviewing', 'fixing', 'integrating'}
TERMINAL = {'done', 'failed', 'blocked', 'cancelled'}

# Bridge tools that only read; any other tool call in an attempt counts as a possible write (rule I5).
READ_ONLY_TOOLS = ('read_file', 'list_files', 'context', 'screen_windows', 'screen_capture', 'nt_list_dir', 'nt_search_code')


def harness_enabled(chat, settings):
    override = str((chat or {}).get('harness', '')).lower()
    return override == 'on' if override in ('on', 'off') else bool(settings.get('harness_enabled', True))


# Fix V2: like bridge.LIVE_TASK_STATES. An uncertain task may still be working in Notion
# (the stream ended, the turn goes on), so its lease stays valid for writes.
LIVE_TASK_STATES = ('running', 'uncertain')

# Streaming telemetry (Engine._partial): at most one task store write per task in this window.
TELEMETRY_SECONDS = 5

# notion.turn_state states that prove 'own turn finished without reply text' from ONE transcript read.
EMPTY_PROOF_STATES = ('done_empty', 'done_error')


# B46: a coworker whose turn Notion still runs and that keeps adding steps is working, not hung.
REMOTE_LIVE_FIELDS = ('remote_running', 'remote_steps', 'remote_checked_at', 'remote_newest_seq',
                      'remote_progress_at', 'remote_working', 'remote_stall_noted')
REMOTE_WORKING_POLL_SECONDS = 15
REMOTE_QUIET_MINUTES = 10  # mirrors notion.REMOTE_QUIET_SECONDS


def working_text(stored, now):
    """B46: honest status line for a coworker that is still working after the live stream ended."""
    stored = stored or {}
    ago = max(0, int(now - float(stored.get('remote_progress_at') or now)))
    return ('Coworker is still working in Notion (%d steps, last step %ds ago); watching, nothing is resent'
            % (int(stored.get('remote_steps') or 0), ago))


class PendingRemote(Exception):
    """Upstream acceptance/completion is unknown. Never automatically resend.

    state/reason come from notion.turn_state of the same transcript read. Only EMPTY_PROOF_STATES
    (own turn provably finished, no reply text) may lead to a retry; '' means no proof either way."""
    def __init__(self, *args, state='', reason=''):
        super().__init__(*args)
        self.state, self.reason = state, reason


class ResetNotAccepted(Exception):
    """Notion verifiably did not commit a context-replacement turn; one normal resend is safe."""


class ContextCheckpointPending(Exception):
    """Only a checkpoint request may be pending; the actual assignment was not sent."""


class RateLimited(Exception):
    """confirmed: Notion recorded the turn as failed without a reply; free: nothing was submitted."""
    # scope: 'account' cools the whole account down (a real 429); 'thread' means only this chat was busy
    # (e.g. the operator's own turn), so the account and its other chats keep working (Phase 4 fix).
    def __init__(self, seconds=30, message='', confirmed=False, free=False, scope='account'):
        super().__init__(message or 'Notion rate limit')
        self.seconds = seconds
        self.message = message
        self.confirmed = confirmed
        self.free = free
        self.scope = scope


# Free waits for a busy chat use no attempt; this bound keeps a stuck remote session from parking a task forever.
BUSY_WAIT_LIMIT = 120
# How long a busy Main stays handed over to free coworkers before the harness tries the Main again.
MAIN_BUSY_FALLBACK_S = 120


class TransientInference(Exception):
    pass


CANCELLED_BEFORE_SUBMISSION = 'Assignment cancelled before submission'
HEAL_BASE, HEAL_CAP = 60, 600  # seconds: 60, 120, 240, 480, 600, 600, ...
# Owner rule: at the hard token limit (burst level 'pause' or the 6 h budget used up) every RUNNING
# coworker turn stops at once, not only new ones. The stop is local (Notion has no verified stop
# endpoint): the stream is closed, a sent turn is reconciled (I5, never resent), an unsent one waits.
LIMIT_STOP_EVENT = 'limit_stop'
LIMIT_RESUME_EVENT = 'limit_resume'
LIMIT_PAUSED_BY = 'token_limit'
LIMIT_CHECK_SECONDS = 2.0   # cheap ledger check at most every 2 s (engine clock)
LIMIT_CLEAR_SECONDS = 5.0   # hysteresis: two clear checks at least 5 s apart before the latch clears


def cancelled_before_submission(exc):
    # notion.py:826 and network_budget.py:182 raise a plain ValueError with exactly this text.
    return type(exc) is ValueError and str(exc) == CANCELLED_BEFORE_SUBMISSION


def thread_missing_type():
    """notion.ThreadMissing, imported lazily: notion.py imports this module, so a top-level import would be circular."""
    from .notion import ThreadMissing
    return ThreadMissing


class Engine:
    def __init__(self, store, adapter):
        self.store, self.adapter = store, adapter
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=40, thread_name_prefix='notion-io')
        self.inflight = {}
        self.stop_event = threading.Event()
        self.round_robin = 0
        self.thread = None
        self.clock = time.time  # injectable for the worker heal backoff
        self.team_grower = None  # team_grow.TeamGrower, set by the Application (automatic team growth)
        # Token hard-limit latch (memory only; rebuilt from the ledger by the next check after a restart).
        self.limit_latch = None
        self._limit_checked = None
        self._limit_clear_since = 0.0
        self._restore()

    def _restore(self):
        # Persisted assignment IDs fence old reports even after an app restart.
        for task in self.store.all('tasks'):
            if task['state'] in ('running', 'cancelling'):
                if task.get('submission_phase') == 'preparing':
                    cancelled = self.store.get('runs', task.get('run_id'), {}).get('state') == 'cancelled'
                    task.update(state='cancelled' if cancelled else 'retry', next_at=0, attempts=max(0, task.get('attempts',1)-1))
                    self.store.put('tasks', task)
                    continue
                task.update(state='uncertain', reconcile_at=time.time(), error='Recovering the previous API request')
                self.store.put('tasks', task)
        assigned = {t.get('worker_id'):t for t in self.store.all('tasks') if t['state'] == 'uncertain' or t.get('remote_unknown')}
        for worker in self.store.all('workers'):
            if worker['id'] in assigned:
                worker['state'] = 'recovering'
                worker['lease'] = assigned[worker['id']].get('lease','')
            elif worker.get('state') in ('busy', 'recovering'):
                worker['state'] = 'ready'
                worker['lease'] = ''
            self.store.put('workers', worker)

    def start(self):
        self.wake_event = getattr(self, 'wake_event', None) or threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name='coordinator')
        self.thread.start()

    def _wake(self):
        """Run the next tick now instead of after the 0.25 s poll."""
        event = getattr(self, 'wake_event', None)
        if event is not None:
            event.set()

    def _watch(self, future, info):
        """A finished remote call wakes the coordinator; done_at measures the wait until _complete."""
        def done(_future, info=info):
            info['done_at'] = time.time()
            self._wake()
        future.add_done_callback(done)

    def close(self):
        self.stop_event.set()
        self._wake()
        with self.lock:
            for future, info in list(self.inflight.items()):
                info['cancel'].set()
        if self.thread:
            self.thread.join(2)
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self):
        wake = getattr(self, 'wake_event', None) or threading.Event()
        while not self.stop_event.is_set():
            if wake.wait(0.25):
                wake.clear()  # a finished remote call: tick right away
            if self.stop_event.is_set():
                break
            try:
                self.tick()
            except Exception as exc:
                self.store.event('coordinator_error', message=type(exc).__name__)
            grower = getattr(self, 'team_grower', None)
            if grower is not None:  # startup check, then every 5 minutes (team_grow.CHECK_S)
                try:
                    grower.maybe_check()
                except Exception as exc:
                    # Name and text: 'ImportError' alone hid the missing chat_pool.name_coworkers for hours.
                    self.store.event('team_grow_check_failed', message=(type(exc).__name__ + ': ' + str(exc))[:300])

    def tasks(self, run_id):
        return [t for t in self.store.all('tasks') if t['run_id'] == run_id]

    def add_task(self, run, role, prompt, title='', deps=(), files=(), acceptance='', ident=None, specialty='', model=_UNSET_MODEL, model_reason='', use_main=False, task_class='', risk=''):
        explicit_model = model is not _UNSET_MODEL
        if not explicit_model:
            model = DEFAULT_MODEL
        task = dict(id=ident or uid('t'), run_id=run['id'], role=role, title=title or role.title(),
                    prompt=prompt, deps=list(deps), files=list(files), acceptance=acceptance,
                    state='queued', attempts=0, created=time.time(), next_at=0, result='', error='',
                    worker_id='', lease='', step_id='', started=0, finished=0, specialty=specialty,
                    model=model, model_reason=model_reason, use_main=use_main,
                    task_class=str(task_class or '').strip().lower())
        if str(risk or '').strip():
            task['risk'] = str(risk).strip().lower()
        if run.get('model_routing') and role == 'executor' and not explicit_model:
            self._route_model(task)
        elif role == 'executor' and not explicit_model and not str(model_reason or '').strip():
            self._rule_reason(task)
        if task['files']:
            # The planned list stays fixed; claim_file appends later claims to task['files'] only.
            task['assigned_files'] = list(task['files'])
        with_reason(task)  # every role stores a non-empty model_reason; task['model'] is unchanged
        self.store.event('task_queued', run['id'], commit=False, task_id=task['id'], role=role, message=task['title'])
        self.store.put('tasks', task)  # also commits task_queued
        return task

    @staticmethod
    def _route_model(task):
        """Phase 4 change 1: an executor of a routed run without an explicit model gets the
        task_router.decide(task_class, risk) choice. On an error or an unusable result the task
        keeps today's default model and nothing new is recorded."""
        try:
            from . import task_router
            decision = task_router.decide(task.get('task_class', ''), task.get('risk'))
            chosen = decision.get('model') if isinstance(decision, dict) and not decision.get('exhausted') else None
            reason = str(decision.get('model_reason') or '').strip() if chosen else ''
            if chosen not in MODELS or not reason:
                return task
        except Exception:  # the router must never break task creation
            return task
        task['model'] = chosen
        task['model_reason'] = 'task_router: ' + reason
        return task

    @staticmethod
    def _rule_reason(task):
        """Executor of an unrouted run without an explicit model: the model stays the default
        (Opus 5.5), but model_reason names the real task_router rule instead of 'fallback'."""
        try:
            from . import task_router
            rule = task_router.rule_for(task.get('task_class', ''), task.get('risk'))
            reason = str(rule.get('model_reason') or '').strip()
            if not reason:
                return task
            if rule.get('model') == task.get('model'):
                task['model_reason'] = 'model rule: ' + reason
            else:
                kept = MODELS.get(task.get('model'), task.get('model'))
                task['model_reason'] = (f'model rule: routing is off for this run, {kept} kept; '
                                        f'rule would pick {MODELS.get(rule.get("model"), rule.get("model"))} ({reason})')
        except Exception:  # never break task creation
            return task
        return task

    def submit(self, chat_id, goal, request_id=''):
        with self.lock:
            chat = self.store.get('chats', chat_id)
            if not chat or not goal.strip():
                raise ValueError('Choose a chat and enter a message')
            request_id = str(request_id or '')[:100]
            if request_id:
                # Idempotent send: a retried or reconnected request returns the accepted run.
                known = next((r for r in self.store.all('runs') if r.get('request_id') == request_id), None)
                if known:
                    if known['chat_id'] != chat_id:
                        raise ValueError('request_id already belongs to another chat')
                    return dict(known, duplicate=True)
            if chat.get('state') == 'released':
                raise ValueError('This chat was released; its Notion thread belongs to the user')
            if chat.get('operator'):
                raise ValueError('The operator Main delegates with /api/operator/delegate')
            main = self.store.get('workers', chat['main_worker'])
            if not main or not main.get('enabled'):
                raise ValueError('Choose an available Notion main chat in Accounts')
            if getattr(self.adapter, 'single_account_harness', False):
                from .harness_account import selected_account
                if not selected_account(self.store) or main['account_id'] != selected_account(self.store):
                    raise ValueError('Select this chat\'s account as the single harness account first')
            run_id = uid('r')
            # Context lives on the operator's PC (handoff files, project memory); never inject old reports.
            context = ''
            # A chat queues its next run, while independent main chats share the pool.
            # Up to max_runs_per_chat runs of one chat work at once (file claims keep them apart); the rest queue in order.
            same = [r for r in self.store.all('runs') if r['chat_id'] == chat_id]
            existing = (any(r['state'] == 'queued' for r in same)
                        or sum(r['state'] in ACTIVE | {'paused'} for r in same) >= self._runs_per_chat())
            ready_accounts={a['id'] for a in self.store.all('accounts') if a.get('state')=='ready'}
            workers = [w for w in self.store.all('workers') if w.get('purpose') == 'worker' and w.get('enabled') and w['account_id'] in ready_accounts
                       and (not getattr(self.adapter, 'single_account_harness', False) or w['account_id'] == main['account_id'])]
            capacity = coworker_capacity(self.store.settings(), workers)  # at most N per account, max_workers overall
            run = dict(id=run_id, chat_id=chat_id, main_worker=main['id'], goal=goal.strip(),
                       state='queued' if existing else 'approach', stage='', context=context,
                       project_path=chat.get('project_path', ''), folder_id=chat.get('folder_id', ''),
                       created=time.time(), updated=time.time(), capacity=capacity, account_id=main['account_id'],
                       fixes=0, summary='', error='', request_id=request_id)
            run['allocation'] = allocation(goal, capacity)
            run['model_routing'] = bool(getattr(self.adapter, 'task_model_routing', False))
            run['harness'] = harness_enabled(chat, self.store.settings())
            if not run['harness']:
                run['allocation'] = dict(mode='direct', budget=1, advisers=0, reason='Harness off: Architect answers directly')
            run['worker_budget'] = run['allocation']['budget']
            self.store.put('runs', run)
            self.store.message(chat_id, 'user', goal.strip(), run_id)
            self.store.event('run_created', run_id, message='Request queued' if existing else 'Approach planning started')
            if not existing:
                self._approach(run)
            return run

    def _attempt_side_effects(self, task):
        """True when the current attempt may have changed something: claimed files or a recorded tool call."""
        if task.get('files') or task.get('absolute_resources') or task.get('tool_activity'):
            return True
        log = task.get('attempt_log') or []
        begun = float((log[-1].get('started') if log else 0) or task.get('started') or 0)
        db = getattr(self.store, 'db', None)
        if db is None:
            return True  # no event log to prove the attempt was clean
        with self.store.lock:
            row = db.execute("SELECT 1 FROM events WHERE json_extract(doc,'$.task_id')=? "
                             "AND kind IN ('tool_started','tool_finished','file_claimed') AND ts>=? LIMIT 1",
                             (task['id'], begun)).fetchone()
        return bool(row)

    def _hold_for_reconcile(self, task, info):
        # Never hand an unconfirmed attempt with side effects to another chat; it waits for reconciliation.
        if not task.get('side_effect_hold'):
            task['side_effect_hold'] = True
            self.store.event('retry_refused_side_effects', task['run_id'], task_id=task['id'], worker_id=info['worker_id'],
                             message='The unconfirmed attempt used tools or claimed files; it is reconciled, not resent')

    def _attempt_writes(self, task):
        """True when the current attempt may have written: PC paths, newly claimed files or a non-read tool call."""
        if task.get('absolute_resources'):
            return True
        assigned = task.get('assigned_files')
        if assigned is not None and set(task.get('files') or []) - set(assigned):
            return True
        log = task.get('attempt_log') or []
        begun = float((log[-1].get('started') if log else 0) or task.get('started') or 0)
        db = getattr(self.store, 'db', None)
        if db is None:
            return True  # no event log to prove the attempt only read
        with self.store.lock:
            rows = db.execute("SELECT json_extract(doc,'$.tool') FROM events WHERE json_extract(doc,'$.task_id')=? "
                              "AND kind IN ('tool_started','file_claimed') AND ts>=?", (task['id'], begun)).fetchall()
        return any(name is None or str(name) not in READ_ONLY_TOOLS for (name,) in rows)

    def _empty_remote_reply(self, task, info, run, exc=None):
        """Retry only when ONE transcript read proved the own turn finished without reply text.

        False-empty fix (t3ec11e437, 2026-10-20 22:41:57): the old rule retried after 3 checks and 60 s
        when a second signal (the thread record) said 'not busy', although recover had just said the turn
        was not finished. Now only exc.state in EMPTY_PROOF_STATES from notion.turn_state may retry; elapsed
        time and check counts never decide (they only set the next check time)."""
        if getattr(exc, 'state', '') not in EMPTY_PROOF_STATES:
            return False
        if str(task.get('result') or '').strip() or str(task.get('partial') or '').strip():
            return False
        if self._attempt_side_effects(task):
            self._hold_for_reconcile(task, info)
            return False
        # Live bug t41585b6a7 (effort max): Notion closed the stream after a few minutes while the
        # model kept thinking for 14-19 min. The 'empty' turn was resent 6 times to other coworkers.
        # A turn Notion has not finished yet is never empty: keep reconciling instead of resending.
        running = getattr(self.adapter, 'turn_running', None)
        worker = self.store.get('workers', info['worker_id'])
        if running and worker:
            # Veto only: True (busy) or None (unknown) keeps reconciling; it can never turn 'running' into 'empty'.
            try:
                if running(worker) is not False:
                    return False
            except Exception:
                return False  # unknown is not empty; never resend on a failed check
        self._release(info['worker_id'], info['lease'])
        task.pop('remote_unknown', None)
        task.update(state='cancelled' if run['state'] == 'cancelled' else 'retry', next_at=time.time() + 5,
                    reconcile_checks=0, remote_done_at=0, partial='',
                    error='Notion finished with an empty reply; retrying on a free coworker')
        self.store.event('empty_reply_retry', task['run_id'], task_id=task['id'], worker_id=info['worker_id'],
                         message='Notion finished with an empty reply; the task is retried')
        return True

    def _done_poll_since(self, task, info, now, settings):
        """When this attempt's reply stream closed or broke; 0 keeps the old backoff.

        Case (a) stream_end sets remote_done_at; case (b) a broken stream leaves first_output and
        remote_done at 0. Both reach this point from the attempt itself (recovery=False), so neither
        timestamp is needed and remote_done_at is never written here (the empty-reply retry stays as before).
        """
        since = float(task.get('done_poll_since') or 0)
        if since and since >= float(task.get('sent_at') or task.get('started') or 0):
            return since  # still the same attempt
        cancel = info.get('cancel')
        if info.get('recovery') or (cancel is not None and cancel.is_set()):
            return 0  # restart recovery or a cancel: no stream close in this attempt
        if now - float(task.get('started') or now) > task_limit(settings, task):
            return 0  # stopped at the task time limit, not by a stream close
        return now

    def _done_poll_slot_free(self, task_id, now, settings):
        """Cap the fast done poll to DONE_POLL_MAX_TASKS tasks so it stays far below 180 requests/min."""
        limit = int(_setting_number(settings, 'done_poll_max_tasks', DONE_POLL_MAX_TASKS, 0, DONE_POLL_MAX_TASKS))
        window = _setting_number(settings, 'done_poll_window_seconds', DONE_POLL_WINDOW_SECONDS, 0, 7200)
        busy = 0
        for other in self.store.all('tasks'):
            if (other.get('id') != task_id and other.get('state') == 'uncertain' and other.get('done_poll_fast')
                    and now - float(other.get('done_poll_since') or 0) <= window):
                busy += 1
        return busy < limit

    def _main_compressing(self, run):
        # Phase 4 Fix P: a Main with a requested compress waits for its rotation; its tasks go to coworkers.
        return bool(self.store.get('workers', run.get('main_worker'), {}).get('compress_requested'))

    def _runs_per_chat(self):
        try:
            return max(1, min(int(self.store.settings().get('max_runs_per_chat', 3)), 6))
        except (TypeError, ValueError):
            return 1

    def _run_slot_free(self, run, runs):
        same = [r for r in runs if r['chat_id'] == run['chat_id'] and r['id'] != run['id']]
        if any(r['state'] == 'queued' and r['created'] < run['created'] for r in same):
            return False  # queued runs of one chat still start in their order
        return sum(r['state'] in ACTIVE | {'paused'} for r in same) < self._runs_per_chat()

    def _approach(self, run):
        # Allocate only useful approach roles. Reserve the main chat for the final plan.
        policy = run.get('allocation', allocation(run['goal'], run['capacity']))
        if policy['mode'] == 'direct':
            run['state'] = 'integrating'
            self.store.put('runs', run)
            if policy.get('reason') == 'Conversational message' or not run.get('harness', True):
                self.add_task(run, 'integrator', 'Respond naturally to the conversational message.')
            else:
                # Phase 4 DIRECT: exactly one turn on the Main, no planner or separate integration stage.
                self.add_task(run, 'integrator', 'Answer this request directly and completely yourself in one turn. '
                              'No coworker is assigned. Follow any requested output format exactly; '
                              'otherwise briefly name what is an assumption.')
            return
        if run.get('model_routing'):
            run['state'] = 'planning'
            self.store.put('runs', run)
            hint = 'Choose whether to work alone or delegate, and choose each task model explicitly.'
            if policy.get('planner_mode') == 'deep':
                hint += ' DEEP mode: analyse or review first; every change task depends on that analysis.'
            self.add_task(run, 'planner', hint)
            return
        roles = ['architect', 'researcher', 'quality_planner'][:policy['advisers']]
        if not roles:
            roles = ['planner']
            run['state'] = 'planning'
            self.store.put('runs', run)
        for role in roles:
            self.add_task(run, role, 'Propose a specific execution method, deliverables, dependencies, and tests for the user request.',
                          acceptance='Concrete approach with verifiable checks; no execution yet')

    def cancel(self, run_id):
        with self.lock:
            run = self.store.get('runs', run_id)
            if not run:
                raise ValueError('Run not found')
            run.update(state='cancelled', updated=time.time())
            self.store.put('runs', run)
            for task in self.tasks(run_id):
                if task['state'] in ('queued', 'retry'):
                    task['state'] = 'cancelled'
                    self.store.put('tasks', task)
                elif task['state'] == 'running':
                    task['state'] = 'cancelling'
                    self.store.put('tasks', task)
                elif task['state'] in ('blocked', 'uncertain') and self._worker_ai_disabled(task):
                    # Notion AI is off on the worker account, so Notion can never confirm this turn.
                    # Nothing is resent (I5); the task just ends with its cancelled run.
                    if task.get('worker_id'):
                        self._release(task['worker_id'], task.get('lease', ''))
                    for key in ('remote_unknown', 'side_effect_hold', 'reconcile_at'):
                        task.pop(key, None)
                    task.update(state='cancelled', error='Cancelled with its run: the worker account has Notion AI '
                                'disabled, so Notion can never confirm this turn. Nothing was resent.')
                    self.store.put('tasks', task)
            for info in self.inflight.values():
                if info['run_id'] == run_id:
                    info['cancel'].set()
            self.store.event('run_cancelled', run_id, message='Local work stopped; upstream chats are reconciled before reuse')

    def _worker_ai_disabled(self, task):
        worker_id = task.get('worker_id', '') or ''
        worker = self.store.get('workers', worker_id, {}) if worker_id else {}
        account_id = (worker or {}).get('account_id') or worker_id.split(':')[0]
        if not account_id:
            return False
        account = self.store.get('accounts', account_id, {}) or {}
        return account.get('state') == 'ai_disabled' or (account.get('ai_access') or {}).get('state') == 'disabled'

    def pause(self, run_id, paused=True):
        with self.lock:
            run = self.store.get('runs', run_id)
            if not run:
                raise ValueError('Run not found')
            if paused and run['state'] in ACTIVE:
                run['resume_state'] = run['state']
                run['state'] = 'paused'
            elif not paused and run['state'] == 'paused':
                run['state'] = run.pop('resume_state', 'executing')
            self.store.put('runs', run)

    def _budget(self):
        # The process-wide request coordinator, when the adapter has one (Phase 3).
        budget = getattr(self.adapter, 'network_budget', None)
        return budget if isinstance(budget, NetworkBudget) else None

    def _parallel_cap(self, configured):
        budget = self._budget()
        return budget.parallel_limit(configured) if budget is not None else configured

    def _machine_check(self, run, task):
        """Local, fast check of one done task (no Notion, no network, no test runs). Returns '' or a short reason.

        Every declared file must exist inside the project root. The check runs as a 'static'
        criterion through done_definition and the verifier cascade; tasks without files pass.
        """
        files = [f for f in task.get('files') or [] if str(f).strip()]
        if not files:
            return ''
        from .done_definition import build_done_definition
        from .verifier import run_verifier
        root = self._project_root(run)

        def files_exist(criterion):
            problems = []
            for name in criterion['target']:
                try:
                    relative = file_registry.normalize(root, name)
                except ValueError:
                    problems.append('outside the project folder: ' + name)
                    continue
                if not (root / relative).exists():
                    problems.append('missing: ' + relative)
            return dict(passed=not problems, detail='; '.join(problems)[:300])

        criteria = build_done_definition(task['id'], dict(criteria=[dict(
            id='files', method='static', target=files, text='Every declared file exists inside the project folder')]))
        # verifier.run_verifier only treats 'high' specially: it then demands a model_review
        # callable (verifier.py:153-159), which the engine does not have yet, so high stays 'low'.
        risk = str(task.get('risk') or '').strip().lower()
        if risk not in ('low', 'medium'):
            risk = 'low'
        verdict = run_verifier(task['id'], criteria, dict(static=files_exist), risk)
        if verdict['passed']:
            return ''
        return 'Machine check failed: ' + '; '.join(d['detail'] for d in verdict['defects'])[:400]

    def _main_decisions(self, run, tasks):
        """Finished tasks the Main must decide on: a failed machine check, a failed operator acceptance
        check or requires_review. Reasons go to run['needs_main_reasons']; nothing is retried (I5)."""
        from .operator_main import result_done
        reasons, machine = {}, {}
        for t in tasks:
            if t['state'] != 'done':
                continue
            why = []
            # Read-only stage tasks and builds superseded by a fixer are judged through their chain.
            staged = t.get('read_only') or t.get('fixed_by')
            failed = None if staged else self._machine_check(run, t)
            if failed:
                machine[t['id']] = failed
                why.append(failed)
            if run.get('operator') and not staged and not result_done(t.get('result', '')):
                why.append('Answer has no line RESULT: DONE')
            if t.get('review_open'):
                why.append('Review open points: ' + t['review_open'])
            if t.get('requires_review'):
                why.append('The task asked for review')
            if t.get('agent_red_flag'):  # I12: never auto-accepted
                from .agent_guard import PROBLEM_TITLE
                why.append(PROBLEM_TITLE)
            if why:
                reasons[t['id']] = '; '.join(why)
        run['needs_main_reasons'], run['machine_check_failed'] = reasons, machine
        return list(reasons)

    def _record_outcomes(self, run, tasks):
        """Honest result counts. Sets task['outcome'] on every finished task and run['outcomes'], run['fixes'],
        run['reviews']; no state value changes. Operator executors are 'not_done' without a final RESULT: DONE line.
        Stage roles (reviewer, architect, ...) answer in JSON, so they are 'accepted' when finished.
        'not_done' wins over 'check_failed' (the task itself said it did not finish)."""
        from .operator_main import result_done
        machine = run.get('machine_check_failed') or {}
        counts = dict(accepted=0, not_done=0, check_failed=0)
        for t in tasks:
            if t['state'] != 'done':
                continue
            if t['role'] != 'executor':
                outcome = 'accepted'
            elif run.get('operator') and not result_done(t.get('result', '')):
                outcome = 'not_done'
            elif t['id'] in machine or t.get('agent_red_flag'):
                outcome = 'check_failed'
            else:
                outcome = 'accepted'
            if t.get('outcome') != outcome:
                t['outcome'] = outcome
                self.store.put('tasks', t)
            if t['role'] == 'executor' and not t.get('fixed_by'):  # the same tasks the run summary counts
                counts[outcome] += 1
        reviews = dict(approved=0, changes_requested=0, skipped=0)
        for t in tasks:
            if t['role'] == 'executor':
                if t.get('review_skipped'):  # marker set by operator_roles.step; never counted as approved
                    reviews['skipped'] += 1
                elif t.get('review_state') == 'approved':
                    reviews['approved'] += 1
            elif t['role'] == 'reviewer' and t['state'] == 'done':
                try:
                    data = json.loads(t.get('result') or '{}')
                except ValueError:
                    data = {}
                if isinstance(data, dict) and data.get('passed') is False:
                    reviews['changes_requested'] += 1
        fixes = sum(1 for t in tasks if t.get('fix_of'))
        run['outcomes'], run['reviews'] = counts, reviews
        # max(): the non-operator fix-round counter (run['fixes'] < max_fix_rounds) must never go backwards.
        run['fixes'] = max(int(run.get('fixes') or 0), fixes)

    @staticmethod
    def _outcome_text(run):
        """Plain English counts for the run summary; empty when everything is accepted with no fix and no review."""
        o, r, fixes = run.get('outcomes') or {}, run.get('reviews') or {}, run.get('fixes') or 0
        if not (o.get('not_done') or o.get('check_failed') or fixes or any(r.values())):
            return ''
        return ('; outcomes: %d accepted, %d not done, %d check failed; fixes: %d; reviews: %d approved, '
                '%d changes requested, %d skipped') % (
            o.get('accepted', 0), o.get('not_done', 0), o.get('check_failed', 0), fixes,
            r.get('approved', 0), r.get('changes_requested', 0), r.get('skipped', 0))

    def _finish(self, run, tasks):
        # Three-stage acceptance: machine check -> Main -> done. Fully passing runs finish at once.
        needs = self._main_decisions(run, tasks)
        self._record_outcomes(run, tasks)
        for task_id, reason in run['machine_check_failed'].items():
            self.store.event('machine_check_failed', run['id'], task_id=task_id, message=reason)
        if needs:
            run.update(state='awaiting_main', needs_main=needs)
            self.store.event('awaiting_main', run['id'], message='Waiting for Main confirmation: ' + ', '.join(needs))
        else:
            run['state'] = 'done'

    def confirm_run(self, run_id):
        """POST /api/run/confirm: the Main accepts a run in awaiting_main; only awaiting_main -> done."""
        with self.lock:
            run = self.store.get('runs', run_id)
            if not run:
                raise ValueError('Run not found')
            if run.get('state') != 'awaiting_main':
                raise ValueError('Only a run awaiting Main confirmation can be confirmed (state: %s)' % run.get('state', ''))
            now = time.time()
            run.update(state='done', confirmed_at=now, updated=now)
            self.store.put('runs', run)
            self.store.event('run_confirmed', run_id, message='The Main confirmed the run')
            self.store.event('stage_changed', run_id, message='done')
            return dict(id=run_id, state='done', confirmed_at=now)

    def resolve_uncertain(self, task_id, resolution, note=''):
        """The Main decides a task whose Notion turn finished but whose answer never reached the harness.

        Nothing is sent again (I5). 'accept': the Main checked the work directly and takes it as the result
        (RESULT: DONE). 'abandon': the task ends as failed, so its run stops for the Main. Either way the
        reserved thread is released and the task no longer holds its files."""
        if resolution not in ('accept', 'abandon'):
            raise ValueError("resolution must be 'accept' or 'abandon'")
        with self.lock:
            task = self.store.get('tasks', task_id)
            if not task:
                raise ValueError('Task not found')
            if task.get('state') != 'uncertain' and not task.get('remote_unknown'):
                raise ValueError('Only an unconfirmed task can be resolved by the Main (state: %s)' % task.get('state', ''))
            if not task.get('remote_done_at'):
                raise ValueError('Notion has not confirmed that the turn finished; wait for it or stop the turn first')
            if any(info.get('task_id') == task_id for info in self.inflight.values()):
                raise ValueError('A reconciliation check is running for this task; try again in a few seconds')
            note = ' '.join(str(note or '').split())[:2000]
            now = time.time()
            if task.get('worker_id'):
                self._release(task['worker_id'], task.get('lease', ''))
            for key in ('remote_unknown', 'side_effect_hold', 'reconcile_at'):
                task.pop(key, None)
            if resolution == 'accept':
                summary = ('Accepted by the Main after reconciliation: the Notion turn finished, its answer never '
                           'reached the harness, and the Main checked the work directly.')
                if note:
                    summary += ' ' + note
                task.update(state='done', requires_review=False, error='',
                            result=json.dumps(dict(status='done', summary=summary + '\nRESULT: DONE'), ensure_ascii=False))
            else:
                task.update(state='failed',
                            error=('Abandoned by the Main after reconciliation; nothing was resent. ' + note).strip()[:400])
            task.update(main_resolution=resolution, resolved_at=now, finished=now)
            self.store.put('tasks', task)
            self.store.event('task_resolved_by_main', task['run_id'], task_id=task_id, worker_id=task.get('worker_id', ''),
                             resolution=resolution,
                             message='The Main %s the unconfirmed task without resending it'
                                     % ('accepted' if resolution == 'accept' else 'abandoned'))
            return dict(id=task_id, state=task['state'], resolution=resolution)

    def coordination(self):
        budget = self._budget()
        out = budget.status() if budget is not None else dict(paused=False)
        try:
            out = dict(out, patch=self.patch_status())
        except Exception:
            pass
        return out

    def resume_remote(self):
        # Manual resume after ACCOUNT_BLOCKED: lift the pause and requeue only the tasks it stopped.
        with self.lock:
            budget = self._budget()
            resumed = budget.resume() if budget is not None else False
            requeued, runs = 0, set()
            for task in self.store.all('tasks'):
                if task.get('paused_by') and task['state'] == 'blocked' and not task.get('remote_unknown'):
                    hazard = self._resend_hazard(task)
                    if hazard:
                        # Same rule as retry_blocked: never resend work that already reached Notion; it stays blocked.
                        self.store.event('resend_refused', task['run_id'], task_id=task['id'], message=hazard)
                        continue
                    task.pop('paused_by', None)
                    task.update(state='queued', error='', next_at=0)
                    self.store.put('tasks', task)
                    requeued += 1
                    runs.add(task['run_id'])
            resumed_runs = 0
            for run_id in runs:
                run = self.store.get('runs', run_id)
                if run and run['state'] == 'blocked' and not any(
                        t['state'] in ('failed', 'blocked') or t.get('remote_unknown') for t in self.tasks(run_id)):
                    run.update(state=run.get('blocked_stage', 'executing'), error='')
                    self.store.put('runs', run)
                    self.store.event('stage_changed', run['id'], message=run['state'])
                    resumed_runs += 1
            return dict(resumed=resumed, requeued_tasks=requeued, resumed_runs=resumed_runs)

    def _resend_hazard(self, task):
        """Why requeueing this blocked/failed task could send work that already reached Notion again ('' = safe)."""
        if task.get('side_effect_hold'):
            return 'the failed attempt used tools or claimed files'
        sent = float(task.get('sent_at') or 0)
        if not sent or task.get('submission_phase') != 'inference':
            return ''  # nothing of the current attempt was submitted
        confirmed = float(task.get('remote_done_at') or 0) >= sent or task.get('model_verification') == 'turn_error'
        if not confirmed:
            return 'the submitted turn has no confirmed outcome'
        if self._attempt_side_effects(task):
            return 'the submitted turn used tools or claimed files'
        return ''

    def retry_blocked(self, run_id, allow_resend=False):
        with self.lock:
            run = self.store.get('runs', run_id)
            if not run or run['state'] not in ('blocked', 'failed'):
                raise ValueError('Only a blocked or failed run can be resumed')
            if any(t['state'] == 'uncertain' or t.get('remote_unknown') for t in self.tasks(run_id)):
                raise ValueError('Wait for reconciliation before retrying')
            hazards = {t['id']: self._resend_hazard(t) for t in self.tasks(run_id) if t['state'] in ('blocked', 'failed')}
            hazards = {k: v for k, v in hazards.items() if v}
            if hazards and not allow_resend:
                raise ValueError('Retry would resend work that already reached Notion: ' +
                                 '; '.join('%s (%s)' % item for item in sorted(hazards.items())))
            budget = self._budget()
            if budget is not None:
                budget.resume()  # Retry on a blocked run doubles as the manual resume after ACCOUNT_BLOCKED.
            run['state'] = run.get('blocked_stage', 'executing')
            run['error'] = ''
            for task in self.tasks(run_id):
                if task['state'] in ('blocked', 'failed'):
                    if task['id'] in hazards:
                        task.pop('side_effect_hold', None)
                        self.store.event('manual_resend', run_id, task_id=task['id'], message=hazards[task['id']])
                    task.update(state='queued', attempts=0, error='', next_at=0)
                    task.pop('paused_by', None)
                    self.store.put('tasks', task)
            self.store.put('runs', run)

    def _project_root(self, run):
        return Path(run.get('project_path') or Path(self.store.directory) / 'artifacts' / run['id']).resolve()

    def _resource_keys(self, run, files):
        if not files:
            return []
        try:
            return file_registry.resource_keys(self._project_root(run), files)
        except ValueError as exc:
            raise ContractError('A planned file is outside the project folder: ' + str(exc)) from exc

    @staticmethod
    def _overlap(a, b):
        # Folder/file locks also protect overlapping nested directories.
        sep = '\\' if '\\' in a or '\\' in b else '/'
        return a == b or a.startswith(b.rstrip(sep) + sep) or b.startswith(a.rstrip(sep) + sep)

    def _lock_files(self, run, files):
        """Files that lock. A folder in files ('tests/', '.', or an existing folder) is advisory and never locks."""
        root = self._project_root(run) if run else None
        kept = []
        for name in files or []:
            text = str(name).strip()
            if not text or text.endswith(('/', '\\')) or text in ('.', './'):
                continue
            try:
                if root is not None and (root / text).is_dir():
                    continue
            except (OSError, ValueError):
                pass
            kept.append(name)
        return kept

    def _conflicts(self, task, run):
        # Patch mode: every task edits its own worktree, so project file and folder locks are skipped.
        # Explicit full-PC paths (absolute_resources) still lock in both modes.
        patch = self._patch_on(run)
        keys = ([] if patch else self._resource_keys(run, self._lock_files(run, task['files']))) + task.get('absolute_resources',[])
        if not keys:
            return False
        for other in self.store.all('tasks'):
            if other['id'] == task['id'] or (other['state'] not in ('running', 'cancelling', 'uncertain') and not other.get('remote_unknown')):
                continue
            other_run = self.store.get('runs', other['run_id'])
            theirs = ([] if patch or self._patch_on(other_run) else self._resource_keys(other_run, self._lock_files(other_run, other['files'])))
            theirs = theirs + other.get('absolute_resources',[])
            for a in keys:
                if any(self._overlap(a,b) for b in theirs):
                    return True
        return False

    # ---- patch mode (patch_mode.py) ------------------------------------------
    def _patch_on(self, run):
        """True when patch mode applies: setting patch_mode (default on) and a git project with a main branch."""
        if not run or not run.get('project_path'):
            return False
        value = self.store.settings().get(patch_mod.SETTING, PATCH_MODE_DEFAULT)
        if not patch_mod.enabled({patch_mod.SETTING: value}):
            return False
        root = str(self._project_root(run))
        cache = self.__dict__.setdefault('_patch_roots', {})
        now = time.monotonic()
        hit = cache.get(root)
        if hit and now - hit[1] < 60:
            return hit[0]
        ok = False
        if (Path(root) / '.git').exists():
            try:
                ok = subprocess.run(['git', '-C', root, 'rev-parse', '--verify', '-q', 'refs/heads/' + patch_mod.BASE],
                                    capture_output=True, timeout=10,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).returncode == 0
            except Exception:
                ok = False
        cache[root] = (ok, now)
        return ok

    def _patcher(self, run):
        root = str(self._project_root(run))
        patchers = self.__dict__.setdefault('_patchers', {})
        if root not in patchers:
            factory = getattr(self, 'patch_factory', None) or patch_mod.PatchMode
            patchers[root] = factory(root)
        return patchers[root]

    def _patch_prepare(self, task, run):
        """Own worktree for an executor before dispatch. On a git error the task runs as before (with locks off)."""
        try:
            patcher = self._patcher(run)
            task['patch_branch'] = patcher.branch_name(task)
            task['patch_worktree'] = patcher.prepare(task)
            task['patch_state'] = 'working'
        except Exception as exc:
            for key in ('patch_branch', 'patch_worktree', 'patch_state'):
                task.pop(key, None)
            self.store.event('patch_prepare_failed', run['id'], commit=False, task_id=task['id'], message=str(exc)[:300])

    def _start_merges(self):
        """collect -> verify -> merge for finished patch tasks, one at a time (PatchMode merge queue)."""
        with self.lock:
            self._start_merges_locked()

    def _start_merges_locked(self):
        merging = self.__dict__.setdefault('_patch_merging', set())
        heads = {}  # project root -> current main commit (only asked when a task waits for a red main)
        for task in self.store.all('tasks'):
            if task.get('patch_state') != 'waiting' or task['id'] in merging or not task.get('patch_worktree'):
                continue
            run = self.store.get('runs', task['run_id'])
            if not run:
                continue
            red_at = task.get('patch_main_red')
            if red_at:
                root = str(self._project_root(run))
                if root not in heads:
                    try:
                        patcher = self._patcher(run)
                        heads[root] = patcher.rev(getattr(patcher, 'base', patch_mod.BASE))
                    except Exception:
                        heads[root] = None
                if heads[root] == red_at:
                    continue  # main is still the red commit: wait for a fix before merging again
            merging.add(task['id'])
            try:
                future = self._patcher(run).submit(dict(task))
            except Exception as exc:
                merging.discard(task['id'])
                self._patch_result(task['id'], dict(status='error', message=str(exc)[:600]))
                continue
            # The result is stored from a fresh thread, never inside the caller that may hold self.lock.
            future.add_done_callback(lambda f, tid=task['id']: threading.Thread(
                target=self._merged, args=(tid, f), daemon=True, name='patch-result').start())

    def _merged(self, task_id, future):
        try:
            result = future.result()
        except Exception as exc:
            result = dict(status='error', message=str(exc)[:600])
        self._patch_result(task_id, result)

    def _patch_result(self, task_id, result):
        with self.lock:
            merging = self.__dict__.setdefault('_patch_merging', set())
            task = self.store.get('tasks', task_id)
            if not task:
                merging.discard(task_id)
                return
            status = str(result.get('status') or 'error')
            main_red = status == 'main_red'
            # A red main is not the task's fault: waiting for it never uses up the retry limit.
            tries = task.get('patch_tries', 0) + (0 if main_red else 1)
            if status in ('merged', 'nothing'):
                state = 'merged'
            elif status == 'conflict':
                state = 'conflict'
            elif main_red:
                state = 'waiting'
            elif status in PATCH_RETRY and tries < PATCH_RETRY_LIMIT:
                state = 'waiting'
            else:
                state = 'failed'
            task.update(patch_state=state, patch_tries=tries,
                        patch_result=dict(status=status, message=str(result.get('message') or '')[:600], at=time.time(),
                                          main_after=str(result.get('main_after') or ''), pushed=bool(result.get('pushed')),
                                          failing=[str(n) for n in (result.get('failing') or [])][:20],
                                          tests=patch_mod.test_tail(result)))
            if main_red:
                red_at = str(result.get('main_red_commit') or result.get('main_before') or '')
                task['patch_main_red'] = red_at
                seen = self.__dict__.setdefault('_main_red_seen', set())
                # One operator event per red main commit, not one per task; the stored events keep this across restarts.
                if red_at not in seen and not self._main_red_reported(red_at):
                    seen.add(red_at)
                    self.store.event('main_red', task['run_id'], commit=False, task_id=task_id, main_commit=red_at,
                                     failing=task['patch_result']['failing'], message=task['patch_result']['message'][:300])
            else:
                task.pop('patch_main_red', None)
            run = self.store.get('runs', task['run_id'])
            if state == 'conflict' and run and not task.get('patch_follow_up'):
                follow = result.get('follow_up') or {}
                new = self.add_task(run, 'executor', follow.get('prompt') or 'Resolve the merge conflict.',
                                    title=follow.get('title') or 'Resolve conflict', files=follow.get('files') or [],
                                    acceptance='The conflict is fixed in the given folder and the tests pass')
                new.update(patch_branch=follow.get('patch_branch') or task.get('patch_branch'),
                           patch_worktree=follow.get('patch_worktree') or task.get('patch_worktree'),
                           patch_origin=task['id'])
                self.store.put('tasks', new)
                task['patch_follow_up'] = new['id']
            origin = self.store.get('tasks', task['patch_origin']) if task.get('patch_origin') else None
            if origin and state in ('merged', 'failed'):
                origin.update(patch_state=state, patch_result=task['patch_result'])
                self.store.put('tasks', origin)
            self.store.event('patch_' + state, task['run_id'], commit=False, task_id=task_id, status=status,
                             message=task['patch_result']['message'][:300])
            self.store.put('tasks', task)
            merging.discard(task_id)  # only after the new patch_state is stored, so no second merge starts

    def patch_status(self):
        """Settings status: is patch mode on, merges today, conflicts waiting, tasks waiting to merge."""
        start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        tasks = self.store.all('tasks')
        value = self.store.settings().get(patch_mod.SETTING, PATCH_MODE_DEFAULT)
        return dict(enabled=patch_mod.enabled({patch_mod.SETTING: value}),
                    merges_today=sum(1 for t in tasks if t.get('patch_state') == 'merged' and not t.get('patch_origin')
                                     and (t.get('patch_result') or {}).get('at', 0) >= start),
                    conflicts_waiting=sum(1 for t in tasks if t.get('patch_state') == 'conflict'),
                    waiting_to_merge=sum(1 for t in tasks if t.get('patch_state') == 'waiting'),
                    labels={t['id']: PATCH_LABELS[t['patch_state']] for t in tasks if t.get('patch_state') in PATCH_LABELS})

    def _main_red_reported(self, main_commit):
        """True when the store already holds a 'main_red' event for this main commit (survives restarts)."""
        db = getattr(self.store, 'db', None)
        if not main_commit or db is None:
            return False
        try:
            with self.store.lock:
                row = db.execute("SELECT 1 FROM events WHERE kind='main_red' AND json_extract(doc,'$.main_commit')=? "
                                 "LIMIT 1", (main_commit,)).fetchone()
        except Exception:
            return False
        return row is not None

    def _remerge_refusal(self, task):
        """Why a task may not be merged again, or None. Tasks without their worktree are never touched."""
        if task.get('state') != 'done':
            return 'The task is not done'
        if task.get('patch_state') != 'failed':
            return 'The merge of this task did not fail'
        status = (task.get('patch_result') or {}).get('status')
        if status not in REMERGE_STATUSES:
            return 'The merge failed with status %r, not red or main_red' % status
        folder = task.get('patch_worktree')
        if not folder or not (Path(folder) / '.git').exists():
            return 'The task has no worktree any more; nothing to merge again'
        return None

    def remerge(self, task_id=None, since=None):
        """Put merges rejected as red/main_red back to 'waiting' (tries reset); tick() merges them again.

        task_id: one task (refusal raises ValueError). since: all such tasks whose merge failed at or after
        this unix time; refused candidates are listed in 'skipped'. The task itself is never re-run (I5).
        """
        if task_id is None and since is None:
            raise ValueError('Give a task id or since (unix time)')
        with self.lock:
            skipped = []
            if task_id is not None:
                task = self.store.get('tasks', str(task_id))
                if not task:
                    raise ValueError('Task not found')
                reason = self._remerge_refusal(task)
                if reason:
                    raise ValueError(reason)
                picked = [task]
            else:
                since = float(since)
                picked = []
                for task in self.store.all('tasks'):
                    result = task.get('patch_result') or {}
                    if task.get('patch_state') != 'failed' or result.get('status') not in REMERGE_STATUSES \
                            or float(result.get('at') or 0) < since:
                        continue
                    reason = self._remerge_refusal(task)
                    if reason:
                        skipped.append(dict(id=task['id'], reason=reason))
                    else:
                        picked.append(task)
            ids = []
            for task in picked:
                old = (task.get('patch_result') or {}).get('status')
                task.update(patch_state='waiting', patch_tries=0, patch_remerged_at=time.time())
                task.pop('patch_main_red', None)
                self.store.put('tasks', task)
                self.store.event('patch_remerge', task['run_id'], commit=False, task_id=task['id'], status=old)
                ids.append(task['id'])
            return dict(tasks=ids, count=len(ids), skipped=skipped)

    def claim_file(self, task_id, lease, relative):
        with self.lock:
            task = self.store.get('tasks', task_id)
            if not task or task['state'] not in LIVE_TASK_STATES or task['lease'] != lease:
                raise ValueError('Assignment is no longer active')
            if task.get('read_only'):
                raise ValueError('This role is read-only; describe the needed change in your result instead')
            run = self.store.get('runs', task['run_id'])
            root = self._project_root(run)
            relative = file_registry.normalize(root, relative)
            held = {file_registry.path_key(root / f) for f in file_registry.normalize_files(root, task['files'], strict=False)}
            if file_registry.path_key(root / relative) in held:
                return  # this task already holds the file under another spelling
            trial = dict(task, files=task['files'] + [relative])
            if file_registry.operator_owner(self.store, self._resource_keys(run, [relative])):
                raise ValueError('The operator is editing this file right now; describe the needed change in your result instead')
            if self._conflicts(trial, run):
                raise ValueError('Another active task owns this file; request a dependency instead')
            task['files'] = trial['files']
            if task.get('proof_base'):
                task_proof.extend(task['id'], relative, root)  # Proof base for the newly claimed file.
            self.store.put('tasks', task)

    def check_lease(self, task_id, lease):
        with self.lock:
            task = self.store.get('tasks', task_id)
            if not task or task['state'] not in LIVE_TASK_STATES or task['lease'] != lease:
                raise ValueError('Assignment is no longer active')

    def claim_absolute(self, task_id, lease, path):
        """Serialize explicit full-PC writes using the same cross-chat ownership."""
        with self.lock:
            task=self.store.get('tasks',task_id)
            if not task or task['state'] not in LIVE_TASK_STATES or task['lease']!=lease:
                raise ValueError('Assignment is no longer active')
            # One key per file however it is spelled (case, slashes, ./, ..), as claim_file uses.
            resource=file_registry.path_key(path)
            if file_registry.operator_owner(self.store,[resource]):
                raise ValueError('The operator is editing this PC path right now; describe the needed change in your result instead')
            trial=dict(task,absolute_resources=sorted({file_registry.path_key(r) for r in task.get('absolute_resources',[])}|{resource}))
            if self._conflicts(trial,self.store.get('runs',task['run_id'])):
                raise ValueError('Another active task owns this PC path; request a dependency instead')
            self.store.put('tasks',trial)

    # ---- Token hard limit: stop running coworker turns ------------------------------------------
    def limit_state(self, now=None):
        """Read-only: dict(hard, source '6h'|'burst'|'', wait, level, reason). The throttle level is never hard."""
        from . import token_budget
        now = self.clock() if now is None else now
        try:
            wait = float(token_budget.exhausted(self.store, now) or 0)
        except Exception:
            wait = 0.0
        if wait > 0:
            return dict(hard=True, source='6h', wait=wait, level='pause', reason='6 h token budget used up')
        try:
            burst = token_budget.burst_state(self.store, now) or {}
        except Exception:
            burst = {}
        level = str(burst.get('level') or 'ok')
        if level == 'pause':
            return dict(hard=True, source='burst', wait=float(burst.get('wait') or 120), level=level,
                        reason=str(burst.get('reason') or 'token burst hard limit'))
        return dict(hard=False, source='', wait=0.0, level=level, reason='')

    def _limit_check(self):
        """Called by tick() under the lock; cheap (two ledger reads) and at most every LIMIT_CHECK_SECONDS."""
        now = self.clock()
        last = getattr(self, '_limit_checked', None)
        if last is not None and 0 <= now - last < LIMIT_CHECK_SECONDS:
            return getattr(self, 'limit_latch', None)
        self._limit_checked = now
        state = self.limit_state(now)
        latch = getattr(self, 'limit_latch', None)
        if state['hard']:
            self._limit_clear_since = 0.0
            stopped, kept = self._limit_stop(state)
            task_ids = [info['task_id'] for info, _ in stopped]
            worker_ids = [info['worker_id'] for info, _ in stopped]
            if latch is None:
                self.limit_latch = dict(since=now, source=state['source'], wait=state['wait'],
                                        reason=state['reason'], task_ids=task_ids)
                self.store.event(LIMIT_STOP_EVENT, '', source=state['source'], reason=state['reason'],
                                 wait_s=round(state['wait'], 1), count=len(stopped), task_ids=task_ids,
                                 worker_ids=worker_ids, main_kept=kept,
                                 message='Token hard limit reached (%s): %d running coworker turn(s) stopped; '
                                         'Main chats untouched' % (state['reason'], len(stopped)))
            else:
                latch.update(source=state['source'], wait=state['wait'], reason=state['reason'])
                if stopped:
                    latch['task_ids'] = list(latch.get('task_ids') or []) + task_ids
                    self.store.event('limit_stop_more', '', source=state['source'], count=len(stopped),
                                     task_ids=task_ids, worker_ids=worker_ids,
                                     message='Token limit still reached: %d more coworker turn(s) stopped' % len(stopped))
            if stopped:
                self._limit_stop_turns(stopped)
        elif latch is not None:
            # Hysteresis: two clear checks in a row, at least LIMIT_CLEAR_SECONDS apart.
            if not self._limit_clear_since:
                self._limit_clear_since = now
            elif now - self._limit_clear_since >= LIMIT_CLEAR_SECONDS:
                self._limit_resume(latch, now)
        return self.limit_latch

    def _limit_stop(self, state):
        """Set the cancel Event of every running coworker turn. Main/operator turns and recovery reads stay."""
        stopped, kept = [], 0
        for future, info in list(self.inflight.items()):
            if info.get('recovery') or info.get('limit_stop') or future.done():
                continue  # a turn that already finished counts as done
            worker = self.store.get('workers', info.get('worker_id')) or {}
            if worker.get('purpose') != 'worker':
                kept += 1
                continue
            info.update(limit_stop=True, limit_wait=float(state.get('wait') or 0))
            cancel = info.get('cancel')
            if cancel is not None:
                cancel.set()
            stopped.append((info, worker))
        return stopped, kept

    def _limit_stop_turns(self, stopped):
        """Ask the adapter to stop the remote turns, off the engine lock; every answer is logged."""
        stop = getattr(self.adapter, 'stop_turn', None)
        if not callable(stop):
            return None
        jobs = []
        for info, worker in stopped:
            task = self.store.get('tasks', info['task_id']) or {}
            jobs.append((info['task_id'], info.get('run_id') or '', info['worker_id'],
                         task.get('thread_id') or worker.get('thread_id') or '', dict(worker)))

        def call():
            for task_id, run_id, worker_id, thread_id, worker in jobs:
                try:
                    answer = stop(thread_id, worker)
                except Exception as exc:
                    answer = dict(ok=False, reason='error: %s' % str(exc)[:200])
                if not isinstance(answer, dict):
                    answer = dict(ok=bool(answer), reason='')
                try:
                    self.store.event('limit_stop_turn', run_id, task_id=task_id, worker_id=worker_id,
                                     thread_id=thread_id, ok=bool(answer.get('ok')),
                                     reason=str(answer.get('reason') or ''),
                                     message='Remote stop for a limit-stopped turn: %s' %
                                             ('ok' if answer.get('ok') else str(answer.get('reason') or 'not stopped')))
                except Exception:
                    pass

        thread = threading.Thread(target=call, name='limit-stop-turn', daemon=True)
        self._limit_stop_thread = thread
        thread.start()
        return thread

    def _limit_hold(self, task, info, exc):
        """Outcome of a limit-stopped turn that ended with an exception (never failed, never resent)."""
        now = self.clock()
        wait = float(info.get('limit_wait') or 0) or 120.0
        started = float(task.get('started') or 0)
        sent = (task.get('submission_phase') not in (None, '', 'preparing')
                or float(task.get('sent_at') or 0) >= started > 0)
        if str(exc).startswith('Assignment cancelled before') or not sent:
            # Nothing reached Notion: the chat is fine, the attempt is not used, the task waits for the limit.
            self._release(info['worker_id'], info['lease'])
            task.pop('remote_unknown', None)
            task.update(state='retry', attempts=max(0, task['attempts'] - 1), next_at=now + wait,
                        paused_by=LIMIT_PAUSED_BY, limit_stopped_at=now,
                        error='Stopped by token limit before anything was sent; waits until the limit clears '
                              '(about %d s)' % int(wait))
        else:
            # I5: the turn may have left; it is read back by reconcile, never resent.
            task.update(state='uncertain', remote_unknown=False, reconcile_checks=task.get('reconcile_checks', 0) + 1,
                        reconcile_at=time.time() + 5, paused_by=LIMIT_PAUSED_BY, limit_stopped_at=now,
                        error='Stopped locally by token limit; the Notion turn is read back, never resent')
            worker = self.store.get('workers', info['worker_id'])
            if worker:
                worker['state'] = 'recovering'
                self.store.put('workers', worker)

    def _limit_resume(self, latch, now):
        self.limit_latch = None
        self._limit_clear_since = 0.0
        resumed = 0
        for task in self.store.all('tasks'):
            if task.get('paused_by') != LIMIT_PAUSED_BY or task.get('state') != 'retry':
                continue
            task.pop('paused_by', None)
            task.update(next_at=min(float(task.get('next_at') or 0), now), error='')
            self.store.put('tasks', task)
            resumed += 1
        self.store.event(LIMIT_RESUME_EVENT, '', source=latch.get('source', ''), reason=latch.get('reason', ''),
                         stopped_s=round(max(0.0, now - float(latch.get('since') or now)), 1), resumed=resumed,
                         message='Token limit cleared: %d waiting coworker task(s) resume' % resumed)

    def _limit_charge_reply(self, task, info, text):
        """A limit-stopped turn read back as done: its reply was never charged (the stream was cancelled)."""
        task.pop('paused_by', None)
        if not info.get('recovery') or task.get('limit_reply_charged'):
            return
        from . import token_budget
        try:
            token_budget.charge_reply(self.store, self.store.get('workers', info['worker_id']) or {}, text)
        except Exception:
            pass
        task['limit_reply_charged'] = True

    def tick(self):
        with self.lock:
            for future, info in list(self.inflight.items()):
                if future.done():
                    del self.inflight[future]
                    if info.get('done_at'):
                        # Seconds from the finished remote call to its completion here (lock wait + tick start).
                        info['complete_wait_s'] = round(max(0.0, time.time() - info['done_at']), 3)
                    self._complete(future, info)
            # Owner rule: at the hard token limit every running coworker turn stops at once (Main untouched).
            limit_latch = self._limit_check()
            self._start_merges()
            self._reconcile()
            self._heal_workers()
            try:
                from .account_failover import rehome_runs
                rehome_runs(self.store)  # B41: runs of dead/AI-disabled accounts move to a working Main
            except Exception as exc:  # never let failover stop dispatch
                self.store.event('run_rehome_failed', '', error=str(exc)[:300])
            runs = self.store.all('runs')
            if getattr(self.adapter, 'single_account_harness', False):
                from .harness_account import runnable_accounts
                # B39: every Main of every account may run while at least one account is ready (its executor
                # tasks go to coworkers of ready accounts); each chat stays strictly ordered.
                runs = runs if runnable_accounts(self.store) else []
            for run in runs:
                if run['state'] == 'queued' and self._run_slot_free(run, runs):
                    run['state'] = 'approach'
                    self.store.put('runs', run)
                    self._approach(run)
                if run['state'] in ACTIVE:
                    self._advance(run)
            active = [self.store.get('runs', r['id']) for r in runs if self.store.get('runs', r['id'])['state'] in ACTIVE]
            if not active:
                return
            offset = self.round_robin % len(active)
            active = active[offset:] + active[:offset]
            self.round_robin += 1
            settings = self.store.settings()
            # Round-robin one assignment per run per pass, across all accounts.
            while len(self.inflight) < min(40, settings['max_workers']):
                assigned = False
                for run in active:
                    if len(self.inflight) >= min(40, settings['max_workers']):
                        break
                    if sum(info['run_id']==run['id'] for info in self.inflight.values()) >= run.get('worker_budget', max(1,run['capacity'])):
                        continue
                    for task in self.tasks(run['id']):
                        if task['state'] not in ('queued', 'retry') or task['next_at'] > self.clock():
                            continue
                        deps = [self.store.get('tasks', d) for d in task['deps']]
                        if any(not d or d['state'] in ('failed', 'blocked', 'cancelled') for d in deps):
                            task.update(state='blocked', error='A dependency failed')
                            self.store.put('tasks', task)
                            continue
                        if any(d['state'] != 'done' for d in deps) or self._conflicts(task, run):
                            continue
                        if any(d.get('patch_worktree') and d.get('patch_state') in ('working', 'waiting') for d in deps):
                            # A dependency's build is still merging: its copy may vanish and main lacks it yet.
                            continue
                        first_ready = not task.get('ready_at')
                        if first_ready:
                            # Committed with the next task write, so the dispatch path gets no extra disk sync.
                            task['ready_at'] = time.time()
                            self.store.event('task_ready', run['id'], commit=False, task_id=task['id'], deps=len(task['deps']))
                        worker = self._choose_worker(task, run, settings)
                        if worker and limit_latch is not None and worker.get('purpose') == 'worker':
                            # Token hard limit: no new coworker turn until the latch clears; next_at is not
                            # changed, so nothing piles up. Main tasks still dispatch (run() keeps its checks).
                            worker = None
                        if worker:
                            self._dispatch(task, run, worker)
                            assigned = True
                            break
                        if first_ready:
                            self.store.put('tasks', task)
                if not assigned:
                    break

    def _choose_worker(self, task, run, settings):
        workers = self.store.all('workers')
        if getattr(self.adapter, 'single_account_harness', False):
            from .harness_account import runnable_accounts
            usable = runnable_accounts(self.store)
            workers = [w for w in workers if w['account_id'] in usable]  # B39: coworkers of every ready account
        main_role = task['role'] in ('planner', 'integrator') or task.get('use_main', False)
        # A Main that was just found busy (usually the operator's own chat) hands Main tasks to a free joined coworker.
        main_busy = main_role and (task.get('main_busy_at', 0) > time.time() - MAIN_BUSY_FALLBACK_S
                                   or self._main_compressing(run))
        main_account = self.store.get('workers', run['main_worker'], {}).get('account_id')
        if main_account:
            # B45: Coworkers only on the account the Main is logged in with; never borrowed from other accounts.
            workers = [w for w in workers if w.get('account_id') == main_account]
        choices = [w for w in workers if w.get('enabled') and w['state'] == 'ready'
                   and w.get('cooldown_until', 0) <= time.time()
                   and (w['id'] == run['main_worker'] if main_role and not main_busy else
                        w.get('purpose') == 'worker' and (not main_busy or w['account_id'] == main_account))]
        pending_rotations = {r['id']:r for r in self.store.all('rotations')}
        choices = [w for w in choices if not pending_rotations.get(w['id'], {}).get('task_id') or
                   pending_rotations[w['id']]['task_id'] == task['id'] or
                   self.store.get('tasks', pending_rotations[w['id']]['task_id'], {}).get('state') in TERMINAL]
        if not choices and not main_role and run['capacity'] == 0:
            choices = [w for w in workers if w['id'] == run['main_worker'] and w.get('enabled') and w['state'] == 'ready']
        # I32: a Coworker chat 23 h or older gets no new task in any branch (executor, reviewer, fixer, fallbacks).
        choices = coworker_age_guard.young(self.store, choices, time.time(), run.get('id', ''))
        counts = {}
        checkpoint_workers = {ident for ident, rotation in pending_rotations.items()
                              if rotation.get('state') == 'submitted' or
                              (not rotation.get('semantic_verified') and
                               self.store.get('tasks', rotation.get('summary_task_id'), {}).get('sent_at'))}
        for worker in workers:
            if worker['state'] in ('busy', 'recovering') or worker['id'] in checkpoint_workers:
                counts[worker['account_id']] = counts.get(worker['account_id'], 0) + 1
        accounts = {a['id']:a for a in self.store.all('accounts')}
        choices = [w for w in choices if accounts.get(w['account_id'], {}).get('state')=='ready'
                   and counts.get(w['account_id'], 0) - int(w['id'] in checkpoint_workers) < self._parallel_cap(settings['per_account'])
                   and accounts.get(w['account_id'], {}).get('cooldown_until', 0) <= time.time()]
        if task.get('review_of'):
            # Operator review: a different Coworker than every worker of the reviewed chain; never a Main.
            choices = [w for w in choices if w['id'] not in set(task.get('exclude_workers') or [])
                       and w.get('purpose') == 'worker']
        elif task['role'] == 'reviewer':
            executors = {t.get('worker_id') for t in self.tasks(run['id']) if t['role']=='executor'}
            independent = [w for w in choices if w['id'] not in executors]
            if independent:
                choices = independent
            else:
                candidates, choices = choices, []  # An executor must not review its own contribution.
                # Phase 4 Fix L: a Main that was just found busy (usually the operator's own chat) must not
                # hold the review until BUSY_WAIT_LIMIT; the coworker with the smallest own share reviews.
                review_main_busy = (task.get('main_busy_at', 0) > time.time() - MAIN_BUSY_FALLBACK_S
                                    or self._main_compressing(run))
                main=self.store.get('workers',run['main_worker'])
                if (not review_main_busy and main and main.get('enabled') and main['state']=='ready'
                    and accounts.get(main['account_id'],{}).get('state')=='ready'
                    and main.get('cooldown_until',0)<=time.time()
                    and accounts.get(main['account_id'],{}).get('cooldown_until',0)<=time.time()
                    and counts.get(main['account_id'],0)-int(main['id'] in checkpoint_workers)<self._parallel_cap(settings['per_account'])):
                    choices=[main]  # The planner did not execute any of the parts.
                elif review_main_busy and candidates:
                    shares = {}
                    for done in self.tasks(run['id']):
                        if done['role'] == 'executor':
                            shares[done.get('worker_id')] = shares.get(done.get('worker_id'), 0) + 1
                    least = min(shares.get(w['id'], 0) for w in candidates)
                    choices = [w for w in candidates if shares.get(w['id'], 0) == least]
                    if not task.get('review_fallback'):
                        task['review_fallback'] = True
                        self.store.event('reviewer_fallback', task['run_id'], task_id=task['id'],
                                         message='The Main chat is busy; the coworker with the smallest own share reviews')
        choices = [w for w in choices if not pending_rotations.get(w['id'], {}).get('task_id') or
                   pending_rotations[w['id']]['task_id'] == task['id'] or
                   self.store.get('tasks', pending_rotations[w['id']]['task_id'], {}).get('state') in TERMINAL]
        choices = [w for w in choices if not (coworker_age_guard.is_coworker(w) and coworker_age_guard.too_old(w, time.time()))]
        choices.sort(key=lambda w: (w.get('last_used', 0), counts.get(w['account_id'], 0)))
        preferred = [w for w in choices if task.get('prefer_worker') and w['id'] == task['prefer_worker']]
        return (preferred or choices)[0] if choices else None

    def _dispatch(self, task, run, worker):
        account=self.store.get('accounts',worker['account_id'],{})
        if task['role']=='executor' and task['files'] and not account.get('bridge_connected'):
            task.update(state='blocked',error='Connect NotionTool PC tools before executing tasks that change project files')
            self.store.put('tasks',task)
            return
        task.update(state='running', worker_id=worker['id'], lease=secrets.token_urlsafe(32),
                    step_id=uid(), started=time.time(), attempts=task['attempts']+1, error='', partial='',
                    thread_id=worker['thread_id'], submission_phase='preparing')
        if task.get('paused_by') == LIMIT_PAUSED_BY:
            task.pop('paused_by', None)  # a fresh attempt after a token-limit wait (nothing was sent before)
        # H2 (t3ec11e437): reconcile state belongs to one attempt; a new attempt starts from zero.
        task.update(reconcile_checks=0, remote_done_at=0, reconcile_at=0, approval_seen=None, awaiting_approval='',
                    approval_evidence=False, pending_tool='', pending_tool_state='', turn_state='', turn_state_reason='', done_poll_since=0, done_poll_fast=False,
                        remote_working=False, remote_running=False, remote_progress_at=0, remote_newest_seq=0,
                        remote_steps=0, remote_stall_noted=False)
        # Every attempt keeps its own timing record for the metrics.
        task['attempt_log'] = (task.get('attempt_log') or [])[-19:] + [
            dict(attempt=task['attempts'], worker_id=worker['id'], started=task['started'])]
        if task.get('files') and not task.get('proof_base'):
            # Proof: base snapshot of the write scope, taken once when the task starts (proof.py).
            task['proof_base'] = task_proof.start(task['id'], task['files'], self._project_root(run))
        read_only = bool(task.get('read_only')) or bool(re.search(r'\bread[- ]only\b', task.get('prompt') or '', re.I))
        if task['role'] == 'executor' and self._patch_on(run) and not read_only:
            # own git worktree before dispatch (patch_mode.py); read-only audits skip it (dispatch spread)
            self._patch_prepare(task, run)
        self.store.put('tasks', task)  # Commit the submission marker BEFORE the API can accept it.
        worker.update(state='busy', lease=task['lease'], last_used=time.time(),
                      prev_task_id=worker.get('last_task_id', ''), last_task_id=task['id'])
        self.store.event('lease_acquired', run['id'], commit=False, task_id=task['id'], worker_id=worker['id'], attempt=task['attempts'])
        self.store.put('workers', worker)  # also commits lease_acquired
        context_tasks = [self.store.get('tasks', dep) for dep in task['deps']]
        completed = [t for t in self.tasks(run['id']) if t['state']=='done']
        if task['role']=='planner':
            context_tasks = [t for t in completed if t['role'] in ('architect','researcher','quality_planner')]
        elif task.get('review_of'):
            context_tasks = [t for t in completed if t['id'] in task.get('review_scope', [])]
        elif task['role']=='reviewer':
            # Checks use actual deliverables, not speculative approach answers.
            context_tasks = [t for t in completed if t['role']=='executor']
        elif task['role']=='integrator':
            reviews = [t for t in completed if t['role']=='reviewer']
            context_tasks = [t for t in completed if t['role']=='executor'] + reviews[-1:]
        context = self._context(context_tasks)
        account = self.store.get('accounts', worker['account_id'], {})
        make_prompt=plain_protocol.prompt if getattr(self.adapter,'response_mode','json')=='plain' else prompt_for
        # Every chat sees which files other chats (coworker tasks or the operator) are editing right now.
        claims = file_registry.summary(self.store, run.get('project_path', ''), exclude_task=task['id'], task=task) if task['role'] in ('planner', 'executor') else ''
        tools_available = bool(account.get('bridge_connected') and self.store.settings().get('advertise_project_tools', False))
        prompt_task = dict(task, prompt=task['prompt'] + PATCH_NOTE % (task['patch_worktree'], task.get('patch_branch', ''))) if task.get('patch_worktree') else task
        if task.get('review_of'):
            prompt_task = dict(prompt_task, prompt=prompt_task['prompt'] + review_note(self.store.get('tasks', task['review_of'], None)))
        prompt = make_prompt(prompt_task, dict(run, file_claims=claims) if claims else run, context, task['lease'], tools_available)
        # I18: stable parts (request, memory, project, rules) go only once into each coworker chat.
        full_chars = len(prompt)
        prompt, sent_state = prompt_once.compact(worker, task, run, prompt,
                                                 enabled=self.store.settings().get('prompt_send_once', True))
        worker['prompt_sent'] = sent_state
        if len(prompt) < full_chars:
            self.store.event('prompt_compacted', run['id'], commit=False, task_id=task['id'], worker_id=worker['id'],
                             full_chars=full_chars, sent_chars=len(prompt))
        self.store.put('workers', worker)
        # Committed together with task_started right after the submit.
        self.store.event('context_built', run['id'], commit=False, task_id=task['id'], worker_id=worker['id'], attempt=task['attempts'],
                         context_chars=len(context), context_tasks=sum(1 for t in context_tasks if t), prompt_chars=len(prompt))
        cancel = threading.Event()
        future = self.pool.submit(run_with_priority, 'critical', self.adapter.run, worker, task, prompt, cancel,
                                  lambda text: self._partial(task['id'], task['lease'], text))
        self.inflight[future] = dict(task_id=task['id'], lease=task['lease'], worker_id=worker['id'],
                                    run_id=run['id'], cancel=cancel, recovery=False)
        self._watch(future, self.inflight[future])
        self.store.event('task_started', run['id'], task_id=task['id'], worker=worker['label'], worker_id=worker['id'],attempt=task['attempts'],role=task['role'], message=task['title'])

    def _context(self, tasks):
        parts = []
        for task in tasks:
            if not task:
                continue
            # Full reports remain durable and available to the context MCP tool.
            result = task.get('result', '')
            if getattr(self.adapter,'response_mode','json')=='plain':
                try:result=json.loads(result).get('summary') or result
                except (ValueError,AttributeError):pass
                label=task['title']
            else:
                label=f"[{task['id']}] {task['title']}"
            check = ('\nAcceptance: '+task['acceptance']) if task.get('acceptance') else ''
            state = task.get('patch_state')
            merge = task.get('patch_result') if isinstance(task.get('patch_result'), dict) else {}
            if state == 'merged' and merge.get('status') == 'nothing':
                check += '\nWhere: this task changed no files; its files were already in the main project folder as they are.'
            elif state == 'merged':
                check += '\nWhere: merged into the main project folder' + (' (commit ' + str(merge['main_after'])[:10] + ')' if merge.get('main_after') else '') + '; read its files there.'
            elif task.get('patch_worktree') and state in ('working', 'waiting', 'conflict'):
                check += '\nWhere: not merged yet, files are in ' + str(task['patch_worktree'])
            elif state == 'failed':
                check += '\nWhere: the merge failed; these changes are NOT in the main folder.'
            parts.append(f"{label}{check}\n{result[:6000]}" +
                         ('\n[Full report available using the context tool.]' if len(result)>6000 else ''))
        text = '\n\n'.join(parts)
        return text[:60000] + ('\n[Further reports available through context.]' if len(text)>60000 else '')

    def _partial(self, task_id, lease, text):
        # Update callback of adapter.run(). Its payload is only the streamed answer text
        # (notion.stream_text keeps agent-inference/assistant_message steps), so tool steps
        # cannot be told apart: last_output_at is stored, last_tool_at is not.
        # At most one store write per task every TELEMETRY_SECONDS; the newest text waits in memory.
        now = time.time()
        tail = str(text or '')[-12000:]
        with self.lock:
            seen = self.__dict__.setdefault('_telemetry', {})
            mark = seen.get(task_id)
            if mark is None or mark['lease'] != lease:
                mark = seen[task_id] = dict(lease=lease, written=None, tail=None, output_at=None)
            if tail.strip() and tail != mark['tail']:
                mark['output_at'] = now
            mark['tail'] = tail
            if mark['written'] is not None and now - mark['written'] < TELEMETRY_SECONDS:
                return
            task = self.store.get('tasks', task_id)
            if not (task and task['lease'] == lease and task['state'] == 'running'):
                seen.pop(task_id, None)
                return
            task['partial'] = tail
            task['progress_at'] = now
            if mark['output_at'] is not None:
                task['last_output_at'] = mark['output_at']
            mark['written'] = now
            self.store.put('tasks', task)

    def _release(self, worker_id, lease, state='ready'):
        worker = self.store.get('workers', worker_id)
        if worker and worker.get('lease') == lease:
            worker.update(state=state, lease='')
            # A new failure schedules its own heal time; a clean release ends the backoff series.
            worker.pop('heal_at', None)
            if state == 'ready':
                worker.pop('heal_step', None)
            self.store.put('workers', worker)

    def _complete(self, future, info):
        task = self.store.get('tasks', info['task_id'])
        if not task or task.get('lease') != info['lease']:
            return  # An old report must not complete a newer attempt.
        run = self.store.get('runs', task['run_id'])
        text=''
        failure=None
        try:
            text = future.result()
            task.pop('remote_unknown',None)
            task['raw_response'] = text
            self._release(info['worker_id'], info['lease'])
            if run['state'] == 'cancelled':
                task['state'] = 'cancelled'
            else:
                data = plain_protocol.normalize(task,text) if getattr(self.adapter,'response_mode','json')=='plain' else json_object(text)
                if plain_protocol.provider_error(data.get('summary','')):
                    raise plain_protocol.ProviderReplyError('Notion returned a provider error instead of an answer')
                failure_line = plain_protocol.inference_error(data.get('summary', ''))
                if failure_line:
                    raise plain_protocol.InferenceErrorReply(failure_line)
                if task['role'] == 'planner':
                    validate_plan(data)
                    validate_allocation(data,run.get('allocation',{}))
                    if run.get('model_routing'):
                        validate_models(data)
                elif task['role'] == 'reviewer':
                    if type(data.get('passed')) is not bool or not isinstance(data.get('summary'), str):
                        raise ContractError('Review needs passed boolean and summary')
                    if not data['passed']:
                        validate_plan({'tasks':data.get('fixes')})
                        if run.get('model_routing'):
                            validate_models({'tasks':data['fixes']})
                elif data.get('status') not in ('done', 'blocked', 'failed') or not isinstance(data.get('summary'), str):
                    raise ContractError('Result needs status and summary')
                task['state'] = 'done' if task['role'] in ('planner', 'reviewer') else data['status']
                task['result'] = json.dumps(data, ensure_ascii=False)
                task['requires_review']=bool(data.get('requires_review'))
                task['error'] = data.get('summary', '') if task['state'] != 'done' else ''
            if task.get('paused_by') == LIMIT_PAUSED_BY:
                self._limit_charge_reply(task, info, text)
            task['finished'] = time.time()
            # Proof record: changed files in the write scope, reported tests, screenshot (proof.py, never raises).
            task['proof'] = task_proof.record(task, text, self._project_root(run)) if task.get('proof_base') else task_proof.record(dict(task, id=''), text, None)
            if task.get('patch_worktree') and task['state'] == 'done':
                task['patch_state'] = 'waiting'  # tick() starts collect -> verify -> merge
            self.store.event('task_finished', task['run_id'], task_id=task['id'], state=task['state'], message=task['title'],
                             complete_wait_s=info.get('complete_wait_s'))
        except ModelVerificationError as exc:
            failure=exc
            self._release(info['worker_id'],info['lease'])
            task.update(state='blocked',error=str(exc),finished=time.time(),
                        model_verification='unverified' if exc.reason=='missing' else 'mismatch',reported_model_ids=exc.reported)
            task.pop('remote_unknown',None)
            self.store.event('model_verification_failed',task['run_id'],task_id=task['id'],
                             expected_model_id=exc.expected,reported_model_ids=exc.reported,message=str(exc))
        except RateLimited as exc:
            failure=exc
            self._release(info['worker_id'], info['lease'])
            busy_over = False
            if getattr(exc, 'scope', 'account') == 'thread':
                # Only this chat was busy: no account cooldown, so the other chats keep working.
                task['busy_waits'] = task.get('busy_waits', 0) + 1
                busy_over = task['busy_waits'] > BUSY_WAIT_LIMIT
                if info['worker_id'] == (run or {}).get('main_worker') and not task.get('main_busy_at'):
                    # The Main is often the operator's own chat: Main tasks move to a free joined coworker.
                    self.store.event('main_busy_fallback', task['run_id'], task_id=task['id'], worker_id=info['worker_id'],
                                     message='The Main chat is busy; this Main task moves to a free coworker')
                if info['worker_id'] == (run or {}).get('main_worker'):
                    task['main_busy_at'] = time.time()
            else:
                account = self.store.get('accounts', self.store.get('workers', info['worker_id'])['account_id'])
                account['cooldown_until'] = time.time() + exc.seconds
                self.store.put('accounts', account)
            if exc.free:
                # Nothing was submitted: waiting for the chat does not use up an attempt.
                task['attempts'] = max(0, task['attempts'] - 1)
            if exc.confirmed:
                # Notion recorded this turn as failed without a reply; a later new turn is not a blind resend.
                task.pop('remote_unknown', None)
                info['verified_retry'] = True
            task.update(state=('cancelled' if exc.free and (run or {}).get('state') == 'cancelled' else
                               'blocked' if busy_over else
                               'retry' if exc.free or task['attempts']<self.store.settings()['max_attempts'] else 'blocked'),
                        next_at=time.time()+exc.seconds,
                        error=(('The chat stayed busy for %d checks; nothing was sent' % task['busy_waits']) if busy_over else
                               exc.message or 'Notion rate limit; account cooldown applied'))
            if task['state'] == 'retry' and not exc.free and self._attempt_side_effects(task):
                # A failed turn that already used tools or claimed files is not retried automatically.
                info.pop('verified_retry', None)
                self._hold_for_reconcile(task, info)
                task.update(state='cancelled' if (run or {}).get('state') == 'cancelled' else 'blocked',
                            error='The attempt used tools or claimed files before it failed; it will not be resent automatically')
        except TransientInference as exc:
            failure=exc
            self._release(info['worker_id'],info['lease'])
            task.update(state='retry' if task['attempts']<self.store.settings()['max_attempts'] else 'blocked',
                        next_at=time.time()+backoff(task['attempts'], base=1, cap=60),error=str(exc)[:300])
        except ContextCheckpointPending as exc:
            failure=exc
            self._release(info['worker_id'], info['lease'])
            task.update(state='cancelled' if run['state']=='cancelled' else 'retry',
                        attempts=max(0, task['attempts']-1), next_at=time.time()+10,
                        error='Waiting for a verified context checkpoint; assignment has not been submitted')
        except ResetNotAccepted as exc:
            failure=exc
            # Verified: Notion finished without committing the reset step. Continue once as a
            # normal message in the same chat; the checkpoint stays in front of the assignment.
            self._release(info['worker_id'], info['lease'])
            worker = self.store.get('workers', info['worker_id'])
            if worker:
                worker.update(context_reset=False, context_mode='soft', handoff_delivered=False,
                              context_fallback='Notion did not accept the context replacement; the checkpoint '
                                               'continues as a normal message in the same chat')
                if not worker.get('context_base_at'):
                    worker['context_base_at'] = max(0, task.get('sent_at', time.time()) - 1)
                self.store.put('workers', worker)
            task.pop('remote_unknown', None)
            task.update(state='cancelled' if run['state'] == 'cancelled' else 'retry', context_reset=False,
                        attempts=max(0, task['attempts'] - 1), next_at=time.time() + 5,
                        error='Context replacement was not accepted by Notion (verified); continuing in the same chat')
            self.store.event('context_reset_fallback', task['run_id'], task_id=task['id'], worker_id=info['worker_id'],
                             message='Context replacement not accepted; continuing in the same Welcome chat with the checkpoint')
            info['verified_fallback'] = True
        except (CircuitOpen, RemotePaused) as exc:
            failure=exc
            if task.get('submission_phase') == 'inference' and self._empty_remote_reply(task, info, run, exc):
                pass  # never true: a circuit/pause error carries no done_empty proof
            elif task.get('submission_phase') == 'inference':
                # Phase 3 / I5: the inference may already have left; never resend, reconcile instead.
                # The task limit only slows the checks down; it never blocks, resends or abandons.
                overdue = time.time()-task['started'] > task_limit(self.store.settings(), task)+120
                checks=task.get('reconcile_checks',0)+1
                task.update(state='uncertain', remote_unknown=False, reconcile_checks=checks,
                            reconcile_at=time.time()+(300 if overdue else max(min(60,5*2**min(checks-1,4)), getattr(exc,'seconds',0))),
                            error='Awaiting confirmation from Notion; request will not be resent')
                worker = self.store.get('workers', info['worker_id'])
                worker['state'] = 'recovering'
                self.store.put('workers', worker)
            elif isinstance(exc, CircuitOpen):
                # Refused locally before anything was sent: no attempt used, nothing to reconcile.
                self._release(info['worker_id'], info['lease'])
                task.update(state='cancelled' if run['state'] == 'cancelled' else 'retry',
                            attempts=max(0, task['attempts']-1), next_at=time.time()+exc.seconds, error=str(exc)[:300])
            else:
                # ACCOUNT_BLOCKED pause: nothing was sent; the task waits for a manual resume.
                self._release(info['worker_id'], info['lease'])
                task.update(state='cancelled' if run['state'] == 'cancelled' else 'blocked',
                            attempts=max(0, task['attempts']-1), paused_by='account_blocked', error=str(exc)[:400])
        except PendingRemote as exc:
            failure=exc
            task.update(turn_state=getattr(exc, 'state', '') or 'unknown', turn_state_reason=getattr(exc, 'reason', ''))
            if self._empty_remote_reply(task, info, run, exc):
                pass
            else:
                # The task limit only slows the checks down; it never blocks, resends or abandons (no time-based stop).
                now = time.time()
                settings = self.store.settings()
                overdue = now-task['started'] > task_limit(settings, task)+120
                checks=task.get('reconcile_checks',0)+1
                # crit_done: read the approval state before the delay is chosen; approval waits keep the old backoff.
                stored = self.store.get('tasks', task['id'], {}) or {}
                approval = bool(stored.get('awaiting_approval') or stored.get('approval_seen'))
                since = 0 if overdue else self._done_poll_since(task, info, now, settings)
                slot_free = bool(since) and not approval and self._done_poll_slot_free(task['id'], now, settings)
                delay = 300 if overdue else reconcile_delay(checks, now, since, approval, slot_free, settings)
                # B46: keep the liveness fields recover() just stored (this put would overwrite them otherwise).
                for key in REMOTE_LIVE_FIELDS:
                    if key in stored:
                        task[key] = stored[key]
                working = getattr(exc, 'reason', '') == 'remote_working' and not approval
                if working:
                    delay = min(delay, REMOTE_WORKING_POLL_SECONDS)  # near-live finish detection, also when overdue
                elif stored.get('remote_running') and stored.get('remote_progress_at') and not stored.get('remote_stall_noted'):
                    task['remote_stall_noted'] = True
                    self.store.event('remote_stalled', task['run_id'], task_id=task['id'], worker_id=info['worker_id'],
                                     message='The coworker chat shows no new step for %d min; nothing is resent'
                                             % (REMOTE_QUIET_MINUTES,))
                task.update(state='uncertain', remote_unknown=False, reconcile_checks=checks, reconcile_at=now+delay,
                            done_poll_since=since, done_poll_fast=slot_free, remote_working=working,
                            error=(working_text(stored, now) if working else
                                   'Awaiting confirmation from Notion; request will not be resent'))
                tool = stored.get('awaiting_approval') or stored.get('pending_tool')
                if tool:
                    # Honest label (H5): 'approval' only with explicit evidence in the transcript entry
                    # (notion.approval_evidence); otherwise the neutral 'tool running / not started' label.
                    evidence = bool(stored.get('approval_evidence') and stored.get('awaiting_approval'))
                    detail = stored.get('pending_tool_state') or ''
                    task.update(awaiting_approval=stored.get('awaiting_approval') or '', approval_evidence=evidence,
                                pending_tool=stored.get('pending_tool') or tool, pending_tool_state=detail,
                                approval_seen=stored.get('approval_seen'),
                                error=('Waiting for approval of ' + tool + ' in the coworker chat; the request is not resent'
                                       if evidence else
                                       'A tool call in the coworker chat has not finished yet (' + tool +
                                       (': ' + detail if detail else '') + '); the request is not resent'))
                worker = self.store.get('workers', info['worker_id'])
                worker['state'] = 'recovering'
                self.store.put('workers', worker)
        except thread_missing_type() as exc:
            failure=exc
            # HTTP 404 for the task's own Notion thread: blocked for good, never retried or resent. The worker
            # gets the existing 'missing' state (worker_gc.MISSING, chat_pool) and is disabled, so it is never picked again.
            worker = self.store.get('workers', info['worker_id']) or {}
            gone = not exc.thread_id or worker.get('thread_id') == exc.thread_id
            self._release(info['worker_id'], info['lease'], 'missing' if gone else 'error')
            worker = self.store.get('workers', info['worker_id'])
            if gone and worker and not worker.get('lease'):
                worker.update(state='missing', enabled=False)
                self.store.put('workers', worker)
            task.pop('remote_unknown', None)
            task.update(state='blocked', finished=time.time(),
                        error='The assigned Notion thread is missing; nothing was resent')
            self.store.event('worker_thread_missing', task['run_id'], task_id=task['id'], worker_id=info['worker_id'],
                             thread_id=exc.thread_id, message=str(exc)[:200])
        except plain_protocol.InferenceErrorReply as exc:
            # A Notion failure line instead of an answer is never done (live bug td7736426921).
            failure=exc
            self._release(info['worker_id'], info['lease'])
            line = exc.line[:300]
            self.store.event('notion_inference_error', task['run_id'], task_id=task['id'], worker_id=info['worker_id'],
                             message=line)
            error = 'Notion inference error: ' + line
            stopped = (run or {}).get('state') == 'cancelled'
            if self._attempt_writes(task):
                # I5: the attempt may have changed something; it is reconciled, never resent.
                self._hold_for_reconcile(task, info)
                task.update(state='cancelled' if stopped else 'blocked',
                            error=error + '; the attempt used write tools or claimed files, so it is reconciled, not resent')
            elif not stopped and not task.get('inference_retry') and task['attempts'] < self.store.settings()['max_attempts']:
                task.update(state='retry', inference_retry=True, next_at=self.clock()+backoff(task['attempts'], base=1, cap=60),
                            error=error + '; the attempt only read, so it is retried once')
            else:
                task.update(state='cancelled' if stopped else 'blocked', error=error)
        except ContractError as exc:
            failure=exc
            self._release(info['worker_id'], info['lease'])
            declined = plain_protocol.declined(task.get('raw_response',''))
            provider_failed=isinstance(exc,plain_protocol.ProviderReplyError)
            safe_provider_retry=(provider_failed and not task.get('files') and
                              getattr(text,'verified_terminal_reply',False) and not getattr(text,'tool_activity',True))
            task.update(state='retry' if not declined and (not provider_failed or safe_provider_retry) and task['attempts']<self.store.settings()['max_attempts'] else 'blocked',
                        next_at=self.clock()+(backoff(task['attempts'], base=1, cap=60) if provider_failed else 2), error=str(exc))
            if declined:
                task['error'] = 'Notion declined the request; see the saved response. No automatic repetition.'
            elif provider_failed:
                task['error']='Notion response temporarily failed; verified tool-free completion allows a bounded retry' if task['state']=='retry' else 'Notion returned an error instead of a result; completed contributions are retained. No format retry.'
            else:
                task['prompt'] += '\nPlease correct this problem in the response: '+str(exc)
        except Exception as exc:
            failure=exc
            if info.get('limit_stop') and (run or {}).get('state') != 'cancelled':
                # Token hard limit stopped this turn: never failed, never the presend failover, never resent.
                failure=None
                self._limit_hold(task, info, exc)
            elif cancelled_before_submission(exc):
                # Fix M: a cancel that won the race is not a chat failure; nothing was sent.
                failure=None
                self._release(info['worker_id'], info['lease'])
                task.update(state='cancelled' if (run or {}).get('state') == 'cancelled' else 'retry',
                            attempts=max(0, task['attempts']-1), next_at=time.time(), error='')
            elif (not task.get('sent_at') and task.get('submission_phase') == 'preparing'
                  and task.get('presend_retries', 0) < PRESEND_RETRY_LIMIT
                  and (run or {}).get('state') != 'cancelled'):
                # Live bug t531ad0a (Coworker 9): an Edge error before anything was sent blocked the whole run.
                # Nothing reached Notion, so the task moves to another coworker; this chat is marked 'error'.
                self._release(info['worker_id'], info['lease'], 'error')
                skip = [w for w in task.get('exclude_workers') or [] if w != info['worker_id']] + [info['worker_id']]
                task.update(state='retry', attempts=max(0, task['attempts'] - 1), next_at=time.time() + 2,
                            presend_retries=task.get('presend_retries', 0) + 1, exclude_workers=skip,
                            error='')
                self.store.event('presend_failover', task['run_id'], task_id=task['id'], worker_id=info['worker_id'],
                                 message='Nothing was sent; moved to another coworker: ' + str(exc)[:200])
            else:
                self._release(info['worker_id'], info['lease'], 'error')
                task.update(state='blocked', error=str(exc)[:400])
        if info.get('limit_stop') and task['state'] == 'uncertain':
            # Sent before the stop (PendingRemote / inference path): read back by reconcile, never resent (I5).
            task.update(paused_by=LIMIT_PAUSED_BY, limit_stopped_at=self.clock(),
                        error='Stopped locally by token limit; the Notion turn is read back, never resent')
        from .telemetry import classify
        error_class = classify(failure) or ('TASK_FAILED' if task['state'] in ('failed', 'blocked') else '')
        if error_class == 'ACCOUNT_BLOCKED' and task['state'] == 'blocked' and not task.get('remote_unknown'):
            # Phase 3: an account block pauses every remote call (no probes) until a manual resume.
            budget = self._budget()
            blocked_worker = self.store.get('workers', info['worker_id']) or {}
            if budget is not None:
                budget.pause(str(failure)[:300], account_id=blocked_worker.get('account_id', ''),
                             status=getattr(failure, 'status', 0) or 0)
            task['paused_by'] = 'account_blocked'
            if blocked_worker.get('state') == 'error' and not blocked_worker.get('lease'):
                blocked_worker['state'] = 'ready'  # the account was blocked, not this chat thread
                self.store.put('workers', blocked_worker)
        log = task.get('attempt_log') or []
        if log and (not log[-1].get('finished') or log[-1].get('state') == 'uncertain'):
            # Close this attempt; remote timestamps count only when written during it.
            entry, now = log[-1], time.time()
            begun = entry.get('started') or now
            stamp = lambda name: task.get(name) if (task.get(name) or 0) >= begun else 0
            entry.update(finished=now, duration=round(now - begun, 3), state=task['state'], error_class=error_class,
                         sent=stamp('sent_at'), first_output=stamp('first_output_at'), remote_done=stamp('remote_done_at'))
        self.store.put('tasks', task)
        self.store.event('task_attempt_finished',task['run_id'],task_id=task['id'],
                         worker_id=info['worker_id'],attempt=task['attempts'],state=task['state'],
                         error_class=error_class,duration=log[-1].get('duration') if log else None)
        parked = self.store.get('runs', task['run_id'])
        if (parked and parked['state'] == 'blocked' and (task['state'] == 'done' or info.get('verified_fallback') or info.get('verified_retry'))
                and not any(t['state'] in ('failed', 'blocked') or t.get('remote_unknown') for t in self.tasks(parked['id']))):
            # The run was only parked while this reply was unconfirmed; it continues where it stopped.
            parked.update(state=parked.get('blocked_stage', 'executing'), error='')
            self.store.put('runs', parked)
            self.store.event('stage_changed', parked['id'], message=parked['state'])

    def _heal_workers(self):
        # An error state is not permanent: an idle errored worker returns after a doubling backoff.
        now = self.clock()
        for worker in self.store.all('workers'):
            if worker.get('state') != 'error' or worker.get('lease'):
                continue  # a worker holding a lease is never touched
            step = int(worker.get('heal_step') or 0)
            delay = min(HEAL_CAP, HEAL_BASE * 2 ** min(step, 4))
            if not worker.get('heal_at'):
                worker.update(heal_at=now + delay, heal_backoff=delay)
                self.store.put('workers', worker)
            elif now >= worker['heal_at']:
                delay = worker.get('heal_backoff') or delay
                worker.update(state='ready', heal_step=step + 1)
                worker.pop('heal_at', None)
                self.store.put('workers', worker)
                self.store.event('worker_recovered', worker_id=worker['id'], backoff=delay,
                                 message='Worker returned to ready after %ds in error' % delay)

    def _reconcile(self):
        assigned = {v['task_id'] for v in self.inflight.values()}
        for task in self.store.all('tasks'):
            if (task['state'] != 'uncertain' and not task.get('remote_unknown')) or task['id'] in assigned or task.get('reconcile_at',0)>time.time():
                continue
            if len(self.inflight)>=40:
                break
            worker = self.store.get('workers', task['worker_id'])
            if getattr(self.adapter, 'single_account_harness', False):
                from .harness_account import selected_account
                if not worker or worker.get('account_id') != selected_account(self.store):
                    continue
            if not worker:
                task.update(state='blocked', error='The assigned Notion thread is missing')
                self.store.put('tasks', task)
                continue
            cancel = threading.Event()
            future = self.pool.submit(self.adapter.recover, worker, task, cancel)
            task['reconcile_at'] = time.time()+10
            self.store.put('tasks', task)
            self.inflight[future] = dict(task_id=task['id'], lease=task['lease'], worker_id=worker['id'],
                                        run_id=task['run_id'], cancel=cancel, recovery=True)
            self._watch(future, self.inflight[future])

    def _import_plan(self, run, plan, prefix=''):
        tasks = validate_plan(plan)
        ids = {t['id']:uid('t') for t in tasks}
        for task in tasks:
            self._resource_keys(run, task.get('files',[]))
        for task in tasks:
            self.add_task(run, 'executor', task['prompt'], prefix+task['title'],
                          deps=[ids[d] for d in task.get('depends_on',[])], files=task.get('files',[]),
                          acceptance=task['acceptance'], ident=ids[task['id']],specialty=str(task.get('role','executor'))[:60],
                          model=task['model'] if 'model' in task else _UNSET_MODEL, model_reason=task.get('model_reason', ''),
                          use_main=task.get('use_main', False), task_class=task.get('task_class', ''), risk=task.get('risk', ''))

    def _advance(self, run):
        tasks = self.tasks(run['id'])
        if not tasks:
            return
        if run.get('operator') and run.get('quality') in ('standard', 'high') and run['state'] in ('executing', 'fixing'):
            from .operator_roles import step  # review/fix stages; never a turn in the operator's chat
            if step(self, run, tasks):
                tasks = self.tasks(run['id'])
            if any(t['role'] == 'executor' and t['state'] == 'done' and not t.get('fixed_by')
                   and t.get('patch_worktree') and t.get('patch_state') in ('working', 'waiting') for t in tasks):
                return  # The review is queued once the merge settled; the run must not finish before.
        if any(t['state'] in ('queued','retry','running','uncertain','cancelling') for t in tasks):
            return
        problems = [t for t in tasks if t['state'] in ('failed','blocked')]
        if run['state']=='approach' and problems and any(t['state']=='done' for t in tasks):
            # Method suggestions are advisory. A declined suggestion is kept in
            # the audit trail, never repeated, and need not discard other useful
            # suggestions or prevent the main chat from planning the real work.
            for task in problems:
                task['state']='skipped'
                self.store.put('tasks',task)
            self.store.event('approach_fallback',run['id'],message='Planning continues with the available suggestions; declined requests were not repeated')
            problems=[]
        if problems:
            run.update(blocked_stage=run['state'], state='blocked', error=problems[0]['error'])
            self.store.put('runs', run)
            return
        try:
            state = run['state']
            if state == 'approach':
                run['state'] = 'planning'
                self.add_task(run, 'planner', 'Combine the independent approaches into a concrete execution plan.')
            elif state == 'planning':
                planner = next(t for t in reversed(tasks) if t['role'] == 'planner')
                plan = json.loads(planner['result'])
                self._import_plan(run, plan)
                run['worker_budget'] = max(1,min(run.get('worker_budget',max(1,run['capacity'])),parallel_width(plan['tasks'])))
                run.update(state='executing', summary=plan.get('summary',''))
                run['solo'] = len(plan['tasks']) == 1 and plan['tasks'][0].get('use_main', False)
            elif state in ('executing', 'fixing'):
                if run.get('operator'):
                    # Operator runs end here: the operator's own chat reviews and integrates the reports.
                    # A finished task is accepted only when its answer ends with a line RESULT: DONE.
                    from .operator_main import result_done
                    done = [t for t in tasks if t['state'] == 'done' and t['role'] == 'executor' and not t.get('fixed_by')]
                    unaccepted = [t for t in done if not result_done(t.get('result', '')) or t.get('agent_red_flag')]
                    run['unaccepted'] = [t['id'] for t in unaccepted]
                    summary = '%d delegated task(s) finished; %d accepted with RESULT: DONE' % (len(done), len(done) - len(unaccepted))
                    if unaccepted:
                        summary += '; not accepted: ' + ', '.join('%s (%s)' % (t['id'], t.get('title', '')) for t in unaccepted)
                    self._finish(run, tasks)
                    summary += self._outcome_text(run)  # plain counts: outcomes, fixes, reviews
                    if run.get('state') == 'awaiting_main':
                        # Nobody reads the reports yet: the run waits for the Main's confirmation.
                        run['summary'] = summary + '; waiting for the Main: ' + ', '.join(run.get('needs_main') or [])
                    else:
                        run['summary'] = summary + '; the operator reads the reports'
                    self.store.message(run['chat_id'], 'assistant', run['summary'], run['id'])
                elif run.get('solo'):
                    run['state'] = 'integrating'
                    self.add_task(run, 'integrator', 'Check your own completed work against every acceptance criterion and give the final answer, stating actual checks.')
                else:
                    run['state'] = 'reviewing'
                    self.add_task(run, 'reviewer', 'Check that all requested work and acceptance criteria are satisfied.')
            elif state == 'reviewing':
                review = json.loads(next(t for t in reversed(tasks) if t['role']=='reviewer')['result'])
                if review['passed']:
                    run['state'] = 'integrating'
                    self.add_task(run, 'integrator', 'Integrate the checked results into a complete, clear response for the user. State what was actually tested.',
                                  acceptance='Complete user answer grounded in the checked deliverables')
                elif run['fixes'] < self.store.settings()['max_fix_rounds']:
                    self._import_plan(run, {'tasks':review['fixes']}, prefix='Fix: ')
                    run.update(state='fixing', fixes=run['fixes']+1)
                else:
                    run.update(blocked_stage='reviewing', state='blocked', error='Verification still failed after the allowed repair rounds: '+review['summary'])
            elif state == 'integrating':
                result = json.loads(next(t for t in reversed(tasks) if t['role']=='integrator')['result'])
                run['summary'] = result['summary']
                self._finish(run, tasks)
                self.store.message(run['chat_id'], 'assistant', result['summary'], run['id'])
                memory_id = run.get('folder_id') or run['chat_id']
                self.store.remember(memory_id, result.get('context',''), run['id'])
            run['updated'] = time.time()
            self.store.put('runs', run)
            self.store.event('stage_changed', run['id'], message=run['state'])
        except (ValueError, ContractError, StopIteration) as exc:
            run.update(blocked_stage=run['state'], state='blocked', error=str(exc))
            self.store.put('runs', run)
