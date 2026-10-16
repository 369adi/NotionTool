"""Phase 3 patch: request coordination. Dry run by default; --apply writes with backups."""
import hashlib
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(r'C:\Users\liket\Downloads\NotionTool-harness\NotionTool')
BLOCK = ROOT / 'work' / 'phase3' / 'network_budget_block.py'
APPLY = '--apply' in sys.argv
EXPECTED = {
    'notiontool/network_budget.py': '26f7db1c',
    'notiontool/engine.py': 'e517fae8',
    'notiontool/edge_transport.py': '5775a556',
    'notiontool/notion.py': '7bed520f',
    'notiontool/telemetry.py': '515d272f',
    'notiontool/server.py': '08a37e33',
}


class Doc:
    def __init__(self, rel):
        self.rel, self.path = rel, ROOT / rel
        raw = self.path.read_bytes()
        self.sha = hashlib.sha256(raw).hexdigest()
        text = raw.decode('utf-8')
        self.crlf = '\r\n' in text
        if self.crlf and text.count('\n') != text.count('\r\n'):
            raise SystemExit(f'{rel}: mixed line endings, refusing to patch')
        self.raw, self.text, self.edits = raw, text.replace('\r\n', '\n'), 0
        want = EXPECTED.get(rel)
        if want and not self.sha.startswith(want):
            raise SystemExit(f'{rel}: changed since discovery (sha {self.sha[:8]}, expected {want})')

    def replace(self, old, new, count=1, label=''):
        found = self.text.count(old)
        if found != count:
            raise SystemExit(f'{self.rel}: anchor {label or old[:70]!r} found {found}x, expected {count}x')
        self.text = self.text.replace(old, new)
        self.edits += 1

    def insert_before(self, anchor, new, label=''):
        self.replace(anchor, new + anchor, 1, label)

    def insert_after(self, anchor, new, label=''):
        self.replace(anchor, anchor + new, 1, label)

    def before_line(self, pattern, make, label):
        found = list(re.finditer(pattern, self.text, re.M))
        if len(found) != 1:
            raise SystemExit(f'{self.rel}: line {label!r} found {len(found)}x, expected 1x')
        match = found[0]
        self.text = self.text[:match.start()] + make(match.group(1)) + self.text[match.start():]
        self.edits += 1

    def output(self):
        text = self.text.replace('\n', '\r\n') if self.crlf else self.text
        return text.encode('utf-8')


def method_span(text, header):
    if text.count(header) != 1:
        raise SystemExit(f'method {header!r} found {text.count(header)}x')
    i = text.index(header)
    j = text.find('\n    def ', i + 1)
    return i, (len(text) if j < 0 else j)


def patch_budget(doc):
    doc.insert_after('import collections\n', 'import contextlib\n')
    doc.insert_after('import email.utils\n', 'import itertools\nimport random\n')
    src = doc.text
    if src.count('class NetworkBudget:') != 1 or src.count('\n\nSHARED_BUDGET') != 1:
        raise SystemExit('network_budget.py: class or SHARED_BUDGET anchor missing')
    start, end = src.index('class NetworkBudget:'), src.index('\n\nSHARED_BUDGET')

    def method(name):
        i = src.index(f'    def {name}(', start)
        j = src.index('\n    def ', i + 1)
        return src[i:j].rstrip('\n') + '\n'

    block = BLOCK.read_text(encoding='utf-8').replace('\r\n', '\n')
    for marker, name in (('    #@TRIM@\n', '_trim'), ('    #@WINDOW_COUNT@\n', 'window_count')):
        if block.count(marker) != 1:
            raise SystemExit(f'block marker {marker!r} missing')
        block = block.replace(marker, method(name))
    doc.text = src[:start] + block.rstrip('\n') + '\n' + src[end:]
    doc.edits += 1


ENGINE_EXCEPT = """        except (CircuitOpen, RemotePaused) as exc:
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
"""

ENGINE_CLASSIFY = "        error_class = classify(failure) or ('TASK_FAILED' if task['state'] in ('failed', 'blocked') else '')\n"

ENGINE_PAUSE = """        if error_class == 'ACCOUNT_BLOCKED' and task['state'] == 'blocked' and not task.get('remote_unknown'):
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
"""

