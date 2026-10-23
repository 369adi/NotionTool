"""Code index (rank 8): symbols, imports and call names of every .py file of one worktree.

Each worktree root gets its own SQLite file. It sits in that worktree's git admin folder
(`git rev-parse --git-path`), outside the project tree and never in data/notiontool.db; outside git
it goes to the temp folder. Entries live in an FTS5 table with the trigram tokenizer; when FTS5 (or
its trigram tokenizer) is missing, a plain table searched with = and LIKE is used instead.

Before every query the index stats all .py files and re-parses only those whose mtime or size
changed (deleted files are dropped), so it is never stale after a git checkout or an edit.

repo_map() ranks files with PageRank over import and call edges and scores every definition by
the rank of the files that call or import it. context_pack uses it to fill its char budget with the
most connected symbols of a task's larger files. Standard library only.
"""
import ast
import hashlib
import os
import sqlite3
import subprocess
import tempfile
import threading
import warnings

SCHEMA = '1'
DB_PREFIX = 'notiontool-code-index-'
SKIP = frozenset({'.git', 'node_modules', '__pycache__', 'build', 'dist', '.venv', 'venv', '.test-data', 'data',
                  'browser-profiles', '.pytest_cache', 'backups', 'work', '.mypy_cache', '.ruff_cache',
                  '.hypothesis', '.tox', 'site-packages'})
MAX_FILE_BYTES = 2000000
DAMPING = 0.85
ITERATIONS = 50
CALL_FANOUT = 5  # a call name defined in more files than this is too generic to count as an edge
SAME_FILE_WEIGHT = 0.25
DEFINITIONS = ('def', 'class')
COLUMNS = 'name, kind, path, line, end_line, scope, target'
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_DB_PATHS = {}


def _lock(key):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def db_path(root):
    """Per-worktree index file: in the worktree's git admin folder, else in the temp folder."""
    root = os.path.realpath(str(root))
    if root in _DB_PATHS:
        return _DB_PATHS[root]
    name = DB_PREFIX + hashlib.sha1(os.path.normcase(root).encode('utf-8')).hexdigest()[:16] + '.sqlite'
    path = None
    try:
        done = subprocess.run(['git', 'rev-parse', '--git-path', name], cwd=root, capture_output=True, text=True,
                              timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        out = (done.stdout or '').strip()
        if done.returncode == 0 and out:
            path = os.path.realpath(os.path.join(root, out))
    except (OSError, subprocess.SubprocessError):
        path = None
    if not path:
        path = os.path.join(tempfile.gettempdir(), 'notiontool-code-index', name)
    _DB_PATHS[root] = path
    return path


def iter_py(root):
    """(relative path with '/', full path) of every .py file below root, skipping build/data/work folders."""
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith('.'))
        for name in sorted(files):
            if name.endswith('.py'):
                full = os.path.join(base, name)
                yield os.path.relpath(full, root).replace(os.sep, '/'), full


def module_name(rel):
    parts = rel[:-3].split('/') if rel.endswith('.py') else rel.split('/')
    if parts and parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(p for p in parts if p)


class _Visitor(ast.NodeVisitor):
    """Rows (name, kind, line, end_line, scope, target); kind is def, class, import or call."""

    def __init__(self, rel):
        module = module_name(rel)
        self.package = module if rel.endswith('__init__.py') else module.rpartition('.')[0]
        self.scope = []
        self.rows = []

    def _define(self, node, kind):
        qual = '.'.join(self.scope + [node.name])
        self.rows.append((node.name, kind, node.lineno, getattr(node, 'end_lineno', None) or node.lineno, qual, ''))
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node):
        self._define(node, 'def')

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._define(node, 'class')

    def visit_Import(self, node):
        for alias in node.names:
            self.rows.append((alias.name, 'import', node.lineno, node.lineno, '.'.join(self.scope), alias.name))

    def visit_ImportFrom(self, node):
        base = node.module or ''
        if node.level:
            package = self.package.split('.') if self.package else []
            package = package[:max(0, len(package) - (node.level - 1))]
            base = '.'.join(p for p in package + [node.module or ''] if p)
        for alias in node.names:
            self.rows.append((alias.name, 'import', node.lineno, node.lineno, '.'.join(self.scope), base))

    def visit_Call(self, node):
        func, target = node.func, ''
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            target = func.value.id
        if name:
            self.rows.append((name, 'call', node.lineno, node.lineno, '.'.join(self.scope) or '<module>', target))
        self.generic_visit(node)


