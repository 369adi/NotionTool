# Phase 2 patch: exact single-occurrence replacements; all-or-nothing; backups first.
import hashlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LF, CRLF = chr(10), chr(13) + chr(10)
P = {}


def add(path, old, new):
    P.setdefault(path, []).append((old, new))


# ---------------- store.py ----------------
ST = 'notiontool/store.py'
add(ST, r'''        self.db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
        self.db.commit()
''', r'''        self.db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
        # Daily counts per kind of events removed by the 2000-event retention window.
        self.db.execute('CREATE TABLE IF NOT EXISTS event_rollup(day TEXT, kind TEXT, total INTEGER NOT NULL, PRIMARY KEY(day,kind))')
        self.db.execute("CREATE INDEX IF NOT EXISTS event_task ON events(json_extract(doc, '$.task_id'))")
        self.db.commit()
        self.projects = {}
''')
add(ST, r'''    def event(self, kind, run_id='', **fields):
        fields = redact(fields)
        with self.lock:
            self.db.execute('INSERT INTO events(ts,run_id,kind,doc) VALUES(?,?,?,?)',
                            (time.time(), run_id, kind, json.dumps(fields, ensure_ascii=False)))
            self.db.execute('DELETE FROM events WHERE seq < (SELECT COALESCE(MAX(seq),0)-2000 FROM events)')
            self.db.commit()
''', r'''    def event(self, kind, run_id='', **fields):
        from .telemetry import EVENT_SCHEMA, IDENTIFIERS
        fields = redact(fields)
        for name in IDENTIFIERS:
            fields.setdefault(name, '')
        if run_id and not fields['project_id']:
            fields['project_id'] = self.project_of(run_id)
        fields['v'] = EVENT_SCHEMA
        with self.lock:
            seq = self.db.execute('INSERT INTO events(ts,run_id,kind,doc) VALUES(?,?,?,?)',
                                  (time.time(), run_id, kind, json.dumps(fields, ensure_ascii=False))).lastrowid
            cutoff = seq - 2000
            if self.db.execute('SELECT 1 FROM events WHERE seq < ? LIMIT 1', (cutoff,)).fetchone():
                # Count the oldest events per day and kind before retention removes them.
                self.db.execute("INSERT INTO event_rollup(day,kind,total) SELECT date(ts,'unixepoch','localtime'),kind,COUNT(*) "
                                "FROM events WHERE seq < ? GROUP BY 1,2 "
                                "ON CONFLICT(day,kind) DO UPDATE SET total=total+excluded.total", (cutoff,))
                self.db.execute('DELETE FROM events WHERE seq < ?', (cutoff,))
            self.db.commit()
        return seq

    def project_of(self, run_id):
        # Project of a run for event attribution; the chat folder until projects exist.
        projects = self.__dict__.setdefault('projects', {})
        if run_id not in projects:
            run = self.get('runs', run_id)
            if not run:
                return ''
            projects[run_id] = run.get('project_id') or run.get('folder_id') or ''
        return projects[run_id]
''')

# ---------------- engine.py ----------------
EN = 'notiontool/engine.py'
add(EN, r'''                    model=model, model_reason=model_reason, use_main=use_main)
        self.store.put('tasks', task)
        return task
''', r'''                    model=model, model_reason=model_reason, use_main=use_main)
        self.store.put('tasks', task)
        self.store.event('task_queued', run['id'], task_id=task['id'], role=role, message=task['title'])
        return task
''')
add(EN, r'''                        if any(d['state'] != 'done' for d in deps) or self._conflicts(task, run):
                            continue
''', r'''                        if any(d['state'] != 'done' for d in deps) or self._conflicts(task, run):
                            continue
                        if not task.get('ready_at'):
                            task['ready_at'] = time.time()
                            self.store.put('tasks', task)
                            self.store.event('task_ready', run['id'], task_id=task['id'], deps=len(task['deps']))
''')
add(EN, r'''                    thread_id=worker['thread_id'], submission_phase='preparing')
        self.store.put('tasks', task)  # Commit the submission marker BEFORE the API can accept it.
''', r'''                    thread_id=worker['thread_id'], submission_phase='preparing')
        # Every attempt keeps its own timing record for the metrics.
        task['attempt_log'] = (task.get('attempt_log') or [])[-19:] + [
            dict(attempt=task['attempts'], worker_id=worker['id'], started=task['started'])]
        self.store.put('tasks', task)  # Commit the submission marker BEFORE the API can accept it.
''')
add(EN, r'''        self.store.put('workers', worker)
        context_tasks = [self.store.get('tasks', dep) for dep in task['deps']]
''', r'''        self.store.put('workers', worker)
        self.store.event('lease_acquired', run['id'], task_id=task['id'], worker_id=worker['id'], attempt=task['attempts'])
        context_tasks = [self.store.get('tasks', dep) for dep in task['deps']]
''')
add(EN, r'''        prompt = make_prompt(task, run, context, task['lease'], bool(account.get('bridge_connected')))
''', r'''        prompt = make_prompt(task, run, context, task['lease'], bool(account.get('bridge_connected')))
        self.store.event('context_built', run['id'], task_id=task['id'], worker_id=worker['id'], attempt=task['attempts'],
                         context_chars=len(context), context_tasks=sum(1 for t in context_tasks if t), prompt_chars=len(prompt))
''')
add(EN, r'''        text=''
        try:
            text = future.result()
''', r'''        text=''
        failure=None
        try:
            text = future.result()
''')


