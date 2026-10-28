"""Operator developer tools exposed over the NotionTool MCP (Host API bearer only)."""
import ast, fnmatch, hashlib, os, re, shutil, subprocess, sys, time
from pathlib import Path

def _root():
    # Frozen EXE built into <project>/dist: use the project folder so backups and memory stay out of dist.
    if not getattr(sys, 'frozen', False):
        return Path(__file__).resolve().parent.parent
    exe = Path(sys.executable).resolve().parent
    parent = exe.parent
    if exe.name.lower() == 'dist' and (parent / 'notiontool').is_dir() and (parent / 'project-memory').is_dir():
        return parent
    return exe


ROOT = _root()
SKIP = {'.git', 'node_modules', '__pycache__', 'build', 'dist', '.venv', 'venv', '.test-data', 'data', 'browser-profiles', '.pytest_cache', 'backups'}
MEMORY = ROOT / 'project-memory'
LIMIT = 60000
BINARY = ('.exe', '.dll', '.pyd', '.png', '.jpg', '.jpeg', '.ico', '.zip', '.db', '.pyc', '.woff', '.woff2', '.gif', '.pdf')


# nt_shell guard: Windows Defender flags long inline code in PowerShell commands (here-strings piped to
# python, hidden cmd /c launches) as Trojan:Win32/Commando.A!ml. Such commands are rejected before running.
SHELL_BLOCKED = ('Blocked: write files with nt_write_file and run tests with python -m pytest directly. '
                 'Long inline code in shell commands triggers antivirus false alarms.')
INLINE_MAX = 400  # characters of inline code a shell command may carry
_HERE_OPEN = re.compile(r'@([\'"])[ \t]*\r?\n')
_PY = r'py(?:thon[0-9.]*)?(?:\.exe)?'
_PY_C = re.compile(r'(?<![\w-])' + _PY + r'[\'"]?\s+(?:-\w+\s+)*-c\b', re.I)
_PY_PIPE = re.compile(r'\|\s*(?:&\s*)?[\'"]?(?:[^\s|;\'"]*[\\/])?' + _PY + r'[\'"]?\s+-(?=\s|$|[|;)])', re.I)
_START = re.compile(r'\bStart-Process\b', re.I)
_HIDDEN = re.compile(r'-WindowStyle\s+[\'"]?Hidden\b', re.I)
_CMD_C = re.compile(r'\bcmd(?:\.exe)?\b[^;\n]*?(?<![\w./\\])/[ck]\b', re.I)
_PS_CMD = re.compile(r'\b(?:powershell|pwsh)(?:\.exe)?\b.*?(?<![\w-])-(?:c|command|e|ec|enc|encodedcommand)\b', re.I | re.S)


# Opening an image/video in a desktop app pops up the Photos app on the owner PC (User 2026-10-23).
IMAGE_OPEN_BLOCKED = ('Blocked: never open images/videos in a desktop app (Photos pops up on the owner PC). '
                      'Check renders inside Blender (bpy.data.images pixel stats) or upload them so they return a view_url '
                      'and open that with viewFileUrl instead.')
_IMG_OPEN = re.compile(r'\b(?:Start-Process|saps|Invoke-Item|ii|start|explorer(?:\.exe)?|mspaint(?:\.exe)?)\b'
                       r'[^;|\n]*\.(?:png|jpe?g|webp|bmp|gif|tiff?|exr|mp4)\b', re.I)


def shell_guard(command):
    """Block opening image files in desktop apps, then apply the antivirus guard."""
    if _IMG_OPEN.search(str(command or '')):
        return IMAGE_OPEN_BLOCKED
    return _shell_guard_base(command)


def _shell_guard_base(command):
    """Return SHELL_BLOCKED for command shapes antivirus flags as droppers, else None."""
    c = str(command or '')
    for m in _HERE_OPEN.finditer(c):
        end = re.search(r'\r?\n' + m.group(1) + '@', c[m.end():])
        body = c[m.end():m.end() + end.start()] if end else c[m.end():]
        if len(body) > INLINE_MAX:
            return SHELL_BLOCKED
    m = _PY_C.search(c)
    if m and len(c[m.end():].strip()) > INLINE_MAX:
        return SHELL_BLOCKED
    m = _PY_PIPE.search(c)
    if m and len(c[:m.start()].strip()) > INLINE_MAX:
        return SHELL_BLOCKED
    if _START.search(c) and _HIDDEN.search(c) and (_CMD_C.search(c) or _PS_CMD.search(c)):
        return SHELL_BLOCKED
    return None


def _schema(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required)}


