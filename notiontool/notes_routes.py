"""Read-only Notes routes for the desktop 'Notes' tab (plans and notes the Main keeps as Notion pages).

The Main creates and updates plans/notes itself with Notion AI page tools; the harness had no page
list/read route. This module adds one, read-only:

- Sources of pages: notion.so / notion.com page links in recent task results and run records
  (linked Main = run main_worker, linked run = run id), plus optional registry docs of kind 'notes'
  ({id, page_id, title, kind, status, run_id, main_id, updated}) if another part writes them.
- Titles / last edited / page content come from the existing NotionClient (call('syncRecordValues'),
  call('loadPageChunk')), i.e. through the selected account's transport and its NetworkBudget, at
  'background' priority. Own cap: NOTION_CALLS_PER_MIN Notion calls per minute for Notes; above it
  the cache is served with stale=True. Titles are cached TITLE_TTL s, pages PAGE_TTL s, the store
  scan until the store revision changes.
- Nothing here writes to Notion or to the store, and nothing sends a chat: draft() only returns text
  for the user to review and send manually.

Hooks (other owners): server.py GET /api/notes, /api/notes/page, /api/notes/draft -> http_route();
desktop_shell.py DESK_ACTIONS + handlers from desk_handlers(server) -> notes_list, notes_page, notes_draft.
"""
from __future__ import annotations

import re
import threading
import time

NOTION_CALLS_PER_MIN = 12
TITLE_TTL = 300.0
PAGE_TTL = 60.0
LIST_LIMIT = 200
TASK_SCAN_LIMIT = 400
MAX_BLOCKS = 400
MAX_TEXT = 4000
MAX_DEPTH = 3
STATUSES = ('draft', 'active', 'done')
NOTION_BASE = ''.join(('https://', 'www.', 'notion', '.so/'))
KINDS = ('plan', 'note')

_HEX = r'[0-9a-fA-F]'
_ID = r'(%s{32}|%s{8}-%s{4}-%s{4}-%s{4}-%s{12})' % ((_HEX,) * 6)
PAGE_LINK_RE = re.compile(r'https?://(?:www\.)?notion\.(?:so|com)/[^\s)\]>"\'<]*?' + _ID + r'(?![0-9a-fA-F])')
STATUS_RE = re.compile(r'^\s*\[?\(?\s*(draft|active|done|in progress|finished|complete[d]?)\s*\]?\)?\s*[:\-]?', re.I)
STATUS_LINE_RE = re.compile(r'^\s*status\s*[:\-]\s*(draft|active|done|in progress|finished|complete[d]?)\b', re.I)
_STATUS_WORD = {'draft': 'draft', 'active': 'active', 'in progress': 'active', 'done': 'done',
                'finished': 'done', 'complete': 'done', 'completed': 'done'}

BLOCK_TYPES = {'header': 'h1', 'sub_header': 'h2', 'sub_sub_header': 'h3', 'text': 'p',
               'bulleted_list': 'li', 'numbered_list': 'ol', 'to_do': 'todo', 'quote': 'quote',
               'code': 'code', 'divider': 'divider', 'callout': 'callout', 'toggle': 'toggle',
               'page': 'subpage', 'equation': 'code'}


class NotesUnavailable(ValueError):
    """No signed-in Notion account (or no client) for reading pages."""


def page_id(value):
    """Dashed lower-case page id from an id or a Notion link, or ''."""
    text = str(value or '').strip()
    match = PAGE_LINK_RE.search(text) or re.search(_ID + r'(?![0-9a-fA-F])', text)
    if not match:
        return ''
    raw = match.group(1).replace('-', '').lower()
    return '%s-%s-%s-%s-%s' % (raw[:8], raw[8:12], raw[12:16], raw[16:20], raw[20:])


def page_url(ident):
    return NOTION_BASE + str(ident or '').replace('-', '')


def page_ids_in(text):
    seen, out = set(), []
    for match in PAGE_LINK_RE.finditer(str(text or '')):
        ident = page_id(match.group(1))
        if ident and ident not in seen:
            seen.add(ident)
            out.append(ident)
    return out


