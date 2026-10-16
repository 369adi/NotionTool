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
from .allocation import allocation, parallel_width, validate_allocation
from .model_routing import validate_models
from .model_verification import ModelVerificationError
from .network_budget import CircuitOpen, NetworkBudget, RemotePaused, backoff, run_with_priority

ACTIVE = {'approach', 'planning', 'executing', 'reviewing', 'fixing', 'integrating'}
TERMINAL = {'done', 'failed', 'blocked', 'cancelled'}

def harness_enabled(chat, settings):
    override = str((chat or {}).get('harness', '')).lower()
    return override == 'on' if override in ('on', 'off') else bool(settings.get('harness_enabled', True))


class PendingRemote(Exception):
    """Upstream acceptance/completion is unknown. Never automatically resend."""


class ResetNotAccepted(Exception):
    """Notion verifiably did not commit a context-replacement turn; one normal resend is safe."""


class ContextCheckpointPending(Exception):
    """Only a checkpoint request may be pending; the actual assignment was not sent."""


class RateLimited(Exception):
    """confirmed: Notion recorded the turn as failed without a reply; free: nothing was submitted."""
    def __init__(self, seconds=30, message='', confirmed=False, free=False):
        super().__init__(message or 'Notion rate limit')
        self.seconds = seconds
        self.message = message
        self.confirmed = confirmed
        self.free = free


class TransientInference(Exception):
    pass


