"""Compact MCP facade inspired by SecretScript's lazy tool discovery.

Assignment capabilities replace join/session guessing. By default file access is
confined to the selected project; writes inside the project claim a coordinator
lock before touching the file. Two explicit opt-in settings widen the reach:

* ``mcp_full_access``    - absolute paths anywhere on the PC, shell in any folder
                           (home folder when the chat has no project), longer timeouts.
* ``mcp_browser_access`` - browser tools (Edge/Chrome via DevTools protocol, own profile).

Planning/review roles always stay read-only. Every write, shell command and browser
action is written to the activity log.
"""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import itertools


TOOLS = {
    'context': ('Read source reports, memory, or archived native thread context; paginated',
                {'task_id':{'type':'string'}, 'thread_id':{'type':'string'}, 'offset':{'type':'integer'}}),
    'list_files': ('List files in a directory (project-relative; absolute paths with full PC access)', {'path':{'type':'string'}}),
    'read_file': ('Read a UTF-8 text file (project-relative; absolute paths with full PC access)', {'path':{'type':'string'}}),
    'write_file': ('Write a text file (project-relative; absolute paths with full PC access)', {'path':{'type':'string'},'text':{'type':'string'}}),
    'run_shell': ('Run a bounded PowerShell command in the project folder (any folder via cwd with full PC access)',
                  {'command':{'type':'string'},'timeout':{'type':'integer'},'cwd':{'type':'string'}}),
    'memory': ('Persist a durable project finding', {'text':{'type':'string'}}),
}

BROWSER_TOOLS = {
    'browser_tabs': ('List open browser tabs (id, title, url)', {}),
    'browser_snapshot': ('Read the visible text, links, buttons and inputs of a tab', {'tab_id':{'type':'string'}}),
    'browser_screenshot': ('Save a PNG screenshot of a tab on the PC and return its path', {'tab_id':{'type':'string'}}),
    'browser_open': ('Open a URL in the current tab or a new tab and wait for it to load',
                     {'url':{'type':'string'},'new_tab':{'type':'boolean'},'tab_id':{'type':'string'}}),
    'browser_click': ('Click an element by CSS selector or by its visible text',
                      {'selector':{'type':'string'},'text':{'type':'string'},'tab_id':{'type':'string'}}),
    'browser_type': ('Type text into an element (CSS selector); submit=true presses Enter',
                     {'selector':{'type':'string'},'text':{'type':'string'},'submit':{'type':'boolean'},'tab_id':{'type':'string'}}),
    'browser_eval': ('Evaluate a JavaScript expression in a tab and return the JSON result',
                     {'expression':{'type':'string'},'tab_id':{'type':'string'}}),
    'browser_close': ('Close a browser tab', {'tab_id':{'type':'string'}}),
}
BROWSER_ACTIONS = {'browser_open','browser_click','browser_type','browser_eval','browser_close'}
REA_TOOLS = {
    'rea_tools': ('Discover REA (Reverse Engineer Anything) tools and their argument schemas', {}),
    'rea_call': ('Call a discovered REA tool', {'name':{'type':'string'},'arguments':{'type':'object'}}),
}
from .local_mcp import SERVERS as LOCAL_SERVERS, BY_KEY as LOCAL_BY_KEY, TOOL_SERVER, tool_specs, enabled as local_enabled
READ_ONLY_ROLES = ('architect','researcher','quality_planner','planner','reviewer')
from . import long_calls
from . import token_budget
# Hard usage limit (owner rule: ALL chats stop at once, the Main too): while the 6 h token budget
# is used up, Bridge.limit_gate refuses every tool with a LIMIT_STOP: error, for the Main (no
# assignment token) and for coworkers (with token). During a burst pause only coworker tools are
# refused (B42). Only this read-only allow list still works. Every name was checked against the
# tool specs: OPERATOR_TOOLS entries with readOnlyHint True (devtools RO, SCREEN_OPERATOR_TOOLS),
# the bridge's own discovery tools, nt_job_result, the screen tools and the coworkers' read-only
# tools (engine.READ_ONLY_TOOLS: read_file, list_files, context). Not allowed although marked
# read-only: nt_local_mcp (refresh=true restarts servers) and nt_inference_body (Notion client).
# Local MCP '*_tools' discovery (readOnlyHint True in tools/list) is allowed by name suffix.
# The gate sits in _call(), operator_call() and local_call(), before any tool runs. A long job
# started before the limit keeps running (it is not killed); nt_job_result still reads its result.
LIMIT_ALLOWED_TOOLS = frozenset((
    'mcp_servers', 'mcp_list_tools', long_calls.JOB_TOOL,
    'nt_read_file', 'nt_list_dir', 'nt_search_code', 'nt_file_outline', 'nt_memory_read',
    'nt_screen_windows', 'nt_screen_capture', 'screen_windows', 'screen_capture',
    'read_file', 'list_files', 'context',
))
# nt_api: every GET is read-only; of the POST routes only stopping work (docs/HOST_API.md:
# /api/run/cancel {id}, /api/run/pause {id, paused}), which lowers usage.
LIMIT_ALLOWED_API_POSTS = ('/api/run/cancel', '/api/run/pause')
LIMIT_EVENT = 'limit_stop_tool_refused'
LIMIT_EVENT_SECONDS = 60.0  # at most one event per (caller, tool) per minute
# nt_job_result (long_calls.JOB_TOOL) only reads a finished or running job of the same caller:
# read-only and allowed for every role. Bridge.rpc answers it before any role check or lock.
ANY_ROLE_TOOLS = (long_calls.JOB_TOOL,)
# Writes stay synchronous, never background jobs (a late write could collide with the next owner).
INLINE_TOOLS = ('write_file', 'nt_write_file', 'nt_edit_file')
# Custom roles (settings role_definitions, read like server.role_state()) are read-only when their
# tools value is role_policy.READ_ONLY ('read_only'); see Bridge.role_is_read_only.
CUSTOM_READ_ONLY = 'read_only'
READ_ONLY_REFUSAL = 'This planning/review role has read-only PC access'
from .screen_capture import SCREEN_TOOLS, SCREEN_OPERATOR_TOOLS, SCREEN_OPERATOR_NAMES, ScreenCapture
from .agent_guard import GUARD_RULE, PLAN_RULE


def shell_command(command):
    """PowerShell on Windows; pwsh or sh elsewhere (development/tests)."""
    if os.name=='nt':
        return ['powershell.exe','-NoProfile','-NonInteractive','-Command',command]
    if shutil.which('pwsh'):
        return ['pwsh','-NoProfile','-NonInteractive','-Command',command]
    return ['sh','-c',command]


TEST_COMMAND = re.compile(r'python -m unittest((?: tests[.][A-Za-z0-9_]+){1,20})', re.ASCII)


def read_only_test_modules(command):
    # The one shell form read-only roles may run: python -m unittest followed by
    # 1 to 20 tests.<module> names, single spaces. Returns the module names, or
    # None for anything else (flags, quotes, pipes, redirection, separators,
    # variables, newlines, non-ASCII text, non-strings).
    if not isinstance(command, str) or not command.isascii():
        return None
    match = TEST_COMMAND.fullmatch(command)
    if not match:
        return None
    return match.group(1).split()