def _unwrap(value):
    while isinstance(value, dict) and isinstance(value.get('value'), dict):
        value = value['value']
    return value if isinstance(value, dict) else {}


def rich_text(prop):
    """Plain text of a Notion rich-text property ([[text, marks], ...]); mentions become their text."""
    if not isinstance(prop, list):
        return ''
    parts = []
    for seg in prop:
        if isinstance(seg, list) and seg and isinstance(seg[0], str):
            parts.append('' if seg[0] == '\u2023' else seg[0])
    return ''.join(parts)[:MAX_TEXT]


def status_of(title, first_lines=()):
    for line in list(first_lines)[:8]:
        match = STATUS_LINE_RE.match(str(line or ''))
        if match:
            return _STATUS_WORD[match.group(1).lower()]
    match = STATUS_RE.match(str(title or ''))
    if match and (str(title).lstrip()[:1] in '[(' or ':' in str(title)[:20] or '-' in str(title)[:20]):
        return _STATUS_WORD[match.group(1).lower()]
    return 'active'


def kind_of(title, hint=''):
    hint = str(hint or '').lower()
    if hint in KINDS:
        return hint
    return 'plan' if re.search(r'\bplan\b|\broadmap\b', str(title or ''), re.I) else 'note'


def _clean_title(title):
    title = str(title or '').strip()
    stripped = re.sub(r'^\s*[\[(]\s*(draft|active|done)\s*[\])]\s*', '', title, flags=re.I)
    return (stripped or title or 'Untitled')[:200]