S = {'type': 'string'}; I = {'type': 'integer'}; B = {'type': 'boolean'}; N = {'type': 'number'}
RO = {'readOnlyHint': True, 'destructiveHint': False}
RW = {'readOnlyHint': False, 'destructiveHint': True}
TOOLS = [
    {'name': 'nt_shell', 'description': 'Dev: run a PowerShell command on the owner PC (cwd defaults to the NotionTool app folder). Returns exit code, stdout, stderr (capped). Use for builds, tests, git, processes. Calls longer than about 45 s return still_running with a job_id: do not start them again, fetch the result with nt_job_result (timeout stays the hard kill, default 120, max 1800). Files must be written with nt_write_file, never with inline code in the command: long here-strings, code piped into python and hidden cmd /c launches are blocked because they trigger antivirus false alarms.', 'annotations': RW, 'inputSchema': _schema({'command': S, 'cwd': S, 'timeout': N}, ['command'])},
    {'name': 'nt_read_file', 'description': 'Dev: read a text file with line numbers (relative paths are inside the app folder). Returns sha256 for safe writes.', 'annotations': RO, 'inputSchema': _schema({'path': S, 'offset': I, 'limit': I}, ['path'])},
    {'name': 'nt_write_file', 'description': 'Dev: create, overwrite or append a utf-8 text file. Pass expected_sha256 from nt_read_file to never overwrite newer changes. A backup is kept in work/backups.', 'annotations': RW, 'inputSchema': _schema({'path': S, 'content': S, 'expected_sha256': S, 'append': B}, ['path', 'content'])},
    {'name': 'nt_edit_file', 'description': 'Dev: replace exact text in a file. Fails without changes unless old_text occurs exactly count times (default 1). Backup kept.', 'annotations': RW, 'inputSchema': _schema({'path': S, 'old_text': S, 'new_text': S, 'count': I}, ['path', 'old_text', 'new_text'])},
    {'name': 'nt_list_dir', 'description': 'Dev: list a folder; recursive mode skips build, cache and data folders.', 'annotations': RO, 'inputSchema': _schema({'path': S, 'recursive': B, 'max_entries': I})},
    {'name': 'nt_search_code', 'description': 'Codebase: regex search over source files (glob filter like *.py). Returns path:line: text.', 'annotations': RO, 'inputSchema': _schema({'pattern': S, 'path': S, 'glob': S, 'ignore_case': B, 'max_results': I}, ['pattern'])},
    {'name': 'nt_file_outline', 'description': 'Codebase: classes, functions and methods of a .py file, or functions and element ids of a .js/.html file, with line numbers.', 'annotations': RO, 'inputSchema': _schema({'path': S})},
    {'name': 'nt_find_symbol', 'description': 'Codebase: find a Python symbol (function, class or method, optionally Class.method) in one call: its definitions, callers and importers as path:line. Uses a per-worktree index that re-parses changed files before each query, so it is never stale.', 'annotations': RO, 'inputSchema': _schema({'name': S, 'path': S, 'max_results': I}, ['name'])},
    {'name': 'nt_memory_read', 'description': 'Project memory: read the durable notes of a project (default NotionTool): goals, architecture, decisions, status, open tasks. project="*" lists projects.', 'annotations': RO, 'inputSchema': _schema({'project': S})},
    {'name': 'nt_memory_update', 'description': 'Project memory: replace (or append to) one "## section" of a project memory file. Update after every significant change.', 'annotations': {'readOnlyHint': False, 'destructiveHint': False}, 'inputSchema': _schema({'project': S, 'section': S, 'content': S, 'append': B}, ['section', 'content'])},
    {'name': 'nt_test_app', 'description': 'Test app: start an isolated second NotionTool from the CURRENT source code (hidden, own empty data folder, port 8791, no Notion accounts, the real app is never touched) to prove a code fix really works before it reaches the user. action=check (default): start, wait, collect startup errors and tracebacks, stop; verdict passed or failed. start/status/stop keep it running for manual HTTP tests on the returned port. RULE: every NotionTool code change must pass nt_test_app check (plus its pytest tests) before it is reported done; a failed verdict means the task is not done.', 'annotations': RW, 'inputSchema': _schema({'action': {'type': 'string', 'enum': ['check', 'start', 'status', 'stop']}, 'port': I, 'keep': B})},
    {'name': 'nt_restart_app', 'description': 'Dev: restart this NotionTool instance visibly after code changes. The MCP connection drops for about 15 s.', 'annotations': RW, 'inputSchema': _schema({})},
]
NAMES = {t['name'] for t in TOOLS}


def _path(p):
    q = Path(os.path.expandvars(str(p or '.')))
    return (q if q.is_absolute() else ROOT / q).resolve()