def catch(name, bound, rest):
    head = '        except ' + name + (' as exc:' if bound else ':') + LF
    add(EN, head + rest, '        except ' + name + ' as exc:' + LF + '            failure=exc' + LF + rest)


catch('ModelVerificationError', True, r'''            self._release(info['worker_id'],info['lease'])
            task.update(state='blocked',error=str(exc),finished=time.time(),
''')
catch('RateLimited', True, r'''            self._release(info['worker_id'], info['lease'])
            account = self.store.get('accounts', self.store.get('workers', info['worker_id'])['account_id'])
''')
catch('TransientInference', True, r'''            self._release(info['worker_id'],info['lease'])
            task.update(state='retry' if task['attempts']<self.store.settings()['max_attempts'] else 'blocked',
''')
catch('ContextCheckpointPending', False, r'''            self._release(info['worker_id'], info['lease'])
            task.update(state='cancelled' if run['state']=='cancelled' else 'retry',
''')
catch('ResetNotAccepted', False, r'''            # Verified: Notion finished without committing the reset step. Continue once as a
''')
catch('PendingRemote', False, r'''            overdue = time.time()-task['started'] > self.store.settings()['task_timeout']+120
''')
catch('ContractError', True, r'''            self._release(info['worker_id'], info['lease'])
            declined = plain_protocol.declined(task.get('raw_response',''))
''')
catch('Exception', True, r'''            self._release(info['worker_id'], info['lease'], 'error')
            task.update(state='blocked', error=str(exc)[:400])
''')
add(EN, r'''        self.store.put('tasks', task)
        self.store.event('task_attempt_finished',task['run_id'],task_id=task['id'],
                         worker_id=info['worker_id'],attempt=task['attempts'],state=task['state'])
''', r'''        from .telemetry import classify
        error_class = classify(failure) or ('TASK_FAILED' if task['state'] in ('failed', 'blocked') else '')
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
''')