def parse_source(text, rel='module.py'):
    """Return (rows, error) for one source text; a syntax error gives no rows."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            tree = ast.parse(text)
    except (SyntaxError, ValueError) as error:
        return [], ('%s: %s' % (type(error).__name__, error))[:300]
    visitor = _Visitor(rel)
    visitor.visit(tree)
    return visitor.rows, None


def _parse_file(full, rel):
    try:
        with open(full, 'rb') as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as error:
        return [], 0, str(error)[:300]
    if len(data) > MAX_FILE_BYTES:
        return [], 0, 'file too large to index'
    text = data.decode('utf-8', 'replace')
    rows, error = parse_source(text, rel)
    return rows, text.count('\n') + 1, error


def fts5_available(conn):
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._nt_probe USING fts5(x, tokenize='trigram')")
        conn.execute('DROP TABLE temp._nt_probe')
        return True
    except sqlite3.Error:
        return False


def _setup(conn, want_fts):
    conn.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
    meta = dict(conn.execute('SELECT key, value FROM meta').fetchall())
    mode = 'fts5' if want_fts and fts5_available(conn) else 'like'
    if meta.get('schema') == SCHEMA and meta.get('mode') == mode:
        return mode
    conn.execute('DROP TABLE IF EXISTS entries')
    conn.execute('DROP TABLE IF EXISTS files')
    if mode == 'fts5':
        conn.execute('CREATE VIRTUAL TABLE entries USING fts5(name, kind UNINDEXED, path UNINDEXED, line UNINDEXED, '
                     "end_line UNINDEXED, scope UNINDEXED, target UNINDEXED, tokenize='trigram')")
    else:
        conn.execute('CREATE TABLE entries (name TEXT, kind TEXT, path TEXT, line INTEGER, end_line INTEGER, '
                     'scope TEXT, target TEXT)')
        conn.execute('CREATE INDEX entries_name ON entries(name)')
        conn.execute('CREATE INDEX entries_path ON entries(path)')
    conn.execute('CREATE TABLE files (path TEXT PRIMARY KEY, module TEXT, mtime_ns INTEGER, size INTEGER, '
                 'lines INTEGER, error TEXT)')
    conn.executemany('INSERT OR REPLACE INTO meta VALUES (?, ?)', [('schema', SCHEMA), ('mode', mode)])
    conn.commit()
    return mode


def _like_escape(text):
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _hit(path, line, text):
    return '%s:%d: %s' % (path, int(line or 0), text)


class CodeIndex:
    """Index of one worktree root. Every public query refreshes changed files first."""

    def __init__(self, root, db=None, fts=True):
        self.root = os.path.realpath(str(root))
        self.db = os.path.realpath(str(db)) if db else db_path(self.root)
        if os.path.basename(self.db).lower() == 'notiontool.db':
            raise ValueError('the code index never uses the main store notiontool.db')
        self.fts = bool(fts)
        self.mode = None
        self.last = {}

    def _open(self):
        os.makedirs(os.path.dirname(self.db), exist_ok=True)
        conn = sqlite3.connect(self.db, timeout=15)
        self.mode = _setup(conn, self.fts)
        return conn

    def _run(self, work):
        with _lock(self.db):
            conn = self._open()
            try:
                self._refresh(conn)
                return work(conn)
            finally:
                conn.close()

    def _refresh(self, conn):
        known = {row[0]: (row[1], row[2]) for row in conn.execute('SELECT path, mtime_ns, size FROM files')}
        seen, changed = {}, []
        for rel, full in iter_py(self.root):
            try:
                stat = os.stat(full)
            except OSError:
                continue
            seen[rel] = (stat.st_mtime_ns, stat.st_size)
            if known.get(rel) != seen[rel]:
                changed.append((rel, full))
        removed = [rel for rel in known if rel not in seen]
        if changed or removed:
            with conn:
                gone = [rel for rel, _ in changed if rel in known] + removed
                for start in range(0, len(gone), 400):
                    chunk = gone[start:start + 400]
                    marks = ','.join('?' * len(chunk))
                    conn.execute('DELETE FROM entries WHERE path IN (%s)' % marks, chunk)
                    conn.execute('DELETE FROM files WHERE path IN (%s)' % marks, chunk)
                for rel, full in changed:
                    rows, lines, error = _parse_file(full, rel)
                    conn.executemany('INSERT INTO entries (%s) VALUES (?, ?, ?, ?, ?, ?, ?)' % COLUMNS,
                                     [(n, k, rel, a, b, s, t) for n, k, a, b, s, t in rows])
                    conn.execute('INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?)',
                                 (rel, module_name(rel), seen[rel][0], seen[rel][1], lines, error))
        self.last = dict(files=len(seen), changed=len(changed), removed=len(removed), mode=self.mode)
        return self.last

    def refresh(self):
        """Re-parse files whose mtime or size changed; return dict(files, changed, removed, mode)."""
        return self._run(lambda conn: dict(self.last))

    def _named(self, conn, short):
        """Rows whose name is short or ends with '.short' (dotted import names)."""
        if self.mode == 'fts5' and len(short) >= 3:
            query = 'name : "%s"' % short.replace('"', '""')
            rows = conn.execute('SELECT %s FROM entries WHERE entries MATCH ?' % COLUMNS, (query,)).fetchall()
        else:
            rows = conn.execute("SELECT %s FROM entries WHERE name = ? OR name LIKE ? ESCAPE '\\'" % COLUMNS,
                                (short, '%.' + _like_escape(short))).fetchall()
        return [row for row in rows if row[0] == short or row[0].endswith('.' + short)]

    def find_symbol(self, name, limit=50):
        """Definitions, callers and importers of one symbol (also 'Class.method') in one call."""
        name = str(name or '').strip()
        if not name:
            raise ValueError('name is required')
        limit = max(1, min(int(limit or 50), 500))
        owner, _, short = name.rpartition('.')

        def work(conn):
            rows = self._named(conn, short)
            defs = [r for r in rows if r[1] in DEFINITIONS and r[0] == short
                    and (not owner or r[5] == name or r[5].endswith('.' + name))]
            calls = [r for r in rows if r[1] == 'call' and r[0] == short]
            imports = [r for r in rows if r[1] == 'import']
            return defs, calls, imports

        defs, calls, imports = self._run(work)
        order = lambda r: (r[2], int(r[3] or 0))
        definitions = [_hit(r[2], r[3], '%s %s (lines %s-%s)' % (r[1], r[5], r[3], r[4])) for r in sorted(defs, key=order)]
        callers = [_hit(r[2], r[3], 'in %s' % (r[5] or '<module>')) for r in sorted(calls, key=order)]
        importers = [_hit(r[2], r[3], ('import %s' % r[0]) if r[6] == r[0] else 'from %s import %s' % (r[6] or '.', r[0]))
                     for r in sorted(imports, key=order)]
        truncated = any(len(items) > limit for items in (definitions, callers, importers))
        return dict(name=name, root=self.root, index=dict(self.last),
                    counts=dict(definitions=len(definitions), callers=len(callers), importers=len(importers)),
                    definitions=definitions[:limit], callers=callers[:limit], importers=importers[:limit],
                    truncated=truncated)

    def search(self, text, limit=50):
        """Definitions whose name contains text (trigram MATCH or LIKE)."""
        text = str(text or '').strip()
        if not text:
            raise ValueError('text is required')

        def work(conn):
            if self.mode == 'fts5' and len(text) >= 3:
                rows = conn.execute('SELECT %s FROM entries WHERE entries MATCH ?' % COLUMNS,
                                    ('name : "%s"' % text.replace('"', '""'),)).fetchall()
            else:
                rows = conn.execute("SELECT %s FROM entries WHERE name LIKE ? ESCAPE '\\'" % COLUMNS,
                                    ('%' + _like_escape(text) + '%',)).fetchall()
            low = text.lower()
            return [r for r in rows if r[1] in DEFINITIONS and low in r[0].lower()]

        rows = sorted(self._run(work), key=lambda r: (r[2], int(r[3] or 0)))
        return dict(results=[_hit(r[2], r[3], '%s %s' % (r[1], r[5])) for r in rows[:limit]],
                    truncated=len(rows) > limit, index=dict(self.last))

    def repo_map(self, focus=(), limit=200):
        """PageRank over import and call edges: dict(files=[(path, rank)], symbols=[...]) best first."""
        def work(conn):
            rows = conn.execute('SELECT %s FROM entries' % COLUMNS).fetchall()
            modules = dict(conn.execute('SELECT module, path FROM files').fetchall())
            return rows, modules

        rows, modules = self._run(work)
        return build_repo_map(rows, modules, focus, limit)


def _resolve_module(modules, target, name):
    """Repo file for an import (target module, imported name) or None for outside modules."""
    if target and name and target != name and (target + '.' + name) in modules:
        return modules[target + '.' + name]
    parts = (target or name or '').split('.')
    while parts:
        module = '.'.join(parts)
        if module in modules:
            return modules[module]
        parts.pop()
    return None


def pagerank(nodes, edges, damping=DAMPING, iterations=ITERATIONS):
    """Weighted PageRank; edges is {(a, b): weight}. Dangling rank is spread evenly."""
    nodes = list(nodes)
    if not nodes:
        return {}
    count = len(nodes)
    out = {}
    for (a, b), weight in edges.items():
        if a != b and weight > 0:
            out.setdefault(a, {})[b] = out.get(a, {}).get(b, 0.0) + weight
    totals = {a: sum(targets.values()) for a, targets in out.items()}
    rank = dict.fromkeys(nodes, 1.0 / count)
    for _ in range(iterations):
        dangling = sum(rank[v] for v in nodes if v not in totals)
        base = (1.0 - damping) / count + damping * dangling / count
        new = dict.fromkeys(nodes, base)
        for a, targets in out.items():
            share = damping * rank.get(a, 0.0) / totals[a]
            for b, weight in targets.items():
                if b in new:
                    new[b] += share * weight
        delta = sum(abs(new[v] - rank[v]) for v in nodes)
        rank = new
        if delta < 1e-9:
            break
    return rank


def build_repo_map(rows, modules, focus=(), limit=200):
    """Rank files (PageRank) and their definitions (sum of referencing file ranks)."""
    files = sorted(set(modules.values()))
    defined = {}
    for name, kind, path, *_ in rows:
        if kind in DEFINITIONS and not (name.startswith('__') and name.endswith('__')):
            defined.setdefault(name, set()).add(path)
    edges, refs = {}, []  # refs: (from file, to file, name, weight)
    for name, kind, path, line, end, scope, target in rows:
        if kind == 'import':
            short = name.rpartition('.')[2]
            other = _resolve_module(modules, target, name)
            if other and other != path:
                edges[(path, other)] = edges.get((path, other), 0.0) + 1.0
                refs.append((path, other, short, 1.0))
        elif kind == 'call':
            owners = defined.get(name)
            if not owners or len(owners) > CALL_FANOUT:
                continue
            weight = 1.0 / len(owners)
            for other in owners:
                if other != path:
                    edges[(path, other)] = edges.get((path, other), 0.0) + weight
                refs.append((path, other, name, weight))
    rank = pagerank(files, edges)
    score = {}
    for source, other, name, weight in refs:
        factor = SAME_FILE_WEIGHT * rank.get(other, 0.0) if source == other else rank.get(source, 0.0)
        score[(other, name)] = score.get((other, name), 0.0) + factor * weight
    focus = {str(p).replace(os.sep, '/').lstrip('./') if str(p).startswith('./') else str(p).replace(os.sep, '/')
             for p in (focus or ())}
    symbols = []
    for name, kind, path, line, end, scope, target in rows:
        if kind not in DEFINITIONS or (focus and path not in focus):
            continue
        value = score.get((path, name), 0.0) + 0.01 * rank.get(path, 0.0)
        symbols.append(dict(path=path, name=name, qualname=scope, kind=kind, line=int(line or 0),
                            end_line=int(end or line or 0), score=value))
    symbols.sort(key=lambda s: (-s['score'], s['path'], s['line']))
    ranked = sorted(rank.items(), key=lambda item: (-item[1], item[0]))
    return dict(files=ranked[:limit] if limit else ranked, symbols=symbols[:limit] if limit else symbols,
                edges=len(edges))


def find_symbol(root, name, limit=50, fts=True):
    return CodeIndex(root, fts=fts).find_symbol(name, limit)


def refresh(root, fts=True):
    return CodeIndex(root, fts=fts).refresh()


def repo_map(root, focus=(), limit=200, fts=True):
    return CodeIndex(root, fts=fts).repo_map(focus, limit)