def _cap(text, limit=LIMIT):
    text = text or ''
    if len(text) <= limit:
        return text, False
    half = limit // 2
    return text[:half] + f'\n... [{len(text) - limit} chars omitted] ...\n' + text[-half:], True


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _backup(path):
    if not path.is_file():
        return ''
    try:
        rel = path.relative_to(ROOT)
    except ValueError:
        rel = Path('external') / path.relative_to(path.anchor)
    dest = ROOT / 'work' / 'backups' / time.strftime('%Y%m%d-%H%M%S') / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    return str(dest)


def _walk(base, pattern='*'):
    if base.is_file():
        yield base
        return
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            if fnmatch.fnmatch(f, pattern):
                yield Path(root) / f


def _memory_file(project):
    name = re.sub(r'[^A-Za-z0-9._-]+', '-', str(project or 'NotionTool')).strip('-') or 'NotionTool'
    return MEMORY / (name + '.md')


def _rel(f):
    try:
        return str(f.relative_to(ROOT))
    except ValueError:
        return str(f)


def call(name, args):
    args = args or {}
    if name == 'nt_shell':
        blocked = shell_guard(args.get('command'))
        if blocked:
            raise ValueError(blocked)
        timeout = min(float(args.get('timeout') or 120), 1800)
        cwd = _path(args.get('cwd') or '.')
        if os.name == 'nt':
            cmd = ['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', '[Console]::OutputEncoding=[Text.Encoding]::UTF8;' + str(args['command'])]
            flags = subprocess.CREATE_NO_WINDOW
        else:
            cmd, flags = ['sh', '-c', str(args['command'])], 0
        env = {k: v for k, v in os.environ.items() if not k.startswith('_PYI') and k != '_MEIPASS2'}
        env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'  # started programs get their own, fresh bundle state
        p = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags, env=env)
        # subprocess.run(timeout) waited without a limit for pipes a grandchild kept open after the kill and
        # dropped the output ({exit_code: null, timed_out: true}, empty). Bounded, output kept (long_calls).
        from . import long_calls
        raw_out, raw_err, timed_out = long_calls.communicate(p, timeout, flags, long_calls.output_sink())
        out, t1 = _cap(raw_out.decode('utf-8', 'replace'))
        err, t2 = _cap(raw_err.decode('utf-8', 'replace'), 20000)
        if timed_out:
            return {'exit_code': None, 'timed_out': True, 'stdout': out, 'stderr': err, 'truncated': t1 or t2,
                    'cwd': str(cwd), 'error': 'Command timed out after %d s; its process tree was stopped. '
                                              'stdout/stderr hold the output written until then.' % timeout}
        return {'exit_code': p.returncode, 'stdout': out, 'stderr': err, 'truncated': t1 or t2, 'cwd': str(cwd)}
    if name == 'nt_read_file':
        p = _path(args['path'])
        data = p.read_bytes()
        lines = data.decode('utf-8', 'replace').split('\n')
        off = max(int(args.get('offset') or 1), 1)
        lim = min(int(args.get('limit') or 400), 3000)
        chunk = lines[off - 1:off - 1 + lim]
        text, cut = _cap('\n'.join(f'{off + i}: {l}' for i, l in enumerate(chunk)))
        return {'path': str(p), 'sha256': _sha(data), 'total_lines': len(lines), 'from': off, 'to': off + len(chunk) - 1, 'content': text, 'truncated': cut}
    if name == 'nt_write_file':
        p = _path(args['path'])
        expected = args.get('expected_sha256')
        if expected and p.is_file() and _sha(p.read_bytes()) != expected:
            raise ValueError('File changed since it was read (sha256 mismatch); read it again')
        backup = _backup(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        content = str(args['content'])
        with open(p, 'a' if args.get('append') else 'w', encoding='utf-8', newline='') as f:
            f.write(content)
        return {'path': str(p), 'sha256': _sha(p.read_bytes()), 'backup': backup, 'bytes': len(content.encode('utf-8'))}
    if name == 'nt_edit_file':
        p = _path(args['path'])
        raw_text = p.read_bytes().decode('utf-8')
        crlf = '\r\n' in raw_text  # keep the file's own line endings (CRLF files stay CRLF)
        text = raw_text.replace('\r\n', '\n') if crlf else raw_text
        old, want = str(args['old_text']), int(args.get('count') or 1)
        if crlf:
            old = old.replace('\r\n', '\n')
        found = text.count(old) if old else 0
        if found != want:
            raise ValueError(f'old_text found {found} times, expected {want}; nothing changed')
        backup = _backup(p)
        result = text.replace(old, str(args['new_text']).replace('\r\n', '\n') if crlf else str(args['new_text']))
        if crlf:
            result = result.replace('\n', '\r\n')
        with open(p, 'w', encoding='utf-8', newline='') as f:
            f.write(result)
        return {'path': str(p), 'replacements': found, 'sha256': _sha(p.read_bytes()), 'backup': backup}
    if name == 'nt_list_dir':
        p = _path(args.get('path') or '.')
        cap = min(int(args.get('max_entries') or 500), 5000)
        out = []
        if args.get('recursive'):
            for f in _walk(p):
                out.append(str(f.relative_to(p)))
                if len(out) >= cap:
                    break
        else:
            out = [f.name + ('/' if f.is_dir() else '') for f in sorted(p.iterdir())]
        return {'path': str(p), 'entries': out[:cap], 'truncated': len(out) >= cap}
    if name == 'nt_search_code':
        rx = re.compile(str(args['pattern']), re.I if args.get('ignore_case') else 0)
        cap = min(int(args.get('max_results') or 200), 2000)
        hits = []
        for f in _walk(_path(args.get('path') or '.'), str(args.get('glob') or '*')):
            if f.suffix.lower() in BINARY:
                continue
            try:
                if f.stat().st_size > 3000000:
                    continue
                for i, line in enumerate(f.read_text(encoding='utf-8', errors='replace').split('\n'), 1):
                    if rx.search(line):
                        hits.append(f'{_rel(f)}:{i}: {line.strip()[:240]}')
                        if len(hits) >= cap:
                            return {'results': hits, 'truncated': True}
            except OSError:
                continue
        return {'results': hits, 'truncated': False}
    if name == 'nt_file_outline':
        p = _path(args['path'])
        text = p.read_text(encoding='utf-8', errors='replace')
        items = []
        if p.suffix == '.py':
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    items.append((node.lineno, ('class ' if isinstance(node, ast.ClassDef) else 'def ') + node.name))
        else:
            rx = re.compile(r'(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>|\bid="([^"]+)"')
            for i, line in enumerate(text.split('\n'), 1):
                for m in rx.finditer(line):
                    items.append((i, next(g for g in m.groups() if g)))
        items.sort()
        return {'path': str(p), 'outline': [f'{n}: {s}' for n, s in items[:3000]]}
    if name == 'nt_memory_read':
        MEMORY.mkdir(parents=True, exist_ok=True)
        if args.get('project') == '*':
            return {'projects': sorted(f.stem for f in MEMORY.glob('*.md'))}
        f = _memory_file(args.get('project'))
        return {'project': f.stem, 'path': str(f), 'content': f.read_text(encoding='utf-8') if f.is_file() else ''}
    if name == 'nt_memory_update':
        MEMORY.mkdir(parents=True, exist_ok=True)
        f = _memory_file(args.get('project'))
        text = f.read_text(encoding='utf-8') if f.is_file() else f'# {f.stem} project memory\n'
        text = re.sub(r'(?m)^_Last updated: .*\n', '', text)
        head = '## ' + str(args['section']).strip()
        body = str(args['content']).strip()
        parts = re.split(r'(?m)^(?=## )', text)
        found = False
        for i, part in enumerate(parts):
            if part.split('\n', 1)[0].strip() == head:
                old = part.split('\n', 1)[1].strip() if '\n' in part else ''
                parts[i] = head + '\n' + ((old + '\n' + body) if args.get('append') and old else body) + '\n\n'
                found = True
        if not found:
            parts.append(('' if parts[-1].endswith('\n\n') else '\n') + head + '\n' + body + '\n\n')
        lines = ''.join(parts).rstrip().split('\n')
        lines.insert(1, '_Last updated: ' + time.strftime('%Y-%m-%d %H:%M') + '_')
        f.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return {'project': f.stem, 'section': head[3:], 'path': str(f)}
    if name == 'nt_test_app':
        from . import test_app
        return test_app.run(args.get('action') or 'check', port=args.get('port'), keep=args.get('keep'))
    if name == 'nt_restart_app':
        script = ROOT / 'work' / 'restart-notiontool.ps1'
        if not script.is_file():
            raise ValueError('work/restart-notiontool.ps1 is missing')
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
        subprocess.Popen(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-Command', f"Start-Sleep 2; & '{script}'"], cwd=str(ROOT), creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        return {'restarting': True, 'note': 'Reconnect in about 15 seconds.'}
    if name == 'nt_find_symbol':
        # Read-only: the per-worktree index lives in the git admin folder (or temp), never in data/.
        from . import code_index
        base = _path(args.get('path') or '.')
        if base.is_file():
            base = base.parent
        return code_index.find_symbol(base, args['name'], limit=min(int(args.get('max_results') or 50), 500))
    raise ValueError('Unknown dev tool')