# ---------------- notion.py ----------------
NO = 'notiontool/notion.py'
add(NO, r'''from .model_verification import verify_execution,ModelVerificationError,turn_status
''', r'''from .model_verification import verify_execution,ModelVerificationError,turn_status
from .telemetry import http_class
''')
add(NO, r'''class VerifiedReply(str):
''', r'''MODEL_CATALOG_TTL = 600  # seconds; a cached model catalog is read again after this or on a miss


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
''')
add(NO, r'''        label = MODELS[choice(selected)]
        models = self.call('getAvailableModels',{'spaceId':self.info['space_id'],'surface':'workspace_model_settings'}).get('models',[])
        model = next((m for m in models if m.get('modelProvider')=='anthropic' and not m.get('isDisabled')
                      and re.sub(r'\s+',' ',m.get('modelMessage','')).strip().lower()==label.lower()), None)
        if not model or not model.get('model'):
''', r'''        label = MODELS[choice(selected)]
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
''')
add(NO, r'''        self.network_budget = NetworkBudget(store)
''', r'''        self.network_budget = NetworkBudget(store)
        self.confirmed_configs = {}  # thread_id -> (thread_id, generation, model) after a confirmed config check
''')
add(NO, r'''        model = client.model(selected)
        self.ensure_thread_model(client, worker['thread_id'], model)
''', r'''        model = client.model(selected)
        # The saved chat config changes only through our own confirmed write: check once per chat generation.
        config_key = (worker['thread_id'], worker.get('generation', 0), model)
        if getattr(self, 'confirmed_configs', {}).get(worker['thread_id']) != config_key:
            self.ensure_thread_model(client, worker['thread_id'], model)
            self.__dict__.setdefault('confirmed_configs', {})[worker['thread_id']] = config_key
''')
add(NO, r'''        steps = []
        started,last_update = time.monotonic(),0
        try:
            for line in client.transport.stream('runInferenceTranscript',body,client.headers(),timeout=60,cancel=cancel):
                if cancel.is_set() or time.monotonic()-started>self.store.settings()['task_timeout']:
                    raise PendingRemote()
                event = json.loads(line)
                if event.get('type') in ('patch-start','patch-sync'):
                    steps = (event.get('data') or {}).get('s') or steps
                elif event.get('type')=='patch':
                    steps = apply_patch(steps,event.get('v'))
                if time.monotonic()-last_update>0.6:
                    update(stream_text(steps))
                    last_update = time.monotonic()
            errors = [s for s in steps if isinstance(s,dict) and s.get('type')=='error']
            text = stream_text(steps)
            # Streaming steps can contain stale text or provider placeholders.
            # Only the committed answer to our own turn can finish an assignment.
            return self.recover(worker,task,cancel)
        except NotionHttpError as exc:
            if exc.status==429:
                raise RateLimited(exc.retry_after) from None
            raise ValueError(f'Notion inference: HTTP {exc.status}') from None
        except (OSError,TimeoutError,json.JSONDecodeError):
            raise PendingRemote() from None

    def recover(self, worker, task, cancel):
''', r'''        steps = []
        started,last_update = time.monotonic(),0
        trace = dict(task_id=task['id'], worker_id=worker.get('id', ''), attempt=task.get('attempts', 0))
        run_id = task.get('run_id', '')
        self.store.event('remote_sent', run_id, prompt_chars=len(prompt), **trace)
        seen_output, lines, reported = False, 0, []
        def remote_done(outcome, error_class=''):
            if not reported:
                reported.append(outcome)
                self.store.event('remote_done', run_id, outcome=outcome, error_class=error_class, lines=lines,
                                 seconds=round(time.monotonic()-started, 3), **trace)
        try:
            for line in client.transport.stream('runInferenceTranscript',body,client.headers(),timeout=60,cancel=cancel):
                if cancel.is_set() or time.monotonic()-started>self.store.settings()['task_timeout']:
                    raise PendingRemote()
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
            errors = [s for s in steps if isinstance(s,dict) and s.get('type')=='error']
            text = stream_text(steps)
            self.mark_task(task['id'], remote_done_at=time.time())
            remote_done('stream_end')
            # Streaming steps can contain stale text or provider placeholders.
            # Only the committed answer to our own turn can finish an assignment.
            return self.recover(worker,task,cancel)
        except NotionHttpError as exc:
            remote_done('http_'+str(exc.status), http_class(exc.status))
            if exc.status==429:
                raise RateLimited(exc.retry_after) from None
            raise ValueError(f'Notion inference: HTTP {exc.status}') from None
        except (OSError,TimeoutError,json.JSONDecodeError):
            remote_done('unknown', 'REMOTE_UNKNOWN')
            raise PendingRemote() from None
        except PendingRemote:
            remote_done('unconfirmed', 'REMOTE_UNKNOWN')
            raise

    def mark_task(self, task_id, **fields):
        # Persist telemetry timestamps on the task without touching its state.
        with self.store.lock:
            saved = self.store.get('tasks', task_id)
            if saved:
                saved.update(fields)
                self.store.put('tasks', saved)

    def recover(self, worker, task, cancel):
''')
add(NO, r'''            ids = thread.get('messages', [])
            messages = {}
            for start in range(0, len(ids), 100):
                messages.update(records('thread_message', ids[start:start + 100]))
            steps = [dict((messages[ident] or {}).get('step', {})) for ident in ids if ident in messages]
''', r'''            steps, _ = own_turn_steps(records, thread.get('messages', []), task.get('step_id'))
''')
add(NO, r'''    def verify_response_model(self,worker,task):
        client=self.client(worker['account_id'])
''', r'''    def verify_response_model(self,worker,task):
        # Check the committed own turn, framed by verify_started / verify_done events.
        trace=dict(task_id=task['id'],worker_id=worker.get('id',''),attempt=task.get('attempts',0))
        began=time.monotonic();outcome='error'
        self.store.event('verify_started',task.get('run_id',''),**trace)
        try:
            result=self.check_response_model(worker,task)
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

    def check_response_model(self,worker,task):
        client=self.client(worker['account_id'])
''')
add(NO, r'''            ids=thread.get('messages',[])
            messages={}
            for start in range(0,len(ids),100):messages.update(records('thread_message',ids[start:start+100]))
            steps=[dict(messages[ident].get('step',{})) for ident in ids if ident in messages]
''', r'''            steps,read=own_turn_steps(records,thread.get('messages',[]),task.get('step_id'))
            self.mark_task(task['id'],verify_messages_read=read)
''')
add(NO, r'''                         trace_id=error.get('traceId', ''), sub_type=sub_type, message=message)
''', r'''                         trace_id=error.get('traceId', ''), sub_type=sub_type, error_class='TRANSIENT', message=message)
''')