class NotesRoutes:
    def __init__(self, store, client_factory, clock=time.time, calls_per_min=NOTION_CALLS_PER_MIN):
        self.store = store
        self.client_factory = client_factory
        self.clock = clock
        self.calls_per_min = int(calls_per_min)
        self.lock = threading.RLock()
        self.calls = []
        self.titles = {}      # id -> (fetched, info)
        self.pages = {}       # id -> (fetched, page)
        self.scan_key = None
        self.scan = []
        self.stats = dict(notion_calls=0, budget_skips=0, errors=0, last_error='')

    # -- budget -------------------------------------------------------------------------------
    def _allow(self):
        now = self.clock()
        self.calls = [t for t in self.calls if now - t < 60.0]
        if len(self.calls) >= self.calls_per_min:
            self.stats['budget_skips'] += 1
            return False
        self.calls.append(now)
        return True

    def _call(self, client, path, body):
        self.stats['notion_calls'] += 1
        try:
            from .network_budget import run_with_priority
        except Exception:  # pragma: no cover - standalone use
            return client.call(path, body)
        return run_with_priority('background', client.call, path, body)

    def _client(self):
        client = self.client_factory() if callable(self.client_factory) else None
        if client is None:
            raise NotesUnavailable('Sign in to Notion in NotionTool first')
        return client

    def _space(self, client):
        return str((getattr(client, 'info', None) or {}).get('space_id') or '')

    # -- store scan ---------------------------------------------------------------------------
    def _revision(self):
        value = getattr(getattr(self.store, 'db', None), 'total_changes', None)
        return value if isinstance(value, int) else None

    def _task_rows(self):
        db, lock = getattr(self.store, 'db', None), getattr(self.store, 'lock', None)
        if db is not None and lock is not None:
            sql = ("SELECT json_extract(doc,'$.id'), json_extract(doc,'$.run_id'), json_extract(doc,'$.worker_id'), "
                   "substr(json_extract(doc,'$.result'),1,20000), json_extract(doc,'$.finished'), "
                   "json_extract(doc,'$.updated') FROM objects WHERE kind='tasks' AND json_type(doc,'$.result')='text' "
                   "AND instr(json_extract(doc,'$.result'),'notion.')>0 ORDER BY rowid DESC LIMIT ?")
            try:
                with lock:
                    rows = db.execute(sql, (TASK_SCAN_LIMIT,)).fetchall()
                return [dict(id=r[0], run_id=r[1], worker_id=r[2], result=r[3], finished=r[4], updated=r[5])
                        for r in rows]
            except Exception:
                pass
        tasks = [t for t in (self.store.all('tasks') or []) if 'notion.' in str(t.get('result') or '')]
        return list(reversed(tasks))[:TASK_SCAN_LIMIT]

    def _all(self, kind):
        try:
            return list(self.store.all(kind) or [])
        except Exception:
            return []

    def _scan(self):
        key = self._revision()
        if key is not None and key == self.scan_key:
            return self.scan
        runs = {r.get('id'): r for r in self._all('runs') if r.get('id')}
        workers = {w.get('id'): w for w in self._all('workers') if w.get('id')}
        found = {}

        def main_label(wid):
            worker = workers.get(wid) or {}
            return str(worker.get('label') or ('Main' if wid else ''))[:80]

        def add(ident, **info):
            entry = found.setdefault(ident, dict(id=ident, run_id='', run_title='', main_id='', main_label='',
                                                 seen=0.0, title='', kind='', status=''))
            for k, v in info.items():
                if v and not entry.get(k):
                    entry[k] = v
            entry['seen'] = max(entry['seen'], float(info.get('seen') or 0))

        for doc in self._all('notes'):
            ident = page_id(doc.get('page_id') or doc.get('url') or doc.get('id'))
            if ident:
                run = runs.get(doc.get('run_id')) or {}
                wid = doc.get('main_id') or run.get('main_worker') or ''
                add(ident, run_id=str(doc.get('run_id') or ''), run_title=str(run.get('goal') or run.get('title') or '')[:160],
                    main_id=wid, main_label=main_label(wid), title=str(doc.get('title') or ''),
                    kind=str(doc.get('kind') or ''), status=str(doc.get('status') or '').lower(),
                    seen=doc.get('updated') or doc.get('created') or 0)
        for run in runs.values():
            text = ' '.join(str(run.get(k) or '') for k in ('goal', 'title', 'result', 'summary', 'plan_url', 'notes_url'))
            for ident in page_ids_in(text):
                wid = run.get('main_worker') or ''
                add(ident, run_id=run['id'], run_title=str(run.get('goal') or run.get('title') or '')[:160],
                    main_id=wid, main_label=main_label(wid), seen=run.get('updated') or run.get('created') or 0)
        for task in self._task_rows():
            run = runs.get(task.get('run_id')) or {}
            wid = run.get('main_worker') or ''
            for ident in page_ids_in(task.get('result')):
                add(ident, run_id=str(task.get('run_id') or ''), run_title=str(run.get('goal') or run.get('title') or '')[:160],
                    main_id=wid, main_label=main_label(wid), seen=task.get('finished') or task.get('updated') or 0)
        self.scan = sorted(found.values(), key=lambda e: -e['seen'])[:LIST_LIMIT]
        self.scan_key = key
        return self.scan

    # -- Notion reads -------------------------------------------------------------------------
    def _fetch_titles(self, ids):
        now = self.clock()
        missing = [i for i in ids if i not in self.titles or now - self.titles[i][0] > TITLE_TTL]
        stale = False
        if missing:
            try:
                client = self._client()
            except NotesUnavailable:
                return True
            space = self._space(client)
            for start in range(0, len(missing), 50):
                if not self._allow():
                    stale = True
                    break
                chunk = missing[start:start + 50]
                try:
                    data = self._call(client, 'syncRecordValues', {'requests': [
                        {'pointer': {'table': 'block', 'id': i, 'spaceId': space}, 'version': -1} for i in chunk]})
                except Exception as exc:
                    self._error(exc)
                    stale = True
                    break
                blocks = (data.get('recordMap') or {}).get('block') or {}
                for i in chunk:
                    block = _unwrap(blocks.get(i) or blocks.get(i.replace('-', '')))
                    info = dict(alive=bool(block) and block.get('alive', True) is not False,
                                title=rich_text((block.get('properties') or {}).get('title')),
                                type=str(block.get('type') or ''),
                                updated=float(block.get('last_edited_time') or 0) / 1000.0,
                                icon=str((block.get('format') or {}).get('page_icon') or '')[:8])
                    self.titles[i] = (now, info)
        return stale

    def _error(self, exc):
        self.stats['errors'] += 1
        self.stats['last_error'] = (type(exc).__name__ + ': ' + str(exc))[:200]

    def list(self, args=None):
        args = args if isinstance(args, dict) else {}
        query = str(args.get('query') or '').strip().lower()[:200]
        status = str(args.get('status') or '').lower()
        kind = str(args.get('kind') or '').lower()
        with self.lock:
            scan = self._scan()
            stale = self._fetch_titles([e['id'] for e in scan])
            notes = []
            for entry in scan:
                info = (self.titles.get(entry['id']) or (0, {}))[1]
                if info and not info.get('alive', True):
                    continue
                if info and info.get('type') and info['type'] not in ('page', 'collection_view_page'):
                    continue
                raw = info.get('title') or entry.get('title') or ''
                page = (self.pages.get(entry['id']) or (0, {}))[1]
                note_status = entry.get('status') if entry.get('status') in STATUSES else (
                    page.get('status') or status_of(raw))
                notes.append(dict(id=entry['id'], title=_clean_title(raw), kind=kind_of(raw, entry.get('kind')),
                                  status=note_status, updated=info.get('updated') or entry['seen'] or 0,
                                  icon=info.get('icon') or '', run_id=entry['run_id'], run_title=entry['run_title'],
                                  main_id=entry['main_id'], main_label=entry['main_label'], url=page_url(entry['id']),
                                  known=bool(info)))
            total = len(notes)
            if query:
                notes = [n for n in notes if query in (n['title'] + ' ' + n['run_title'] + ' ' + n['main_label']).lower()]
            if status in STATUSES:
                notes = [n for n in notes if n['status'] == status]
            if kind in KINDS:
                notes = [n for n in notes if n['kind'] == kind]
            notes.sort(key=lambda n: -float(n['updated'] or 0))
            return dict(notes=notes, total=total, stale=stale, error=self.stats['last_error'] if stale else '')

    def page(self, args=None):
        args = args if isinstance(args, dict) else {}
        ident = page_id(args.get('id') or args.get('url'))
        if not ident:
            raise ValueError('A page id or Notion link is required')
        with self.lock:
            now = self.clock()
            cached = self.pages.get(ident)
            if cached and now - cached[0] < PAGE_TTL and not args.get('refresh'):
                return dict(cached[1], stale=False)
            if not self._allow():
                if cached:
                    return dict(cached[1], stale=True)
                raise ValueError('Notes is waiting for the Notion request limit; try again in a minute')
            client = self._client()
            try:
                data = self._call(client, 'loadPageChunk', {'pageId': ident, 'limit': 100, 'cursor': {'stack': []},
                                                            'chunkNumber': 0, 'verticalColumns': False})
            except Exception as exc:
                self._error(exc)
                if cached:
                    return dict(cached[1], stale=True)
                raise ValueError('Could not read this page from Notion right now') from exc
            page = render_page(ident, (data.get('recordMap') or {}).get('block') or {})
            self.pages[ident] = (now, page)
            self.titles[ident] = (now, dict(alive=True, title=page['raw_title'], type='page',
                                            updated=page['updated'], icon=page['icon']))
            return dict(page, stale=False)

    def draft(self, args=None):
        return dict(text=draft_text(args if isinstance(args, dict) else {}), sent=False)

    def status(self):
        return dict(self.stats, cached_pages=len(self.pages), cached_titles=len(self.titles))