ENGINE_RETRY_RESUME = """            budget = self._budget()
            if budget is not None:
                budget.resume()  # Retry on a blocked run doubles as the manual resume after ACCOUNT_BLOCKED.
"""

ENGINE_METHODS = """    def _budget(self):
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

"""


def patch_engine(doc):
    doc.insert_after('from .model_verification import ModelVerificationError\n',
                     'from .network_budget import CircuitOpen, NetworkBudget, RemotePaused, backoff, run_with_priority\n')
    doc.replace("2**task['attempts']", "backoff(task['attempts'], base=1, cap=60)", 2)
    doc.insert_before('        except PendingRemote as exc:\n', ENGINE_EXCEPT)
    doc.insert_after(ENGINE_CLASSIFY, ENGINE_PAUSE, 'classify line')
    doc.replace("settings['per_account']", "self._parallel_cap(settings['per_account'])", 2)
    doc.replace('self.pool.submit(self.adapter.run,', "self.pool.submit(run_with_priority, 'critical', self.adapter.run,")
    doc.insert_after("                raise ValueError('Wait for reconciliation before retrying')\n", ENGINE_RETRY_RESUME)
    doc.insert_after("                    task.update(state='queued', attempts=0, error='', next_at=0)\n",
                     "                    task.pop('paused_by', None)\n")
    doc.insert_before('    def retry_blocked(self, run_id):\n', ENGINE_METHODS)


def patch_edge(doc):
    doc.replace('        self.budget.acquire(cancel)\n', '        ticket = self.budget.acquire(cancel, source=path)\n')
    doc.replace('        return target, key\n', '        return target, key, ticket\n')
    doc.replace(
        '        target, key = self._start(path, body, headers, timeout, cancel)\n'
        '        deadline = time.monotonic() + timeout + 5\n',
        '        target, key, ticket = self._start(path, body, headers, timeout, cancel)\n'
        '        deadline = time.monotonic() + timeout + 5\n'
        '        # Phase 3: every started request reports its outcome to the shared coordinator.\n'
        "        begun, status, latency, outcome = time.monotonic(), 0, None, 'neutral'\n")
    doc.replace(
        '                if not result:\n'
        "                    raise NotionStreamError('Edge request state lost; reconcile before reuse')\n"
        "                if result.get('status') == 429:\n",
        '                if not result:\n'
        "                    outcome = 'failed'\n"
        "                    raise NotionStreamError('Edge request state lost; reconcile before reuse')\n"
        "                if result.get('status') and latency is None:\n"
        "                    status, latency = result['status'], time.monotonic() - begun\n"
        "                if result.get('status') == 429:\n"
        "                    outcome = 'rate_limited'\n")
    doc.replace(
        "                if result.get('status', 0) >= 400:\n"
        "                    raise NotionHttpError(result['status'])\n"
        "                if result.get('error'):\n",
        "                if result.get('status', 0) >= 400:\n"
        "                    code = result['status']\n"
        "                    outcome = 'blocked' if code in (401, 403) else 'failed' if code >= 500 else 'invalid'\n"
        '                    raise NotionHttpError(code)\n'
        "                if result.get('error'):\n"
        "                    outcome = 'failed'\n")
    doc.replace(
        "                if result.get('done') and not result.get('remaining'):\n"
        '                    self._save_cookies(target)\n'
        '                    return\n'
        '                time.sleep(.1)\n'
        "            raise NotionStreamError('Edge request timed out; reconcile before reuse')\n"
        '        finally:\n'
        '            try:\n',
        "                if result.get('done') and not result.get('remaining'):\n"
        "                    outcome = 'ok'\n"
        '                    self._save_cookies(target)\n'
        '                    return\n'
        '                time.sleep(.1)\n'
        "            outcome = 'failed'\n"
        "            raise NotionStreamError('Edge request timed out; reconcile before reuse')\n"
        '        finally:\n'
        "            release = getattr(self.budget, 'release', None)\n"
        '            if release is not None:\n'
        '                release(ticket, outcome, status=status, latency=latency, account_id=self.account_id)\n'
        '            try:\n')