# ---------------- server.py (CRLF, converted automatically) ----------------
SV = 'notiontool/server.py'
add(SV, r'''                            self.store.put('workers', fresh)
            except Exception:
                self.store.event('context_scan_waiting', message='Native context scan deferred; check the selected account and Edge tab')
''', r'''                            self.store.put('workers', fresh)
                if getattr(self, 'context_scan_blocked', False):
                    self.context_scan_blocked = False
                    self.store.event('context_scan_resumed', message='Native context scan works again')
            except Exception as exc:
                # Record the waiting state once per change instead of on every pass.
                if not getattr(self, 'context_scan_blocked', False):
                    self.context_scan_blocked = True
                    from .telemetry import classify
                    self.store.event('context_scan_waiting', error=type(exc).__name__, error_class=classify(exc),
                                     message='Native context scan deferred; check the selected account and Edge tab')
''')
add(SV, r'''                        if method=='GET' and path=='/api/report':
''', r'''                        if method=='GET' and path=='/api/metrics':
                            from .telemetry import task_metrics
                            return self.response(200,{'tasks':task_metrics(app.store,query.get('run_id',[''])[0] or None)})
                        if method=='GET' and path=='/api/report':
''')

# ---------------- network_budget.py ----------------
NB = 'notiontool/network_budget.py'
add(NB, r'''            self.interval = min(2, self.interval * 2)
''', r'''            before = self.interval
            self.interval = min(2, self.interval * 2)
''')
add(NB, r'''                self.store.event('network_rate_limited', message='All slots paused for the shared Notion cooldown', seconds=delay)
''', r'''                self.store.event('network_rate_limited', message='All slots paused for the shared Notion cooldown', seconds=delay)
                self.store.event('request_coordination_adjusted', message='Request start interval adjusted after a rate limit',
                                 interval_before=before, interval=self.interval, paused_seconds=delay)
''')

# ---------------- apply ----------------
errors, results = [], {}
for rel, pairs in P.items():
    raw = (ROOT / rel).read_bytes()
    text = raw.decode('utf-8')
    crlf = CRLF in text
    for index, (old, new) in enumerate(pairs):
        if crlf:
            old, new = old.replace(LF, CRLF), new.replace(LF, CRLF)
        found = text.count(old)
        if found != 1:
            errors.append('%s #%d: %dx %r' % (rel, index, found, old[:90]))
            continue
        text = text.replace(old, new, 1)
    try:
        compile(text, rel, 'exec')
    except SyntaxError as exc:
        errors.append('%s: syntax %s' % (rel, exc))
    results[rel] = (raw, text)
if errors or '--check' in sys.argv:
    print('NOTHING WRITTEN' if errors else 'CHECK OK, nothing written')
    print(LF.join(errors))
    sys.exit(1 if errors else 0)
backup = ROOT / 'work' / 'backups' / ('phase2-' + time.strftime('%Y%m%d-%H%M%S'))
backup.mkdir(parents=True, exist_ok=True)
for rel, (raw, text) in results.items():
    (backup / rel.replace('/', '_')).write_bytes(raw)
for rel, (raw, text) in results.items():
    (ROOT / rel).write_bytes(text.encode('utf-8'))
    print(rel, len(P[rel]), 'edits', hashlib.sha256(text.encode('utf-8')).hexdigest())
print('backup', backup)