def render_page(ident, blocks):
    """Safe structured content: [{type, text, checked?, language?, depth}] in page order (no HTML)."""
    def get(i):
        return _unwrap(blocks.get(i) or blocks.get(str(i).replace('-', '')))

    root = get(ident)
    out = []

    def walk(ids, depth):
        for child in ids or []:
            if len(out) >= MAX_BLOCKS:
                return
            block = get(child)
            if not block or block.get('alive', True) is False:
                continue
            btype = str(block.get('type') or '')
            props = block.get('properties') or {}
            text = rich_text(props.get('title'))
            kind = BLOCK_TYPES.get(btype, 'p' if text else '')
            if not kind:
                continue
            item = dict(type=kind, text=text, depth=depth)
            if kind == 'todo':
                item['checked'] = rich_text(props.get('checked')).lower() == 'yes'
            if btype == 'code':
                item['language'] = rich_text(props.get('language'))[:30]
            out.append(item)
            if kind != 'subpage' and depth < MAX_DEPTH:
                walk(block.get('content'), depth + 1)

    walk(root.get('content'), 0)
    raw = rich_text((root.get('properties') or {}).get('title'))
    first = [b['text'] for b in out[:8]]
    return dict(id=ident, title=_clean_title(raw), raw_title=raw, status=status_of(raw, first),
                kind=kind_of(raw), updated=float(root.get('last_edited_time') or 0) / 1000.0,
                icon=str((root.get('format') or {}).get('page_icon') or '')[:8], url=page_url(ident),
                blocks=out, truncated=len(out) >= MAX_BLOCKS, found=bool(root))