def unittest_python():
    # Interpreter for read-only test runs; the frozen EXE is not a Python.
    if not getattr(sys, 'frozen', False):
        return sys.executable
    found = shutil.which('python') or shutil.which('py')
    if not found:
        raise ValueError('No Python interpreter found for read-only test runs')
    return found



# Operator scope: only with the owner Host API key (never the worker bridge token).
OPERATOR_TOOLS = [
    {'name':'nt_api','description':'Operator: call a NotionTool Host API route on the owner PC. GET or POST /api/... (e.g. /api/health, /api/state, /api/chats, /api/send, /api/main/create, /api/operations). ' + GUARD_RULE + ' ' + PLAN_RULE,
     'annotations':{'readOnlyHint':False,'destructiveHint':True},
     'inputSchema':{'type':'object','properties':{'method':{'type':'string','enum':['GET','POST']},'path':{'type':'string'},'body':{'type':'object'},'timeout':{'type':'number'}},'required':['path']}},
    {'name':'nt_notion_call','description':'Operator: raw internal Notion API call (POST /api/v3/<method>) as the selected harness account. For verified reverse-engineering tests.',
     'annotations':{'readOnlyHint':False,'destructiveHint':True},
     'inputSchema':{'type':'object','properties':{'method':{'type':'string'},'body':{'type':'object'},'account_id':{'type':'string'},'save_to':{'type':'string','description':'Optional relative path below work/ (e.g. work/notion/page.json). The full JSON response is written to that file and only path, bytes, sha256 and the top-level keys are returned.'}},'required':['method','body']}},
    {'name':'nt_inference_body','description':'Operator: build (not send) the standard NotionTool runInferenceTranscript body for a thread and prompt. Edit it, then send with nt_notion_stream.',
     'annotations':{'readOnlyHint':True,'destructiveHint':False},
     'inputSchema':{'type':'object','properties':{'thread_id':{'type':'string'},'prompt':{'type':'string'},'model':{'type':'string'},'create':{'type':'boolean'},'account_id':{'type':'string'}},'required':['thread_id','prompt']}},
    {'name':'nt_notion_stream','description':'Operator: send a full runInferenceTranscript body and return the streamed NDJSON lines (capped).',
     'annotations':{'readOnlyHint':False,'destructiveHint':True},
     'inputSchema':{'type':'object','properties':{'body':{'type':'object'},'account_id':{'type':'string'},'timeout':{'type':'number'},'max_lines':{'type':'integer'}},'required':['body']}},
    {'name':'nt_local_mcp','description':'Operator: list the local MCP servers (REA, Roblox, Blender, ...) and their current tools. refresh=true restarts the enabled servers and rediscovers their tools, so new tools work without reconnecting this MCP.',
     'annotations':{'readOnlyHint':True,'destructiveHint':False},
     'inputSchema':{'type':'object','properties':{'server':{'type':'string'},'refresh':{'type':'boolean'}}}},
    {'name':'nt_local_mcp_call','description':'Operator: call a tool of an enabled local MCP server (server = rea, roblox, blender). Use nt_local_mcp to see tools and schemas.',
     'annotations':{'readOnlyHint':False,'destructiveHint':True},
     'inputSchema':{'type':'object','properties':{'server':{'type':'string'},'name':{'type':'string'},'arguments':{'type':'object'}},'required':['server','name']}},
]
from .devtools import TOOLS as DEV_TOOLS, NAMES as DEV_NAMES, call as dev_call
from .devtools import _path as dev_path
from . import file_registry
from . import snapshots as file_snapshots
from . import eol_guard


