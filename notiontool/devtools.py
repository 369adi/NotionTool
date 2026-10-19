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


def _schema(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required)}


S = {'type': 'string'}; I = {'type': 'integer'}; B = {'type': 'boolean'}; N = {'type': 'number'}
RO = {'readOnlyHint': True, 'destructiveHint': False}
RW = {'readOnlyHint': False, 'destructiveHint': True}
TOOLS = [
    {'name': 'nt_shell', 'description': 'Dev: run a PowerShell command on the owner PC (cwd defaults to the NotionTool app folder). Returns exit code, stdout, stderr (capped). Use for builds, tests, git, processes.', 'annotations': RW, 'inputSchema': _schema({'command': S, 'cwd': S, 'timeout': N}, ['command'])},
    {'name': 'nt_read_file', 'description': 'Dev: read a text file with line numbers (relative paths are inside the app folder). Returns sha256 for safe writes.', 'annotations': RO, 'inputSchema': _schema({'path': S, 'offset': I, 'limit': I}, ['path'])},
    {'name': 'nt_write_file', 'description': 'Dev: create, overwrite or append a utf-8 text file. Pass expected_sha256 from nt_read_file to never overwrite newer changes. A backup is kept in work/backups.', 'annotations': RW, 'inputSchema': _schema({'path': S, 'content': S, 'expected_sha256': S, 'append': B}, ['path', 'content'])},
    {'name': 'nt_edit_file', 'description': 'Dev: replace exact text in a file. Fails without changes unless old_text occurs exactly count times (default 1). Backup kept.', 'annotations': RW, 'inputSchema': _schema({'path': S, 'old_text': S, 'new_text': S, 'count': I}, ['path', 'old_text', 'new_text'])},
    {'name': 'nt_list_dir', 'description': 'Dev: list a folder; recursive mode skips build, cache and data folders.', 'annotations': RO, 'inputSchema': _schema({'path': S, 'recursive': B, 'max_entries': I})},
    {'name': 'nt_search_code', 'description': 'Codebase: regex search over source files (glob filter like *.py). Returns path:line: text.', 'annotations': RO, 'inputSchema': _schema({'pattern': S, 'path': S, 'glob': S, 'ignore_case': B, 'max_results': I}, ['pattern'])},
    {'name': 'nt_file_outline', 'description': 'Codebase: classes, functions and methods of a .py file, or functions and element ids of a .js/.html file, with line numbers.', 'annotations': RO, 'inputSchema': _schema({'path': S}, ['path'])},
    {'name': 'nt_memory_read', 'description': 'Project memory: read the durable notes of a project (default NotionTool): goals, architecture, decisions, status, open tasks. project="*" lists projects.', 'annotations': RO, 'inputSchema': _schema({'project': S})},
    {'name': 'nt_memory_update', 'description': 'Project memory: replace (or append to) one "## section" of a project memory file. Update after every significant change.', 'annotations': {'readOnlyHint': False, 'destructiveHint': False}, 'inputSchema': _schema({'project': S, 'section': S, 'content': S, 'append': B}, ['section', 'content'])},
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
        timeout = min(float(args.get('timeout') or 120), 1800)
        cwd = _path(args.get('cwd') or '.')
        if os.name == 'nt':
            cmd = ['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', '[Console]::OutputEncoding=[Text.Encoding]::UTF8;' + str(args['command'])]
            flags = subprocess.CREATE_NO_WINDOW
        else:
            cmd, flags = ['sh', '-c', str(args['command'])], 0
        try:
            env = {k: v for k, v in os.environ.items() if not k.startswith('_PYI') and k != '_MEIPASS2'}
            env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'  # started programs get their own, fresh bundle state
            r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, timeout=timeout, creationflags=flags, env=env)
        except subprocess.TimeoutExpired as e:
            out, _ = _cap((e.stdout or b'').decode('utf-8', 'replace'))
            return {'exit_code': None, 'timed_out': True, 'stdout': out, 'stderr': ''}
        out, t1 = _cap(r.stdout.decode('utf-8', 'replace'))
        err, t2 = _cap(r.stderr.decode('utf-8', 'replace'), 20000)
        return {'exit_code': r.returncode, 'stdout': out, 'stderr': err, 'truncated': t1 or t2, 'cwd': str(cwd)}
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
        text = p.read_text(encoding='utf-8')
        old, want = str(args['old_text']), int(args.get('count') or 1)
        found = text.count(old) if old else 0
        if found != want:
            raise ValueError(f'old_text found {found} times, expected {want}; nothing changed')
        backup = _backup(p)
        with open(p, 'w', encoding='utf-8', newline='') as f:
            f.write(text.replace(old, str(args['new_text'])))
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
    if name == 'nt_restart_app':
        script = ROOT / 'work' / 'restart-notiontool.ps1'
        if not script.is_file():
            raise ValueError('work/restart-notiontool.ps1 is missing')
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
        subprocess.Popen(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-Command', f"Start-Sleep 2; & '{script}'"], cwd=str(ROOT), creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        return {'restarting': True, 'note': 'Reconnect in about 15 seconds.'}
    raise ValueError('Unknown dev tool')
