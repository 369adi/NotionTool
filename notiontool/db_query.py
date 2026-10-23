"""Safe read-only SQL on the NotionTool database for coworkers (tools db_query and db_schema).

run_query(db_path, sql, limit=50) opens the file with mode=ro and PRAGMA query_only=ON, allows exactly one
SELECT or WITH statement, caps the rows (default 50, max 500), each cell (300 characters) and the whole
text (6000 characters), and stops a query after 30 seconds. An authorizer refuses everything except
reading, so a WITH ... DELETE is refused too. schema(db_path) lists the tables and their columns.
work/ro_query.py is the same idea as a command line tool.
"""
import sqlite3
import time
from pathlib import Path

DEFAULT_ROWS = 50
MAX_ROWS = 500
MAX_CELL = 300
MAX_CHARS = 6000
TIMEOUT_S = 30
PROGRESS_STEPS = 10000
QUOTES = chr(39) + chr(34) + chr(96)
# Authorizer codes a read-only query needs: SELECT, READ, FUNCTION and RECURSIVE (33, for WITH RECURSIVE).
ALLOWED_ACTIONS = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, 33})
ONLY_SELECT = 'Only SELECT or WITH queries are allowed.'
ONLY_ONE = 'Only one statement is allowed.'


def authorize(action, *_):
    return sqlite3.SQLITE_OK if action in ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def code_only(sql):
    """The SQL with quoted text, quoted names and comments blanked out, so a ; inside them does not count."""
    out = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in QUOTES:
            j = i + 1
            while True:
                if j >= n:
                    raise ValueError('Unterminated quote in SQL.')
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            out.append(' ')
            i = j + 1
        elif c == '[':
            j = sql.find(']', i + 1)
            if j < 0:
                raise ValueError('Unterminated [name] in SQL.')
            out.append(' ')
            i = j + 1
        elif sql.startswith('--', i):
            j = sql.find(chr(10), i)
            out.append(' ')
            i = n if j < 0 else j
        elif sql.startswith('/*', i):
            j = sql.find('*/', i + 2)
            out.append(' ')
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def single_statement(sql):
    """The statement text to run; refuses anything but one SELECT or WITH statement."""
    if not isinstance(sql, str):
        raise ValueError('sql must be a string.')
    body = code_only(sql).strip()
    while body.endswith(';'):
        body = body[:-1].rstrip()
    if ';' in body:
        raise ValueError(ONLY_ONE)
    head = body.split(None, 1)[0].upper() if body else ''
    if head not in ('SELECT', 'WITH'):
        raise ValueError(ONLY_SELECT)
    return sql.strip()


def row_limit(value):
    if value is None:
        return DEFAULT_ROWS
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError('limit must be a number.')
    try:
        number = int(value)
    except (ValueError, OverflowError):
        raise ValueError('limit must be a number.') from None
    return max(1, min(MAX_ROWS, number))


def connect(db_path):
    path = Path(db_path)
    if not path.is_file():
        raise ValueError('Database file not found.')
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    con.execute('PRAGMA query_only = ON')
    return con


def cell(value):
    """(display value, was it cut). Text is cut to MAX_CELL characters including the dots."""
    if value is None or isinstance(value, (int, float)):
        return value, False
    if isinstance(value, (bytes, bytearray)):
        return '<blob %d bytes>' % len(value), False
    text = str(value)
    if len(text) > MAX_CELL:
        return text[:MAX_CELL - 3] + '...', True
    return text, False


def run_query(db_path, sql, limit=DEFAULT_ROWS, timeout=TIMEOUT_S):
    """Run one read-only SELECT/WITH statement.

    Returns {columns, rows, more, truncated}. more: rows exist that are not shown (row limit or text limit).
    truncated: a cell or the whole text was cut. Refusals and SQL errors raise ValueError.
    """
    limit = row_limit(limit)
    text = single_statement(sql)
    con = connect(db_path)
    try:
        con.set_authorizer(authorize)
        start = time.monotonic()
        con.set_progress_handler(lambda: 1 if time.monotonic() - start > timeout else 0, PROGRESS_STEPS)
        try:
            cur = con.execute(text)
            columns = [d[0] for d in cur.description or ()]
            fetched = cur.fetchmany(limit + 1)
        except sqlite3.Error as exc:
            message = str(exc)
            if 'interrupt' in message.lower():
                raise ValueError('Query stopped after %s s. Narrow it down.' % timeout) from None
            if 'not authorized' in message.lower():
                raise ValueError('Only read-only SELECT queries are allowed.') from None
            raise ValueError('SQL error: ' + message) from None
    finally:
        con.close()
    more = len(fetched) > limit
    truncated = False
    used = sum(len(name) for name in columns)
    rows = []
    for row in fetched[:limit]:
        cells = []
        size = 0
        for value in row:
            shown, cut = cell(value)
            truncated = truncated or cut
            cells.append(shown)
            size += len(str(shown)) if shown is not None else 0
        if used + size > MAX_CHARS:
            truncated = more = True
            break
        used += size
        rows.append(cells)
    return dict(columns=columns, rows=rows, more=more, truncated=truncated)


def schema(db_path):
    """{tables: [{name, columns}]} of the database, read-only."""
    con = connect(db_path)
    try:
        tables = []
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            quoted = chr(34) + name.replace(chr(34), chr(34) * 2) + chr(34)
            tables.append(dict(name=name, columns=[r[1] for r in con.execute('PRAGMA table_info(%s)' % quoted).fetchall()]))
    finally:
        con.close()
    return dict(tables=tables)