def patch_notion(doc):
    doc.replace('from .network_budget import NetworkBudget\n', 'from .network_budget import NetworkBudget, request_priority\n')
    i, j = method_span(doc.text, '    def recover(')
    body = doc.text[i:j]
    for pattern, label in ((r"^( +)(transcript = self\.client\(worker\['account_id'\]\)\.transcript\(.*\))\n", 'recover transcript'),
                           (r"^( +)(.*self\.verify_response_model\(worker, ?task\).*)\n", 'recover verify')):
        found = list(re.finditer(pattern, body, re.M))
        if len(found) != 1:
            raise SystemExit(f'notion.py: {label} found {len(found)}x, expected 1x')
        match = found[0]
        line = match.group(2)
        if line.rstrip().endswith(('(', ',', '\\')):
            raise SystemExit(f'notion.py: {label} is not a single-line statement: {line!r}')
        indent = match.group(1)
        body = body[:match.start()] + f"{indent}with request_priority('verify'):\n{indent}    {line}\n" + body[match.end():]
        doc.edits += 1
    doc.text = doc.text[:i] + body + doc.text[j:]


def patch_telemetry(doc):
    found = list(re.finditer(r"^( +)'ContractError': 'TASK_FAILED',\n", doc.text, re.M))
    if len(found) != 1:
        raise SystemExit(f'telemetry.py: ContractError mapping found {len(found)}x')
    indent, end = found[0].group(1), found[0].end()
    doc.text = doc.text[:end] + f"{indent}'CircuitOpen': 'TRANSIENT',\n{indent}'RemotePaused': 'ACCOUNT_BLOCKED',\n" + doc.text[end:]
    doc.edits += 1


def patch_server(doc):
    doc.before_line(r"^( +)if method=='GET' and path=='/api/report':\n",
                    lambda ind: f"{ind}if method=='GET' and path=='/api/coordination':\n"
                                f"{ind}    return self.response(200,app.engine.coordination())\n", 'GET /api/report')
    doc.before_line(r"^( +)if path=='/api/run/retry':\n",
                    lambda ind: f"{ind}if path=='/api/coordination/resume':\n"
                                f"{ind}    return self.engine.resume_remote()\n", 'POST /api/run/retry')
    doc.replace("if cooling else 'No active rate limit observed.'),",
                "if cooling else 'No active rate limit observed.') if not paused else paused,")
    doc.before_line(r"^( +)return dict\(confirmed='No published concurrency",
                    lambda ind: f"{ind}coordination = self.engine.coordination()\n"
                                f"{ind}pause = coordination.get('pause') or {{}}\n"
                                f"{ind}paused = ('Remote calls paused: Notion account blocked (' + str(pause.get('reason') or 'sign-in required')\n"
                                f"{ind}          + '). Sign in again, then use Retry on the blocked run to resume.') if coordination.get('paused') else ''\n",
                    'limits return')


def patch_allocation_test(doc):
    doc.replace('NetworkBudget(clock=lambda:self.now,sleep=self.sleep)', 'NetworkBudget(clock=lambda:self.now,sleep=self.sleep,rng=lambda:0)')


PLAN = [
    ('notiontool/network_budget.py', patch_budget),
    ('notiontool/engine.py', patch_engine),
    ('notiontool/edge_transport.py', patch_edge),
    ('notiontool/notion.py', patch_notion),
    ('notiontool/telemetry.py', patch_telemetry),
    ('notiontool/server.py', patch_server),
    ('tests/test_allocation.py', patch_allocation_test),
]


def main():
    docs = []
    for rel, patch in PLAN:
        doc = Doc(rel)
        patch(doc)
        compile(doc.text, rel, 'exec')
        docs.append(doc)
        print(f'{rel}: ok, {doc.edits} edits, {"CRLF" if doc.crlf else "LF"}, sha before {doc.sha[:12]}')
    if not APPLY:
        print('DRY RUN OK - nothing written')
        return
    backup = ROOT / 'work' / 'backups' / ('phase3-' + time.strftime('%Y%m%d-%H%M%S'))
    for doc in docs:
        target = backup / doc.rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(doc.raw)
    for doc in docs:
        data = doc.output()
        doc.path.write_bytes(data)
        print(f'{doc.rel}: written, sha after {hashlib.sha256(data).hexdigest()}')
    print(f'APPLIED - backups in {backup}')


if __name__ == '__main__':
    main()