def draft_text(args):
    """Message for the Main; the user reviews and sends it. Plain, short English."""
    mode = str(args.get('mode') or 'new').lower()
    title = str(args.get('title') or '').strip()[:200]
    idea = str(args.get('idea') or args.get('change') or '').strip()[:4000]
    if mode == 'update':
        ident = page_id(args.get('id') or args.get('url'))
        link = page_url(ident) if ident else ''
        lines = ['Please update the plan "%s"%s.' % (title or 'Untitled', (' (' + link + ')') if link else ''),
                 'Keep its structure and change only this:',
                 idea or '- (write what should change)',
                 'Update the "Status:" line (Draft, Active or Done) and the date, then reply with the page link.']
    else:
        lines = ['Please create a new plan as a Notion page.',
                 'Title: ' + (title or '(write a short title)'),
                 'Goal: ' + (idea or '(write what the plan is for)'),
                 'Use these headings: Goal, Steps, Open questions. Put "Status: Draft" as the first line.',
                 'Reply with the page link when it is done.']
    return '\n'.join(lines)


def _server_client_factory(server):
    def factory():
        store, adapter = getattr(server, 'store', None), getattr(server, 'adapter', None)
        if store is None or adapter is None:
            return None
        from .harness_account import selected_account
        account = selected_account(store)
        if not account:
            return None
        try:
            return adapter.client(account)
        except ValueError:
            return None
    return factory


def routes_for(server):
    routes = getattr(server, '_nt_notes_routes', None)
    if routes is None:
        routes = NotesRoutes(server.store, _server_client_factory(server))
        try:
            server._nt_notes_routes = routes
        except AttributeError:
            pass
    return routes


def desk_handlers(server):
    """Overlay actions for desktop_shell.py: notes_list, notes_page, notes_draft (all read-only)."""
    return {'notes_list': lambda args: routes_for(server).list(args),
            'notes_page': lambda args: routes_for(server).page(args),
            'notes_draft': lambda args: routes_for(server).draft(args)}


def http_route(server, path, query):
    """GET /api/notes?query&status&kind, /api/notes/page?id, /api/notes/draft?mode&id&title&idea."""
    args = {k: (v[0] if isinstance(v, list) and v else v) for k, v in dict(query or {}).items()}
    routes = routes_for(server)
    try:
        if path == '/api/notes':
            return 200, routes.list(args)
        if path == '/api/notes/page':
            return 200, routes.page(args)
        if path == '/api/notes/draft':
            return 200, routes.draft(args)
        if path == '/api/notes/status':
            return 200, routes.status()
    except NotesUnavailable as exc:
        return 409, {'error': str(exc)}
    except ValueError as exc:
        return 400, {'error': str(exc)[:300]}
    return 404, {'error': 'Unknown notes route'}