class Engine:
    def __init__(self, store, adapter):
        self.store, self.adapter = store, adapter
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=40, thread_name_prefix='notion-io')
        self.inflight = {}
        self.stop_event = threading.Event()
        self.round_robin = 0
        self.thread = None
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
        self.thread = threading.Thread(target=self._loop, daemon=True, name='coordinator')
        self.thread.start()

    def close(self):
        self.stop_event.set()
        with self.lock:
            for future, info in list(self.inflight.items()):
                info['cancel'].set()
        if self.thread:
            self.thread.join(2)
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self):
        while not self.stop_event.wait(0.25):
            try:
                self.tick()
            except Exception as exc:
                self.store.event('coordinator_error', message=type(exc).__name__)

    def tasks(self, run_id):
        return [t for t in self.store.all('tasks') if t['run_id'] == run_id]

    def add_task(self, run, role, prompt, title='', deps=(), files=(), acceptance='', ident=None, specialty='', model='opus', model_reason='', use_main=False):
        task = dict(id=ident or uid('t'), run_id=run['id'], role=role, title=title or role.title(),
                    prompt=prompt, deps=list(deps), files=list(files), acceptance=acceptance,
                    state='queued', attempts=0, created=time.time(), next_at=0, result='', error='',
                    worker_id='', lease='', step_id='', started=0, finished=0, specialty=specialty,
                    model=model, model_reason=model_reason, use_main=use_main)
        self.store.event('task_queued', run['id'], commit=False, task_id=task['id'], role=role, message=task['title'])
        self.store.put('tasks', task)  # also commits task_queued
        return task

    @staticmethod
    def _unquoted_memory(text, context, prior):
        """Return project memory without remembered final answers that the chat context already quotes."""
        for message in prior:
            answer = message['text']
            if message['role'] != 'assistant' or not answer.strip() or answer not in context:
                continue
            # The plain protocol remembers the first 6000 characters of a final answer.
            for quoted in {answer, answer[:6000]}:
                text = re.sub(r'(?:\A|(?<=\n))' + re.escape(quoted) + r'(?=\n|\Z)', '', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()

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
            main = self.store.get('workers', chat['main_worker'])
            if not main or not main.get('enabled'):
                raise ValueError('Choose an available Notion main chat in Accounts')
            if getattr(self.adapter, 'single_account_harness', False):
                from .harness_account import selected_account
                if not selected_account(self.store) or main['account_id'] != selected_account(self.store):
                    raise ValueError('Select this chat\'s account as the single harness account first')
            run_id = uid('r')
            completed={r['id'] for r in self.store.all('runs') if r['chat_id']==chat_id and r['state']=='done'}
            prior = [m for m in self.store.all('messages') if m['chat_id'] == chat_id and m.get('run_id') in completed
                     and not (m['role']=='assistant' and plain_protocol.provider_error(m['text']))][-10:]
            memory = self.store.get('memory', chat.get('folder_id') or chat_id, {})
            context = '\n'.join(f"{m['role']}: {m['text']}" for m in prior)[-18000:]
            # Final answers are also remembered; quote each one only once.
            findings = self._unquoted_memory(memory.get('text', ''), context, prior)
            if findings:
                context = (context + '\nProject memory:\n' + findings[-12000:]).lstrip('\n')
            # A chat queues its next run, while independent main chats share the pool.
            existing = any(r['chat_id'] == chat_id and r['state'] in ACTIVE | {'queued', 'paused'} for r in self.store.all('runs'))
            ready_accounts={a['id'] for a in self.store.all('accounts') if a.get('state')=='ready'}
            workers = [w for w in self.store.all('workers') if w.get('purpose') == 'worker' and w.get('enabled') and w['account_id'] in ready_accounts
                       and (not getattr(self.adapter, 'single_account_harness', False) or w['account_id'] == main['account_id'])]
            capacity = min(self.store.settings()['max_workers'], len(workers))
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

    def _approach(self, run):
        # Allocate only useful approach roles. Reserve the main chat for the final plan.
        policy = run.get('allocation', allocation(run['goal'], run['capacity']))
        if policy['mode'] == 'direct':
            run['state'] = 'integrating'
            self.store.put('runs', run)
            if policy.get('reason') == 'Conversational message' or not run.get('harness', True):
                self.add_task(run, 'integrator', 'Respond naturally to the conversational message.')
            else:
                self.add_task(run, 'integrator', 'Answer this focused question directly and completely yourself. '
                              'No coworker is assigned. State what you checked and what is an assumption.')
            return
        if run.get('model_routing'):
            run['state'] = 'planning'
            self.store.put('runs', run)
            self.add_task(run, 'planner', 'Choose whether to work alone or delegate, and choose each task model explicitly.')
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
            for info in self.inflight.values():
                if info['run_id'] == run_id:
                    info['cancel'].set()
            self.store.event('run_cancelled', run_id, message='Local work stopped; upstream chats are reconciled before reuse')

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

    def coordination(self):
        budget = self._budget()
        return budget.status() if budget is not None else dict(paused=False)

    def resume_remote(self):
        # Manual resume after ACCOUNT_BLOCKED: lift the pause and requeue only the tasks it stopped.
        with self.lock:
            budget = self._budget()
            resumed = budget.resume() if budget is not None else False
            requeued, runs = 0, set()
            for task in self.store.all('tasks'):
                if task.get('paused_by') and task['state'] == 'blocked' and not task.get('remote_unknown'):
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

    def retry_blocked(self, run_id):
        with self.lock:
            run = self.store.get('runs', run_id)
            if not run or run['state'] not in ('blocked', 'failed'):
                raise ValueError('Only a blocked or failed run can be resumed')
            if any(t['state'] == 'uncertain' or t.get('remote_unknown') for t in self.tasks(run_id)):
                raise ValueError('Wait for reconciliation before retrying')
            budget = self._budget()
            if budget is not None:
                budget.resume()  # Retry on a blocked run doubles as the manual resume after ACCOUNT_BLOCKED.
            run['state'] = run.get('blocked_stage', 'executing')
            run['error'] = ''
            for task in self.tasks(run_id):
                if task['state'] in ('blocked', 'failed'):
                    task.update(state='queued', attempts=0, error='', next_at=0)
                    task.pop('paused_by', None)
                    self.store.put('tasks', task)
            self.store.put('runs', run)

    def _resource_keys(self, run, files):
        if not files:
            return []
        root = Path(run.get('project_path') or self.store.directory / 'artifacts' / run['id']).resolve()
        result = []
        for path in files:
            resolved = (root / path).resolve()
            if not resolved.is_relative_to(root):
                raise ContractError('A planned file is outside the project folder')
            result.append(str(resolved).casefold())
        return result

    @staticmethod
    def _overlap(a, b):
        # Folder/file locks also protect overlapping nested directories.
        sep = '\\' if '\\' in a or '\\' in b else '/'
        return a == b or a.startswith(b.rstrip(sep) + sep) or b.startswith(a.rstrip(sep) + sep)

    def _conflicts(self, task, run):
        keys = self._resource_keys(run, task['files']) + task.get('absolute_resources',[])
        for other in self.store.all('tasks'):
            if other['id'] == task['id'] or (other['state'] not in ('running', 'cancelling', 'uncertain') and not other.get('remote_unknown')):
                continue
            other_run = self.store.get('runs', other['run_id'])
            for a in keys:
                if any(self._overlap(a,b) for b in self._resource_keys(other_run, other['files']) + other.get('absolute_resources',[])):
                    return True
        return False

    def claim_file(self, task_id, lease, relative):
        with self.lock:
            task = self.store.get('tasks', task_id)
            if not task or task['state'] != 'running' or task['lease'] != lease:
                raise ValueError('Assignment is no longer active')
            run = self.store.get('runs', task['run_id'])
            trial = dict(task, files=task['files'] + [relative])
            if self._conflicts(trial, run):
                raise ValueError('Another active task owns this file; request a dependency instead')
            task['files'] = trial['files']
            self.store.put('tasks', task)

    def claim_absolute(self, task_id, lease, path):
        """Serialize explicit full-PC writes using the same cross-chat ownership."""
        with self.lock:
            task=self.store.get('tasks',task_id)
            if not task or task['state']!='running' or task['lease']!=lease:
                raise ValueError('Assignment is no longer active')
            resource=str(Path(path).resolve()).casefold()
            trial=dict(task,absolute_resources=list(set(task.get('absolute_resources',[])+[resource])))
            if self._conflicts(trial,self.store.get('runs',task['run_id'])):
                raise ValueError('Another active task owns this PC path; request a dependency instead')
            self.store.put('tasks',trial)

    def tick(self):
        with self.lock:
            for future, info in list(self.inflight.items()):
                if future.done():
                    del self.inflight[future]
                    self._complete(future, info)
            self._reconcile()
            runs = self.store.all('runs')
            if getattr(self.adapter, 'single_account_harness', False):
                from .harness_account import selected_account, main_chat
                selected = selected_account(self.store)
                # Every Main of the selected harness account may run; each chat stays strictly ordered.
                runs = [r for r in runs if selected and self.store.get('workers', r.get('main_worker'), {}).get('account_id') == selected]
            for run in runs:
                if run['state'] == 'queued' and not any(
                    r['chat_id'] == run['chat_id'] and r['created'] < run['created'] and r['state'] in ACTIVE | {'paused', 'queued'} for r in runs):
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
                        if task['state'] not in ('queued', 'retry') or task['next_at'] > time.time():
                            continue
                        deps = [self.store.get('tasks', d) for d in task['deps']]
                        if any(not d or d['state'] in ('failed', 'blocked', 'cancelled') for d in deps):
                            task.update(state='blocked', error='A dependency failed')
                            self.store.put('tasks', task)
                            continue
                        if any(d['state'] != 'done' for d in deps) or self._conflicts(task, run):
                            continue
                        first_ready = not task.get('ready_at')
                        if first_ready:
                            # Committed with the next task write, so the dispatch path gets no extra disk sync.
                            task['ready_at'] = time.time()
                            self.store.event('task_ready', run['id'], commit=False, task_id=task['id'], deps=len(task['deps']))
                        worker = self._choose_worker(task, run, settings)
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
            from .harness_account import selected_account
            selected = selected_account(self.store)
            workers = [w for w in workers if w['account_id'] == selected]
        main_role = task['role'] in ('planner', 'integrator') or task.get('use_main', False)
        choices = [w for w in workers if w.get('enabled') and w['state'] == 'ready'
                   and w.get('cooldown_until', 0) <= time.time()
                   and (w['id'] == run['main_worker'] if main_role else w.get('purpose') == 'worker')]
        pending_rotations = {r['id']:r for r in self.store.all('rotations')}
        choices = [w for w in choices if not pending_rotations.get(w['id'], {}).get('task_id') or
                   pending_rotations[w['id']]['task_id'] == task['id'] or
                   self.store.get('tasks', pending_rotations[w['id']]['task_id'], {}).get('state') in TERMINAL]
        if not choices and not main_role and run['capacity'] == 0:
            choices = [w for w in workers if w['id'] == run['main_worker'] and w.get('enabled') and w['state'] == 'ready']
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
        if task['role'] == 'reviewer':
            executors = {t.get('worker_id') for t in self.tasks(run['id']) if t['role']=='executor'}
            independent = [w for w in choices if w['id'] not in executors]
            if independent:
                choices = independent
            else:
                choices=[]  # An executor must not review its own contribution.
                main=self.store.get('workers',run['main_worker'])
                if (main and main.get('enabled') and main['state']=='ready'
                    and accounts.get(main['account_id'],{}).get('state')=='ready'
                    and main.get('cooldown_until',0)<=time.time()
                    and accounts.get(main['account_id'],{}).get('cooldown_until',0)<=time.time()
                    and counts.get(main['account_id'],0)-int(main['id'] in checkpoint_workers)<self._parallel_cap(settings['per_account'])):
                    choices=[main]  # The planner did not execute any of the parts.
        choices = [w for w in choices if not pending_rotations.get(w['id'], {}).get('task_id') or
                   pending_rotations[w['id']]['task_id'] == task['id'] or
                   self.store.get('tasks', pending_rotations[w['id']]['task_id'], {}).get('state') in TERMINAL]
        choices.sort(key=lambda w: (w.get('last_used', 0), counts.get(w['account_id'], 0)))
        return choices[0] if choices else None

    def _dispatch(self, task, run, worker):
        account=self.store.get('accounts',worker['account_id'],{})
        if task['role']=='executor' and task['files'] and not account.get('bridge_connected'):
            task.update(state='blocked',error='Connect NotionTool PC tools before executing tasks that change project files')
            self.store.put('tasks',task)
            return
        task.update(state='running', worker_id=worker['id'], lease=secrets.token_urlsafe(32),
                    step_id=uid(), started=time.time(), attempts=task['attempts']+1, error='', partial='',
                    thread_id=worker['thread_id'], submission_phase='preparing')
        # Every attempt keeps its own timing record for the metrics.
        task['attempt_log'] = (task.get('attempt_log') or [])[-19:] + [
            dict(attempt=task['attempts'], worker_id=worker['id'], started=task['started'])]
        self.store.put('tasks', task)  # Commit the submission marker BEFORE the API can accept it.
        worker.update(state='busy', lease=task['lease'], last_used=time.time(),
                      prev_task_id=worker.get('last_task_id', ''), last_task_id=task['id'])
        self.store.event('lease_acquired', run['id'], commit=False, task_id=task['id'], worker_id=worker['id'], attempt=task['attempts'])
        self.store.put('workers', worker)  # also commits lease_acquired
        context_tasks = [self.store.get('tasks', dep) for dep in task['deps']]
        completed = [t for t in self.tasks(run['id']) if t['state']=='done']
        if task['role']=='planner':
            context_tasks = [t for t in completed if t['role'] in ('architect','researcher','quality_planner')]
        elif task['role']=='reviewer':
            # Checks use actual deliverables, not speculative approach answers.
            context_tasks = [t for t in completed if t['role']=='executor']
        elif task['role']=='integrator':
            reviews = [t for t in completed if t['role']=='reviewer']
            context_tasks = [t for t in completed if t['role']=='executor'] + reviews[-1:]
        context = self._context(context_tasks)
        account = self.store.get('accounts', worker['account_id'], {})
        make_prompt=plain_protocol.prompt if getattr(self.adapter,'response_mode','json')=='plain' else prompt_for
        prompt = make_prompt(task, run, context, task['lease'], bool(account.get('bridge_connected')))
        # Committed together with task_started right after the submit.
        self.store.event('context_built', run['id'], commit=False, task_id=task['id'], worker_id=worker['id'], attempt=task['attempts'],
                         context_chars=len(context), context_tasks=sum(1 for t in context_tasks if t), prompt_chars=len(prompt))
        cancel = threading.Event()
        future = self.pool.submit(run_with_priority, 'critical', self.adapter.run, worker, task, prompt, cancel,
                                  lambda text: self._partial(task['id'], task['lease'], text))
        self.inflight[future] = dict(task_id=task['id'], lease=task['lease'], worker_id=worker['id'],
                                    run_id=run['id'], cancel=cancel, recovery=False)
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
            parts.append(f"{label}{check}\n{result[:6000]}" +
                         ('\n[Full report available using the context tool.]' if len(result)>6000 else ''))
        text = '\n\n'.join(parts)
        return text[:60000] + ('\n[Further reports available through context.]' if len(text)>60000 else '')

    def _partial(self, task_id, lease, text):
        with self.lock:
            task = self.store.get('tasks', task_id)
            if task and task['lease'] == lease and task['state'] == 'running':
                task['partial'] = text[-12000:]
                task['progress_at'] = time.time()
                self.store.put('tasks', task)

    def _release(self, worker_id, lease, state='ready'):
        worker = self.store.get('workers', worker_id)
        if worker and worker.get('lease') == lease:
            worker.update(state=state, lease='')
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
            task['finished'] = time.time()
            self.store.event('task_finished', task['run_id'], task_id=task['id'], state=task['state'], message=task['title'])
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
                               'retry' if exc.free or task['attempts']<self.store.settings()['max_attempts'] else 'blocked'),
                        next_at=time.time()+exc.seconds, error=exc.message or 'Notion rate limit; account cooldown applied')
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
            if task.get('submission_phase') == 'inference':
                # Phase 3 / I5: the inference may already have left; never resend, reconcile instead.
                overdue = time.time()-task['started'] > self.store.settings()['task_timeout']+120
                checks=task.get('reconcile_checks',0)+1
                task.update(state='blocked' if overdue else 'uncertain', remote_unknown=overdue, reconcile_checks=checks,
                            reconcile_at=time.time()+(300 if overdue else max(min(60,5*2**min(checks-1,4)), getattr(exc,'seconds',0))),
                            error='Notion completion is unconfirmed; thread is reserved until reconciled' if overdue else 'Awaiting confirmation from Notion; request will not be resent')
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
            overdue = time.time()-task['started'] > self.store.settings()['task_timeout']+120
            checks=task.get('reconcile_checks',0)+1
            task.update(state='blocked' if overdue else 'uncertain', remote_unknown=overdue,
                        reconcile_checks=checks,reconcile_at=time.time()+(300 if overdue else min(60,5*2**min(checks-1,4))), error='Notion completion is unconfirmed; thread is reserved until reconciled' if overdue else 'Awaiting confirmation from Notion; request will not be resent')
            worker = self.store.get('workers', info['worker_id'])
            worker['state'] = 'recovering'
            self.store.put('workers', worker)
        except ContractError as exc:
            failure=exc
            self._release(info['worker_id'], info['lease'])
            declined = plain_protocol.declined(task.get('raw_response',''))
            provider_failed=isinstance(exc,plain_protocol.ProviderReplyError)
            safe_provider_retry=(provider_failed and not task.get('files') and
                              getattr(text,'verified_terminal_reply',False) and not getattr(text,'tool_activity',True))
            task.update(state='retry' if not declined and (not provider_failed or safe_provider_retry) and task['attempts']<self.store.settings()['max_attempts'] else 'blocked',
                        next_at=time.time()+(backoff(task['attempts'], base=1, cap=60) if provider_failed else 2), error=str(exc))
            if declined:
                task['error'] = 'Notion declined the request; see the saved response. No automatic repetition.'
            elif provider_failed:
                task['error']='Notion response temporarily failed; verified tool-free completion allows a bounded retry' if task['state']=='retry' else 'Notion returned an error instead of a result; completed contributions are retained. No format retry.'
            else:
                task['prompt'] += '\nPlease correct this problem in the response: '+str(exc)
        except Exception as exc:
            failure=exc
            self._release(info['worker_id'], info['lease'], 'error')
            task.update(state='blocked', error=str(exc)[:400])
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

    def _import_plan(self, run, plan, prefix=''):
        tasks = validate_plan(plan)
        ids = {t['id']:uid('t') for t in tasks}
        for task in tasks:
            self._resource_keys(run, task.get('files',[]))
        for task in tasks:
            self.add_task(run, 'executor', task['prompt'], prefix+task['title'],
                          deps=[ids[d] for d in task.get('depends_on',[])], files=task.get('files',[]),
                          acceptance=task['acceptance'], ident=ids[task['id']],specialty=str(task.get('role','executor'))[:60],
                          model=task.get('model', 'opus'), model_reason=task.get('model_reason', ''), use_main=task.get('use_main', False))

    def _advance(self, run):
        tasks = self.tasks(run['id'])
        if not tasks:
            return
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
                if run.get('solo'):
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
                run.update(state='done', summary=result['summary'])
                self.store.message(run['chat_id'], 'assistant', result['summary'], run['id'])
                memory_id = run.get('folder_id') or run['chat_id']
                self.store.remember(memory_id, result.get('context',''), run['id'])
            run['updated'] = time.time()
            self.store.put('runs', run)
            self.store.event('stage_changed', run['id'], message=run['state'])
        except (ValueError, ContractError, StopIteration) as exc:
            run.update(blocked_stage=run['state'], state='blocked', error=str(exc))
            self.store.put('runs', run)