def eol_attribute(path):
    """Return 'lf' or 'crlf' from the git eol attribute of path; 'lf' when unset, unknown or unreadable."""
    path = Path(path)
    try:
        done = subprocess.run(['git', 'check-attr', 'eol', '--', path.name], cwd=str(path.parent), shell=False,
                              capture_output=True, text=True, timeout=10,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        return 'lf'
    if done.returncode != 0:
        return 'lf'
    value = (done.stdout or '').strip().rsplit(': ', 1)[-1].strip()
    return value if value in eol_guard.STYLES else 'lf'


def write_with_eol(path, text):
    """Write text to path keeping the line ending and BOM state of an existing file (rank 2, eol_guard).

    A new file, or an existing file without any newline, follows the git eol attribute ('lf' when unset).
    An existing file whose bytes are mixed (eol_guard.detect) is written as given. The result reports the
    ending that was written and, when it differs from the ending of the given text, 'eol_changed'.
    """
    path = Path(path)
    existing = path.read_bytes() if path.exists() else None
    given = text.encode('utf-8')
    kept_mixed = existing is not None and eol_guard.detect(existing) == 'mixed'
    if kept_mixed:
        data = given
    else:
        default = 'lf'
        if existing is None or eol_guard.detect(existing) == 'none':
            default = eol_attribute(path)
        data = eol_guard.normalize(text, existing, default=default)
    path.write_bytes(data)
    result = {'written': str(path), 'bytes': len(data), 'eol': eol_guard.detect(data)}
    before = eol_guard.detect(given)
    if kept_mixed:
        result['eol_kept'] = 'mixed'
    elif before != 'none' and before != result['eol']:
        result['eol_changed'] = before + '->' + result['eol']
    if text.startswith('\ufeff') and not data.startswith(eol_guard.BOM):
        result['bom_dropped'] = True
    return result

# O5: old bytes of an existing project file are stored before a coworker write or edit,
# under <data folder>/snapshots, bounded by snapshots.prune().
SNAPSHOT_DIR = 'snapshots'
SNAPSHOT_KEEP = 200
SNAPSHOT_MAX_MB = 200
_SNAPSHOT_SEQ = itertools.count(1)
# The data folder (store.directory, with the snapshots) is hidden from project tools when it
# lies inside the project, e.g. NotionTool working on itself. Keys use file_registry.path_key.
DATA_FOLDER_REFUSAL = ('%s refused: %s is inside the NotionTool data folder (coworker snapshots and app state); '
                       'bridge project tools never list, search, outline or change files there.')
DEV_GUARDED = {'nt_list_dir': 'Listing', 'nt_search_code': 'Search', 'nt_file_outline': 'Outline',
               'nt_write_file': 'Write', 'nt_edit_file': 'Edit'}
_HIT_PATH = re.compile(r'^(.*?):\d+: ')


def _key_inside(key, base):
    base = base.rstrip('\\/')
    return key == base or key.startswith(base + os.sep)
OPERATOR_TOOLS = OPERATOR_TOOLS + DEV_TOOLS + SCREEN_OPERATOR_TOOLS
OPERATOR_NAMES = {t['name'] for t in OPERATOR_TOOLS}
for _tool in OPERATOR_TOOLS:
    if _tool['name'] in ('nt_write_file', 'nt_edit_file'):
        # Coworker chats pass their token so their own task files are not refused as locked.
        _tool['inputSchema']['properties'].setdefault('assignment_token', {'type': 'string', 'description': 'Coworker chats only: your assignment token. Your own task files stay writable; files of other tasks or the operator stay locked.'})

# Fix V: an uncertain task may still be working in Notion (the stream ended but the
# turn goes on). Keep its capability alive so its tool calls are not rejected.
LIVE_TASK_STATES = ('running', 'uncertain')

class Bridge:
    def __init__(self, store, engine, browser=None):
        self.store,self.engine = store,engine
        self.shell_gate = threading.BoundedSemaphore(4)
        self._browser = browser

    # ---- settings ---------------------------------------------------------
    def full_access(self):
        return bool(self.store.settings().get('mcp_full_access', False))

    def browser_access(self):
        return bool(self.store.settings().get('mcp_browser_access', False))

    def screen_access(self):
        # Window capture is read-only and on by default; Settings can switch it off.
        return bool(self.store.settings().get('screen_capture', True))

    def screen(self):
        if getattr(self, '_screen', None) is None:
            self._screen = ScreenCapture(self.store)
        return self._screen

    def tools(self):
        tools = dict(TOOLS)
        if self.browser_access():
            tools.update(BROWSER_TOOLS)
        if self.screen_access():
            tools.update(SCREEN_TOOLS)
        settings = self.store.settings()
        for server in LOCAL_SERVERS:
            if local_enabled(settings, server):
                tools.update(tool_specs(server))
        return tools

    def local_client(self, key):
        lock = self.__dict__.setdefault('_local_lock', threading.Lock())
        with lock:
            clients = self.__dict__.setdefault('_local_mcp', {})
            if key not in clients:
                from .rea import Rea
                server = LOCAL_BY_KEY[key]
                clients[key] = Rea(command=server['command'](), label=server['label'], prefix=key)
            return clients[key]

    def local_refresh(self, key=None, restart=True, schemas=False):
        settings = self.store.settings()
        out = []
        for server in LOCAL_SERVERS:
            if key and server['key'] != key:
                continue
            entry = {'server': server['key'], 'name': server['name'], 'enabled': local_enabled(settings, server)}
            if entry['enabled']:
                if restart:
                    self.close_local([server['key']])
                try:
                    tools = self.local_client(server['key']).tools()
                    entry.update(ok=True, count=len(tools),
                                 tools=tools if schemas else [{'name': t.get('name'), 'description': str(t.get('description', ''))[:200]} for t in tools])
                except Exception as exc:
                    entry.update(ok=False, error=str(exc)[:300])
            out.append(entry)
        return out

    def close_local(self, keys=None):
        lock = self.__dict__.setdefault('_local_lock', threading.Lock())
        with lock:
            clients = self.__dict__.setdefault('_local_mcp', {})
            closing = [clients.pop(k) for k in list(clients) if keys is None or k in keys]
        for client in closing:
            try:
                client.close()
            except Exception:
                pass

    @property
    def browser(self):
        if self._browser is None:
            from .browser import Browser
            self._browser = Browser(self.store)
        return self._browser

    # ---- capability -------------------------------------------------------
    def has_assignment(self, token):
        return bool(token) and any(t.get('lease')==token and t.get('state') in LIVE_TASK_STATES for t in self.store.all('tasks'))

    def assignment(self, token):
        # Capability is revoked as soon as an attempt ends or is superseded.
        task = next((t for t in self.store.all('tasks') if t.get('lease')==token and t['state'] in LIVE_TASK_STATES),None)
        if not task:
            raise ValueError('Assignment expired or invalid; do not join again')
        run = self.store.get('runs',task['run_id'])
        if run['state'] not in ('approach','planning','executing','reviewing','fixing','integrating','paused'):
            raise ValueError('Run is no longer active')
        return task,run

    @staticmethod
    def project(run):
        if run.get('project_path') and Path(run['project_path']).is_dir():
            return Path(run['project_path']).resolve()
        return None

    def root(self, run):
        """Project folder, or the user's home folder when full PC access allows it."""
        project = self.project(run)
        if project:
            return project
        if self.full_access():
            return Path.home().resolve()
        if not run.get('project_path'):
            raise ValueError('No PC project folder configured for this chat')
        raise ValueError('The configured project folder does not exist')

    def worktree(self, task):
        """The git worktree a task works in: its own copy, or for a review the reviewed task's copy.

        None when the task has no existing worktree; tools then use the project folder.
        """
        if not task:
            return None
        candidates = [task]
        if task.get('review_of'):
            candidates.append(self.store.get('tasks', task['review_of'], None) or {})
        for item in candidates:
            folder = item.get('patch_worktree')
            if item is not task and item.get('patch_state') == 'merged':
                continue  # A merged change is reviewed in the main project folder.
            if folder and Path(folder).is_dir():
                return Path(folder).resolve()
        return None

    def path(self, run, relative='.', task=None):
        root = self.root(run)
        copy = self.worktree(task)
        if copy is not None:
            # The task works in its own copy: relative paths and absolute paths into the main
            # project folder both mean the same file inside the copy.
            raw = Path(os.path.expanduser(str(relative)))
            project = self.project(run)
            if raw.is_absolute() and project:
                resolved = raw.resolve()
                if resolved.is_relative_to(project) and not resolved.is_relative_to(project / 'work'):
                    raw = resolved.relative_to(project)
            path = (raw if raw.is_absolute() else copy / raw).resolve()
            if self.full_access() or path.is_relative_to(copy) or path.is_relative_to(root):
                return path
            raise ValueError("File is outside this task's copy of the project (%s)" % copy)
        path = (root/os.path.expanduser(str(relative))).resolve()
        if self.full_access():
            # Full PC access (explicit opt-in in Settings): absolute paths anywhere.
            return path
        if not path.is_relative_to(root):
            raise ValueError('File is outside the configured project folder (enable full PC access in Settings)')
        return path

    # ---- hard usage limit -------------------------------------------------
    @staticmethod
    def limit_allowed(name, args):
        """Read-only allow list at the hard limit (see LIMIT_ALLOWED_TOOLS / LIMIT_ALLOWED_API_POSTS)."""
        name = str(name or '')
        if name in LIMIT_ALLOWED_TOOLS:
            return True
        if name in TOOL_SERVER and name.endswith('_tools'):
            return True  # local MCP tool discovery (readOnlyHint True in tools/list)
        if name == 'nt_api':
            args = args if isinstance(args, dict) else {}
            # Same method rule as operator_call: a body without a method means POST.
            method = str(args.get('method') or ('POST' if args.get('body') is not None else 'GET')).upper()
            path = str(args.get('path') or '').split('?', 1)[0].split('#', 1)[0]
            return method == 'GET' or (method == 'POST' and path in LIMIT_ALLOWED_API_POSTS)
        return False

    def _limit_purpose(self, token):
        """'worker' for a live assignment token, else 'main' (also when the lookup fails)."""
        try:
            return 'worker' if token and self.has_assignment(token) else 'main'
        except Exception:
            return 'main'

    def limit_gate(self, name, args, token='', caller=''):
        """Refuse a tool (LimitStop, text 'LIMIT_STOP: ...') at the hard usage limit.

        The 6 h token budget stops every caller: the Main (no assignment token) and coworkers (with
        token). A burst pause stops only coworkers (purpose 'worker'). Read-only allow-listed tools
        always pass. Fail-open: hard_stop_state never raises and returns stop=False (and logs
        token_budget_failed) when the ledger cannot be read.
        """
        if self.store is None or self.limit_allowed(name, args):
            return
        purpose = self._limit_purpose(token)
        now_fn = getattr(self, '_limit_now', None)  # tests only: a fake clock for the window
        state = token_budget.hard_stop_state(self.store, purpose, now_fn() if now_fn else None)
        if not state.get('stop'):
            return
        wait = int(state.get('wait') or 0)
        source = str(state.get('source') or '6h')
        who = caller or ('worker' if purpose == 'worker' else 'main')
        self._limit_event(name, who, purpose, source, wait, str(state.get('reason') or ''))
        raise token_budget.LimitStop(token_budget.limit_stop_text(wait, source))

    def _limit_event(self, name, caller, purpose, source, wait, reason):
        """At most one limit_stop_tool_refused event per (caller, tool) per LIMIT_EVENT_SECONDS."""
        try:
            clock = getattr(self, '_limit_clock', None) or time.monotonic
            key = (str(caller), str(name)[:80])
            with self.__dict__.setdefault('_limit_lock', threading.Lock()):
                now = clock()
                seen = self.__dict__.setdefault('_limit_event_at', {})
                last = seen.get(key)
                if last is not None and 0 <= now - last < LIMIT_EVENT_SECONDS:
                    return
                if len(seen) > 500:
                    for old in [k for k, v in seen.items() if not 0 <= now - v < LIMIT_EVENT_SECONDS]:
                        seen.pop(old, None)
                seen[key] = now
            self.store.event(LIMIT_EVENT, tool=key[1], caller=key[0], purpose=purpose, source=source, wait_s=wait,
                             message=('Tool refused at the hard usage limit: ' + reason)[:300])
        except Exception:
            pass

    # ---- tools ------------------------------------------------------------
    def _tool_started(self, run_id, task_id='', tool='', message=''):
        """Record a tool start; call() records the matching tool_finished (also on errors and timeouts)."""
        self.store.event('tool_started', run_id, task_id=task_id, tool=tool, message=message)
        self._tool_ctx().started = (run_id, task_id, tool, time.monotonic())

    def _tool_ctx(self):
        return self.__dict__.setdefault('_tool_local', threading.local())

    def call(self, name, args, token):
        ctx = self._tool_ctx()
        ctx.started = None
        try:
            result = self._call(name, args, token)
        except BaseException as exc:
            started, ctx.started = ctx.started, None
            if started:
                run_id, task_id, tool, begun = started
                self.store.event('tool_finished', run_id, task_id=task_id, tool=tool, ok=False,
                                 seconds=round(time.monotonic() - begun, 3),
                                 error=(type(exc).__name__ + ': ' + str(exc))[:200])
            raise
        started, ctx.started = ctx.started, None
        if started:
            run_id, task_id, tool, begun = started
            extra = {'exit_code': result.get('exit_code')} if isinstance(result, dict) and 'exit_code' in result else {}
            self.store.event('tool_finished', run_id, task_id=task_id, tool=tool, ok=True,
                             seconds=round(time.monotonic() - begun, 3), **extra)
        return result

    def _call(self, name, args, token):
        self.limit_gate(name, args, token)  # hard usage limit: before any tool runs (Main and coworkers)
        if name in SCREEN_TOOLS and self.screen_access() and not self.has_assignment(token):
            # Read-only screen tools also work in the user's own chats (Main), which have no assignment token.
            return self.screen().call(name, args, role='chat')
        cwd = None
        verifier=getattr(getattr(self.engine,'adapter',None),'verify_response_model',None)
        if verifier is not None:
            # A wrongly routed model must not touch project tools before its
            # final response is rejected. Do network reads outside the engine
            # lock, then recheck the capability below before any side effect.
            with self.engine.lock:
                pending,_=self.assignment(token)
                worker=self.store.get('workers',pending['worker_id'])
            verifier(worker,pending)
        with self.engine.lock:
            task,run = self.assignment(token)
            if name not in self.tools():
                raise ValueError('Unknown or disabled tool' + (' (enable browser access in Settings)' if name in BROWSER_TOOLS else ' (enable '+LOCAL_BY_KEY[TOOL_SERVER[name]]['label']+' in Settings)' if name in TOOL_SERVER else ''))
            if name in TOOL_SERVER:
                rea = self.local_client(TOOL_SERVER[name])
                self._tool_started(run['id'],task_id=task['id'],tool=name,message=LOCAL_BY_KEY[TOOL_SERVER[name]]['label']+': '+str(args.get('name','discover'))[:100])
            if self.role_is_read_only(task['role']) and (name=='write_file' or name in BROWSER_ACTIONS
                    or (name=='run_shell' and read_only_test_modules(args.get('command')) is None)):
                raise ValueError('This planning/review role has read-only PC access')
            if name in TOOL_SERVER:
                pass
            elif name in SCREEN_TOOLS:
                pass
            elif name in BROWSER_TOOLS:
                if name in BROWSER_ACTIONS:
                    detail = args.get('url') or args.get('selector') or args.get('text') or args.get('expression') or ''
                    self._tool_started(run['id'],task_id=task['id'],tool=name,message='Browser: '+name+' '+str(detail)[:200])
            elif name=='context':
                wanted = args.get('task_id')
                if args.get('thread_id'):
                    worker = self.store.get('workers', task['worker_id'])
                    thread = str(args['thread_id'])
                    archive = self.store.get('thread_context', worker['account_id'] + ':' + thread)
                    if not archive:
                        raise ValueError('No observed transcript for this account and thread')
                    text = json.dumps(archive, ensure_ascii=False)
                    offset = max(0, int(args.get('offset', 0)))
                    return dict(text=text[offset:offset+16000], next_offset=offset+16000 if len(text)>offset+16000 else None)
                records = [t for t in self.engine.tasks(run['id']) if not wanted or t['id']==wanted]
                if wanted:
                    source = self.store.get('tasks', wanted)
                    previous = self.store.get('runs', (source or {}).get('run_id'), {})
                    if source and previous.get('chat_id') == run['chat_id']:
                        records = [source]
                memory_id = run.get('folder_id') or run['chat_id']
                payload = dict(goal=run['goal'],conversation=[m for m in self.store.all('messages') if m['chat_id']==run['chat_id']],
                            memory=self.store.get('memory', memory_id, {}),
                            memory_baseline=self.store.get('memory_baselines', memory_id, {}),
                            memory_entries=[m for m in self.store.all('memory_entries') if m['memory_id']==memory_id],
                            reports=[dict(id=t['id'],title=t['title'],state=t['state'],result=t['result']) for t in records],
                            files_in_use=file_registry.in_use(self.store, exclude_task=task['id']))
                text = json.dumps(payload, ensure_ascii=False)
                if len(text) > 16000 or args.get('offset'):
                    offset = max(0, int(args.get('offset', 0)))
                    return dict(text=text[offset:offset+16000], next_offset=offset+16000 if len(text)>offset+16000 else None)
                return payload
            elif name=='memory':
                ident = run.get('folder_id') or run['chat_id']
                self.store.remember(ident, str(args.get('text','')), task['id'])
                return {'saved':True}
            elif name=='run_shell' and self.role_is_read_only(task['role']):
                # Read-only test run: fixed project root (a cwd argument is ignored),
                # no folder claim, only the lease check.
                cwd = self.worktree(task) or self.project(run)
                if cwd is None:
                    raise ValueError('Read-only test runs need a project folder')
                self.engine.check_lease(task['id'],token)
                self._tool_started(run['id'],task_id=task['id'],tool=name,
                                 message='Tests in '+str(cwd)[:200]+': '+str(args.get('command',''))[:300])
            elif name=='run_shell':
                cwd = self.path(run,str(args.get('cwd') or '.'),task=task)
                if not cwd.is_dir():
                    raise ValueError('Shell folder does not exist')
                # Shell is a powerful tool, not a filesystem sandbox. With full PC access it is still
                # serialized against every edit. In a project folder it no longer holds the whole folder
                # for the rest of the task, which blocked every parallel coworker (tests, builds). Writes
                # stay owned per file, and the slots in shell() limit concurrent commands.
                if self.full_access():
                    self.engine.claim_absolute(task['id'],token,Path(cwd).anchor)
                else:
                    self.engine.check_lease(task['id'],token)
                self._tool_started(run['id'],task_id=task['id'],tool=name,
                                 message='Shell in '+str(cwd)[:200]+': '+str(args.get('command',''))[:300])
            else:
                path = self.path(run,str(args.get('path','.')),task=task)
                if name=='list_files':
                    area = self.hidden_area(self.project(run))
                    self.guard_area(path, area, 'Listing')
                    return [dict(name=p.name,directory=p.is_dir()) for p in sorted(path.iterdir()) if not self.in_area(p, area)][:500]
                if name=='read_file':
                    if path.stat().st_size>2*1024*1024:
                        raise ValueError('File is too large; choose a smaller artifact')
                    return path.read_text(encoding='utf-8',errors='replace')
                if name=='write_file':
                    if 'text' not in args and isinstance(args.get('content'),str):
                        args={**args,'text':args['content']}
                    if 'text' in args and 'content' in args and args['text'] != args['content']:
                        raise ValueError('Conflicting text/content values; no file was changed')
                    if not isinstance(args.get('text'),str):
                        raise ValueError('write_file requires a text string; no file was changed')
                    project = self.project(run)
                    self.guard_area(path, self.hidden_area(project), 'Write')
                    self.guard_scope(task, run, path)
                    if project and path.is_relative_to(project):
                        self.engine.claim_file(task['id'],token,str(path.relative_to(project)))
                    else:
                        self.engine.claim_absolute(task['id'],token,path)
                    text = str(args.get('text',''))
                    if len(text)>2*1024*1024:
                        raise ValueError('Write is too large')
                    self._tool_started(run['id'],task_id=task['id'],tool=name,message='Write: '+str(path)[:300])
                    self.snapshot_before_write(task, run, path)
                    path.parent.mkdir(parents=True,exist_ok=True)
                    return write_with_eol(path,text)
        if name in TOOL_SERVER:
            return rea.tools() if name.endswith('_tools') else rea.call(str(args.get('name','')), args.get('arguments') or {})
        if name == 'screen_capture':
            # Worker capture: checked against the shared capture gate first (Phase 7).
            return self.worker_capture(args, task, run)
        if name in SCREEN_TOOLS:
            # Window capture is read-only and runs outside the coordinator lock.
            return self.screen().call(name, args, task_id=task['id'], run_id=run['id'], role=task.get('role', ''))
        if name in BROWSER_TOOLS:
            # Slow browser work runs outside the coordinator lock.
            return self.browser.call(name, args)
        return self.shell(name, args, task, run, cwd)

    # ---- worker capture gate (Phase 7) ------------------------------------
    def capture_settings(self, run):
        """Project settings for the capture policy: the app settings, overlaid by the run's project_settings."""
        settings = dict(self.store.settings() or {})
        extra = (run or {}).get('project_settings')
        if isinstance(extra, dict):
            settings.update(extra)
        return settings

    def _capture_counts(self):
        lock = self.__dict__.setdefault('_capture_lock', threading.Lock())
        return lock, self.__dict__.setdefault('_capture_count_map', {})

    def captures_taken(self, task_id):
        """Number of worker captures that actually happened for this task."""
        lock, counts = self._capture_counts()
        with lock:
            return counts.get(task_id, 0)

    def worker_capture(self, args, task, run):
        """Take a worker screen capture only when plain_protocol.capture_gate allows it.

        A refusal takes no capture, emits capture_refused and returns a readable
        English text. Only captures that actually happen are counted per task.
        Operator and chat captures never come through here.
        """
        from . import plain_protocol
        refusal = plain_protocol.capture_gate(task, self.capture_settings(run), self.captures_taken(task['id']))
        if refusal is not None:
            reason = refusal.get('reason') or {}
            code = str(reason.get('reason') or 'refused')
            self.store.event('capture_refused', run['id'], task_id=task['id'], task_class=reason.get('task_class', ''),
                             reason=code, already_taken=reason.get('already_taken', 0), message=refusal.get('text', ''))
            return {'refused': True, 'reason': code, 'text': refusal.get('text') or 'Screen capture refused for this task. No capture was taken.'}
        result = self.screen().call('screen_capture', args, task_id=task['id'], run_id=run['id'], role=task.get('role', ''))
        lock, counts = self._capture_counts()
        with lock:
            counts[task['id']] = counts.get(task['id'], 0) + 1
        return result

    def local_call(self, name, args):
        """Local MCP tool (Roblox, Blender, REA) for the owner connection, without a worker assignment."""
        self.limit_gate(name, args)  # hard usage limit: refused for the Main as well
        server = LOCAL_BY_KEY[TOOL_SERVER[name]]
        if not local_enabled(self.store.settings(), server):
            raise ValueError('Switch on '+server['label']+' in MCP servers first')
        client = self.local_client(server['key'])
        return client.tools() if name.endswith('_tools') else client.call(str(args.get('name','')), args.get('arguments') or {})

    def shell(self, name, args, task, run, cwd):
        if not self.shell_gate.acquire(timeout=1):
            raise ValueError('All local tool slots are busy; use other work before retrying')
        try:
            timeout = max(1,min(600 if self.full_access() else 120,int(args.get('timeout',60))))
            command = str(args.get('command',''))
            if not command:
                raise ValueError('Command is empty')
            creationflags = subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
            if self.role_is_read_only(task.get('role')):
                # The one read-only shell form: unittest modules as an argument list,
                # no shell, the project root as working folder, at most 120 s.
                modules = read_only_test_modules(command)
                if modules is None:
                    raise ValueError('This planning/review role has read-only PC access')
                timeout = max(1,min(120,int(args.get('timeout',60))))
                root = Path(cwd)
                env = dict(os.environ,PYTHONPATH=os.pathsep.join([str(root),str(root/'tests')]))
                process = subprocess.Popen([unittest_python(),'-m','unittest',*modules],shell=False,
                    cwd=str(root),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=creationflags)
            else:
                process = subprocess.Popen(shell_command(command),
                    cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=creationflags)
            try:
                output,_ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                if os.name=='nt':
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=creationflags)
                else:
                    process.kill()
                process.communicate()
                raise ValueError('Command timed out; its process tree was stopped') from None
            return dict(exit_code=process.returncode,output=output.decode('utf-8',errors='replace')[-30000:])
        finally:
            self.shell_gate.release()

    def save_root(self):
        """Folder for nt_notion_call save_to files (work/ next to the operator tools)."""
        return dev_path('work')

    def save_target(self, save_to):
        """Resolve save_to to a file below work/; None when not given."""
        from pathlib import PureWindowsPath
        if save_to is None or save_to=='': return None
        if not isinstance(save_to,str): raise ValueError('save_to must be a string')
        raw=save_to.replace('\\','/')
        windows=PureWindowsPath(save_to)
        if raw.startswith('/') or windows.drive or windows.root or Path(save_to).is_absolute():
            raise ValueError('save_to must be a relative path below work/, not an absolute path')
        parts=raw.split('/')
        if '..' in parts: raise ValueError("save_to must not contain '..'")
        parts=[part for part in parts if part not in ('','.')]
        if parts and parts[0]=='work': parts=parts[1:]
        if not parts: raise ValueError('save_to must name a file below work/')
        root=Path(self.save_root()).resolve()
        target=root.joinpath(*parts).resolve()
        if target==root or not target.is_relative_to(root):
            raise ValueError('save_to resolves outside work/')
        return target

    def save_response(self, target, result):
        """Write the full JSON response to target and return only a short summary."""
        import hashlib
        data=json.dumps(result,ensure_ascii=False,indent=2).encode('utf-8')
        target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_name(target.name+'.tmp')
        temp.write_bytes(data)
        os.replace(temp,target)
        root=Path(self.save_root()).resolve()
        return {'path':'work/'+target.relative_to(root).as_posix(),'bytes':len(data),
                'sha256':hashlib.sha256(data).hexdigest(),'keys':list(result) if isinstance(result,dict) else []}

    # ---- data folder guard (snapshots stay out of project tools) ---------
    def hidden_area(self, project):
        """path_key of store.directory when it lies inside project, else None (nothing hidden)."""
        if project is None or not getattr(self.store, 'directory', None):
            return None
        data = file_registry.path_key(self.store.directory)
        return data if _key_inside(data, file_registry.path_key(project)) else None

    @staticmethod
    def in_area(path, area):
        return bool(area) and _key_inside(file_registry.path_key(path), area)

    def guard_area(self, path, area, action):
        """The one shared check: refuse a listing, search, outline, write or edit inside the area."""
        if self.in_area(path, area):
            raise ValueError(DATA_FOLDER_REFUSAL % (action, path))

    def filter_dev_result(self, name, args, result, area):
        """Drop data-folder entries from nt_list_dir and nt_search_code results."""
        if not area or not isinstance(result, dict):
            return result
        if name == 'nt_list_dir':
            base = dev_path((args or {}).get('path') or '.')
            return dict(result, entries=[e for e in result.get('entries', [])
                                         if not self.in_area(base / str(e).rstrip('/'), area)])
        if name == 'nt_search_code':
            kept = []
            for hit in result.get('results', []):
                match = _HIT_PATH.match(str(hit))
                if not match or not self.in_area(dev_path(match.group(1)), area):
                    kept.append(hit)
            return dict(result, results=kept)
        return result

    def role_is_read_only(self, role):
        """True for READ_ONLY_ROLES and for a custom role whose role_definitions entry is read-only.

        Reads settings role_definitions the way server.role_state() does (name + tools), without
        importing server.py (it imports this module). Fail-safe: an entry only ever makes a role
        stricter, also while dynamic roles are off or another entry is invalid.
        """
        if role in READ_ONLY_ROLES:
            return True
        try:
            definitions = self.store.settings().get('role_definitions')
        except Exception:
            definitions = None
        for item in definitions if isinstance(definitions, list) else ():
            if isinstance(item, dict) and item.get('name') == role and (
                    item.get('tools') == CUSTOM_READ_ONLY or item.get('read_only') is True):
                return True
        return False

    def guard_scope(self, task, run, path):
        """Refuse a coworker write outside the task's non-empty files list (no list: no limit).

        Any write inside the task's own git worktree is allowed (merged and tested at the end).

        Absolute and project-relative entries are compared through file_registry.path_key, so
        both forms of the same file match; an entry that is a folder covers the files below it.
        engine.claim_file appends every claimed file to task['files'], so the assigned list is
        frozen as task['assigned_files'] at the first guarded write and checked from then on;
        a task that started without a files list stays unlimited after its first claim.
        """
        if isinstance(task.get('assigned_files'), list):
            assigned = task['assigned_files']
        else:
            assigned = list(task.get('files') or [])
            task['assigned_files'] = assigned
            self.store.put('tasks', dict(task))
        files = [str(f).strip() for f in assigned if str(f).strip()]
        copy = self.worktree(task)
        if copy is None and self._needs_own_copy(task, run):
            # Owner rule 2026-10-22: every coworker writes only in its own copy; main is changed by the merge.
            raise ValueError("Write refused: this task has no own copy of the project (git worktree), so writing "
                             "%s would change main directly; no file was changed. Stop and report this in your "
                             "result; NotionTool will give the task its own copy." % (path,))
        if not files:
            return
        project = self.project(run)
        base = project or self.root(run)
        key = file_registry.path_key(path)
        if copy is not None and _key_inside(key, file_registry.path_key(copy)):
            # Owner rule 2026-10-22: a task with its own git worktree may write any file in that copy;
            # the copy is merged and tested at the end, so the files list is a plan hint, not a wall.
            return
        for entry in files:
            raw = Path(os.path.expanduser(entry))
            targets = [raw if raw.is_absolute() else base / raw]
            if copy is not None:
                # The same entry inside the task's own copy (git worktree).
                rel = raw
                if raw.is_absolute():
                    try:
                        rel = raw.resolve().relative_to(project) if project else None
                    except ValueError:
                        rel = None
                if rel is not None:
                    targets.append(copy / rel)
            for target in targets:
                if _key_inside(key, file_registry.path_key(target)):
                    return
        raise ValueError("Write refused: %s is not in this task's files list (%s); no file was changed. "
                         "Only the assigned files may be written; report other needed changes in your result."
                         % (path, ', '.join(files)[:300]))

    def _needs_own_copy(self, task, run):
        """True for a writing executor of a patch-mode run: it must work in its own git worktree."""
        if not task or task.get('role') != 'executor' or not run:
            return False
        patch_on = getattr(self.engine, '_patch_on', None)
        if not callable(patch_on):
            return False
        try:
            from .engine import task_is_read_only
            if task_is_read_only(task):
                return False
            return bool(patch_on(run))
        except Exception:
            return False

    def _task_dev_path(self, token, path):
        """Resolve a coworker write path: inside the task's worktree when it has one, else as before."""
        with self.engine.lock:
            task, run = self.assignment(token)
            if self.worktree(task) is not None:
                return self.path(run, str(path or ''), task=task)
        return dev_path(path)

    def _claim_for_task(self, token, path):
        """Claim path for the task behind token (same rules as the write_file tool)."""
        with self.engine.lock:
            task, run = self.assignment(token)
            if self.role_is_read_only(task['role']):
                raise ValueError(READ_ONLY_REFUSAL)
            project = self.project(run)
            self.guard_area(path, self.hidden_area(project), 'Write')
            self.guard_scope(task, run, path)
            if project and path.is_relative_to(project):
                self.engine.claim_file(task['id'], token, path.relative_to(project).as_posix())
            else:
                self.engine.claim_absolute(task['id'], token, path)
            return task, run

    def snapshot_before_write(self, task, run, path):
        """O5: store the old bytes of an existing project file before a coworker write or edit.

        Byte-exact copy through snapshots.py under <data folder>/snapshots, one snapshot per
        write, bounded by snapshots.prune(). New files and files outside the project get none.
        Never raises: a failure only writes a snapshot_failed event and the write goes on.
        """
        try:
            project = self.project(run)
            target = Path(path)
            if project is None or not target.is_file():
                return None
            target = target.resolve()
            if not target.is_relative_to(project):
                return None
            store_dir = str(Path(self.store.directory).resolve() / SNAPSHOT_DIR)
            ident = '%s-%d-%d' % (file_snapshots.safe_task_id(task['id'])[:80], int(file_snapshots._clock()), next(_SNAPSHOT_SEQ))
            snapshot = file_snapshots.take(ident, [target], project, store_dir=store_dir)
            file_snapshots.prune(keep_last=SNAPSHOT_KEEP, max_mb=SNAPSHOT_MAX_MB, root=project, store_dir=store_dir)
            return snapshot
        except Exception as exc:
            try:
                self.store.event('snapshot_failed', (run or {}).get('id', ''), task_id=(task or {}).get('id', ''),
                                 path=str(path)[:300], message=(type(exc).__name__ + ': ' + str(exc))[:300])
            except Exception:
                pass
            return None

    def operator_call(self, app, name, args):
        import re, urllib.request, urllib.error
        from .harness_account import selected_account
        # Hard usage limit: every operator tool of the Main is refused except the read-only allow list.
        self.limit_gate(name, args, (args.get('assignment_token') or '') if isinstance(args, dict) else '',
                        caller='operator')
        if name in SCREEN_OPERATOR_NAMES: return self.screen().call(name[3:], args, role='operator')
        if name in DEV_NAMES:
            area = self.hidden_area(dev_path('.'))
            if name in DEV_GUARDED:
                self.guard_area(dev_path((args or {}).get('path') or '.'), area, DEV_GUARDED[name])
            if name in ('nt_write_file','nt_edit_file'):
                args = dict(args or {})
                token = args.pop('assignment_token', None)
                if token:
                    # A coworker task writes under its own lock: its own files pass,
                    # files of other tasks or of the operator are still refused.
                    # A task with its own git worktree writes into that copy, never into main.
                    target = self._task_dev_path(token, args.get('path'))
                    args['path'] = str(target)
                    task, run = self._claim_for_task(token, target)
                    # O5: only after the owner-token lock passed; never blocks the write.
                    self.snapshot_before_write(task, run, target)
                else:
                    # Register the operator edit so coworker chats see it and leave the file alone.
                    # Bounded wait (live 2026-10-22 04:13: an edit call never answered and the Main hung until
                    # the user stopped it): a busy engine lock must never stall a file edit indefinitely.
                    if self.engine.lock.acquire(timeout=10):
                        try:
                            file_registry.claim_operator(self.store, dev_path(args.get('path')))
                        finally:
                            self.engine.lock.release()
                    else:
                        try:
                            self.store.event('operator_claim_skipped', message='Engine busy for 10 s; edit runs without registry claim',
                                             path=str(args.get('path'))[:300])
                        except Exception:
                            pass
            return self.filter_dev_result(name, args, dev_call(name, args), area)
        if name=='nt_local_mcp':
            return self.local_refresh(str(args.get('server') or '') or None, bool(args.get('refresh')), schemas=bool(args.get('server')))
        if name=='nt_local_mcp_call':
            server=LOCAL_BY_KEY.get(str(args.get('server','')))
            if server is None: raise ValueError('Unknown local MCP server; use nt_local_mcp')
            if not local_enabled(self.store.settings(), server): raise ValueError('Switch on '+server['label']+' in MCP servers first')
            return self.local_client(server['key']).call(str(args.get('name','')), args.get('arguments') or {})
        if name=='nt_api':
            path=str(args.get('path',''))
            if not path.startswith('/api/'): raise ValueError('path must start with /api/')
            method=str(args.get('method') or ('POST' if args.get('body') is not None else 'GET')).upper()
            data=json.dumps(args.get('body') or {}).encode() if method=='POST' else None
            req=urllib.request.Request(f'http://127.0.0.1:{app.port}{path}',data=data,method=method,
                headers={'Authorization':'Bearer '+app.host_api_key,'Content-Type':'application/json'})
            try:
                with urllib.request.urlopen(req,timeout=float(args.get('timeout') or 60)) as r:
                    text,status=r.read(2000000).decode('utf-8','replace'),r.status
            except urllib.error.HTTPError as e:
                text,status=e.read(200000).decode('utf-8','replace'),e.code
            try: payload=json.loads(text)
            except ValueError: payload=text
            return {'status':status,'body':payload}
        account=str(args.get('account_id') or selected_account(self.store))
        if not account: raise ValueError('No harness account selected; sign in first')
        client=app.adapter.client(account)
        if name=='nt_notion_call':
            method=str(args.get('method',''))
            if not re.fullmatch(r'[A-Za-z]{3,64}',method): raise ValueError('Invalid Notion method name')
            # Validate save_to before calling Notion, so a rejected path has no side effects.
            target=self.save_target(args.get('save_to'))
            result=client.call(method,args.get('body') or {})
            return result if target is None else self.save_response(target,result)
        if name=='nt_inference_body':
            info=client.info
            model=args.get('model') or app.adapter.template['transcript'][0]['value'].get('model')
            step=app.adapter.new_id(info['space_id'],'00aa')
            return app.adapter.body(client,str(args['thread_id']),step,str(args['prompt']),model,create=bool(args.get('create')))
        if name=='nt_notion_stream':
            body=args.get('body')
            if not isinstance(body,dict): raise ValueError('body must be an object')
            limit=min(int(args.get('max_lines') or 400),3000); lines=[]
            for line in client.transport.stream('runInferenceTranscript',body,client.headers(),timeout=float(args.get('timeout') or 180)):
                if isinstance(line,bytes): line=line.decode('utf-8','replace')
                if isinstance(line,str):
                    try: line=json.loads(line)
                    except ValueError: line=line[:4000]
                lines.append(line)
                if len(lines)>=limit: lines.append('[truncated]'); break
            return {'lines':lines}
        raise ValueError('Unknown operator tool')

    # ---- long calls (MCP -32001) ------------------------------------------
    def long_jobs(self):
        jobs = getattr(self, '_long_jobs', None)
        if jobs is None:
            jobs = self._long_jobs = long_calls.Jobs()
        return jobs

    @staticmethod
    def caller_key(args, operator):
        """Who may read a job: the coworker assignment (token) or the owner connection."""
        token = str((args or {}).get('assignment_token') or '') if isinstance(args, dict) else ''
        if token:
            return 'task:' + token
        return 'operator' if operator is not None else 'chat'

    @staticmethod
    def is_job_call(name, args):
        return name in ANY_ROLE_TOOLS or (name == 'mcp_call' and isinstance(args, dict)
                                          and args.get('tool_name') in ANY_ROLE_TOOLS)

    @staticmethod
    def runs_inline(name, args):
        inner = (args or {}).get('tool_name') if name == 'mcp_call' and isinstance(args, dict) else None
        return name in INLINE_TOOLS or inner in INLINE_TOOLS

    def job_result(self, name, args, operator):
        """nt_job_result {job_id, wait_s}: read-only, every role, only the caller that started the job."""
        inner = (args.get('arguments') or {}) if name == 'mcp_call' else args
        return self.long_jobs().result(self.caller_key(args, operator), inner.get('job_id'), inner.get('wait_s'))

    def tool_value(self, name, args, operator):
        """The tools/call dispatch (runs on a long_calls job thread unless it is a write)."""
        tools = self.tools()
        if operator is not None and name in OPERATOR_NAMES:
            return self.operator_call(operator, name, args)
        if name in TOOL_SERVER:
            if operator is not None or name.endswith('_tools'):
                return self.local_call(name, args)
            raise ValueError('Calling local MCP tools from your own chat requires the Bearer token shown in Settings > Remote MCP access, or a valid worker assignment through mcp_call.')
        if name=='mcp_servers':
            value = [{'id':'NotionTool','tools':list(TOOLS),'full_pc_access':self.full_access()}]
            if self.browser_access():
                value.append({'id':'Browser','tools':list(BROWSER_TOOLS)})
            if self.screen_access():
                value.append({'id':'Screen','tools':list(SCREEN_TOOLS),'assignment_token':'not needed'})
            settings = self.store.settings()
            for server in LOCAL_SERVERS:
                if local_enabled(settings, server):
                    value.append({'id':server['label'],'tools':list(tool_specs(server))})
            return value
        if name=='mcp_list_tools':
            value = [dict(name=n,description=d,inputSchema={'type':'object','properties':p}) for n,(d,p) in tools.items()
                     if not args.get('tool_name') or n==args['tool_name']]
            if not args.get('tool_name') or args['tool_name']==long_calls.JOB_TOOL:
                spec = long_calls.JOB_TOOL_SPEC
                value.append(dict(name=spec['name'],description=spec['description'],inputSchema=json.loads(json.dumps(spec['inputSchema']))))
            return value
        if name=='mcp_call':
            inner,inner_args,token = args.get('tool_name'),args.get('arguments') or {},args.get('assignment_token','')
            # Owner connection (operator scope already has shell access): local MCP tools need no assignment token.
            discover = inner in TOOL_SERVER and inner.endswith('_tools') and not token
            return self.local_call(inner,inner_args) if inner in TOOL_SERVER and (discover or (operator is not None and not self.has_assignment(token))) else self.call(inner,inner_args,token)
        raise ValueError('Unknown MCP tool')

    def rpc(self, message, operator=None):
        ident = message.get('id')
        method,params = message.get('method'),message.get('params') or {}
        if ident is None:
            return None
        try:
            if method=='initialize':
                version=params.get('protocolVersion')
                if version not in ('2024-11-05','2025-03-26','2025-06-18'):version='2024-11-05'
                result = dict(protocolVersion=version,capabilities={'tools':{'listChanged':False}},serverInfo={'name':'NotionTool','version':'0.2.0'})
            elif method=='ping':
                result = {}
            elif method=='tools/list':
                result = {'tools':[
                    {'name':'mcp_servers','description':'List NotionTool tool groups; no worker registration is needed',
                     'annotations':{'readOnlyHint':True,'destructiveHint':False},
                     'inputSchema':{'type':'object','properties':{}}},
                    {'name':'mcp_list_tools','description':'Get schemas for a NotionTool tool by name',
                     'annotations':{'readOnlyHint':True,'destructiveHint':False},
                     'inputSchema':{'type':'object','properties':{'tool_name':{'type':'string'}}}},
                    {'name':'mcp_call','description':'Execute a NotionTool tool. Coworker tools need the assignment token from your current prompt; screen_windows and screen_capture need no token or code (leave assignment_token empty); in the owner connection the local MCP tools (roblox_*, blender_*, rea_*) need no token either. Calls longer than about 45 s return still_running with a job_id: do not start them again, fetch the result with nt_job_result',
                     'annotations':{'readOnlyHint':False,'destructiveHint':True},
                     'inputSchema':{'type':'object','properties':{'tool_name':{'type':'string'},'assignment_token':{'type':'string'},
                                   'arguments':{'type':'object'}},'required':['tool_name']}},
                    json.loads(json.dumps(long_calls.JOB_TOOL_SPEC))]}
                if operator is not None:
                    result['tools'] += OPERATOR_TOOLS
                # Publish only the two lazy entrypoints for enabled providers.
                # Discovering this catalog must not start a local MCP process.
                for server in LOCAL_SERVERS:
                    if not local_enabled(self.store.settings(), server):
                        continue
                    for name, (description, properties) in tool_specs(server).items():
                        schema = {'type': 'object', 'properties': properties}
                        if name.endswith('_call'):
                            schema['required'] = ['name']
                        result['tools'].append({'name': name, 'description': description,
                            'inputSchema': schema, 'annotations': {
                                'readOnlyHint': name.endswith('_tools'),
                                'destructiveHint': name.endswith('_call')}})
            elif method=='tools/call':
                name,args = params.get('name'),params.get('arguments') or {}
                if self.is_job_call(name, args):
                    # nt_job_result itself is never wrapped; it waits at most 40 s.
                    value = self.job_result(name, args, operator)
                elif self.runs_inline(name, args):
                    value = self.tool_value(name, args, operator)
                else:
                    # Answer before Notion's MCP request timeout (-32001): a slower call goes on as a
                    # job on one thread (call() logs tool_finished there when it really ends).
                    value = self.long_jobs().run(self.caller_key(args, operator), name, args,
                        lambda: self.tool_value(name, args, operator), long_calls.sync_limit(self.store.settings()))
                if isinstance(value, dict):
                    value = dict(value)  # a stored job result keeps its image for the next nt_job_result
                image = value.pop('_image', None) if isinstance(value, dict) else None
                result = {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}]}
                if image:
                    result['content'].append({'type':'image','data':image['data'],'mimeType':image['mimeType']})
            else:
                return {'jsonrpc':'2.0','id':ident,'error':{'code':-32601,'message':'Method not found'}}
            return {'jsonrpc':'2.0','id':ident,'result':result}
        except (ValueError,OSError,KeyError,TypeError,RuntimeError) as exc:
            return {'jsonrpc':'2.0','id':ident,'result':{'isError':True,'content':[{'type':'text','text':str(exc)[:400]}]}}
        except Exception as exc:  # noqa: BLE001 - an MCP call must always get an answer
            self.log_failure(method, params, exc)
            return {'jsonrpc':'2.0','id':ident,'result':{'isError':True,'content':[{'type':'text','text':'Internal NotionTool error ('+type(exc).__name__+'): '+str(exc)[:300]}]}}

    def log_failure(self, method, params, exc):
        import time
        import traceback
        try:
            folder = Path(getattr(self.store,'directory','.'))/'runtime'
            folder.mkdir(parents=True, exist_ok=True)
            tool = (params or {}).get('name','')
            inner = ((params or {}).get('arguments') or {}).get('tool_name','') if tool=='mcp_call' else ''
            stamp = time.strftime('%Y-%m-%d %H:%M:%S')
            text = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
            with open(folder/'mcp-errors.log','a',encoding='utf-8') as handle:
                handle.write(f'[{stamp}] {method} {tool} {inner}\n{text}\n')
        except Exception:  # noqa: BLE001 - logging must never break the answer
            pass
