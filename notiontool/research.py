"""Web research the Perplexity way, without their models (operator tool nt_research).

Pipeline: several search queries in parallel (DuckDuckGo HTML, no key, no extra service)
-> reciprocal rank fusion and dedupe -> open and read the best pages -> split them into
passages -> BM25 ranking against the question (+ a small search-rank prior) -> the best
passages with numbered sources. The calling chat writes the answer and cites [n]; for a
follow-up round (deep research) it calls again with new queries for the gaps.
Only the standard library; the network call and the clock are injectable (tests use fakes).
"""
import concurrent.futures as cf
import math
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from html.parser import HTMLParser

SEARCH_URL = 'https://' + 'html.duckduckgo' + '.com/html/'
LITE_URL = 'https://' + 'lite.duckduckgo' + '.com/lite/'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/130.0 Safari/537.36')
MAX_QUERIES = 6
MAX_PAGES = 15
DEFAULT_PAGES = 8
MAX_PASSAGES = 30
DEFAULT_PASSAGES = 12
PER_DOMAIN = 2
PER_SOURCE = 3           # passages of one page in the result, so several sources speak
PAGE_PASSAGES = 40       # passages taken from one page (bounds the ranking work)
SEARCH_WORKERS = 3       # gentle: DuckDuckGo blocks bursts from one IP
FETCH_WORKERS = 8
SEARCH_TIMEOUT = 10.0
FETCH_TIMEOUT = 8.0
BUDGET_S = 38.0          # below the 45 s synchronous tool limit
MAX_BYTES = 1500000
PASSAGE_WORDS = 160
PASSAGE_CHARS = 1000
RRF_K = 60
TIMES = {'day': 'd', 'week': 'w', 'month': 'm', 'year': 'y'}
TRACKING = re.compile(r'^(utm_|fbclid$|gclid$|mc_|ref$|ref_src$|spm$)')
WORD = re.compile(r'\w{2,}', re.U)
STOP = frozenset((
    'the and for are but not you your with this that from have has was were will what which who how why when '
    'where can could should would into about than then them they their there its our out all any also only '
    'der die das und oder ein eine einer eines einem einen ist sind war wie was wer wo warum wann mit von '
    'fuer für auf aus bei den dem des sich nicht auch nur noch kann können soll mehr als zum zur im am es ich '
    'du er sie wir ihr man so da dass zu in an um oder ob').split())

DESCRIPTION = (
    'Research: Perplexity-style web research (no extra model). Before calling, split the question into 3-6 '
    'search queries: different wording, English and German, one with site: for official docs or forums, one '
    'with the current year for recent facts. NotionTool searches them in parallel, merges and dedupes the '
    'results, OPENS and READS the best pages and returns the most relevant passages with numbered sources. '
    'Answer only from the passages and cite them as [n]. If something is still missing, call again with new, '
    'more specific queries for the gaps (deep research). No assignment token is needed.')
_PROPS = {
    'question': {'type': 'string', 'description': 'The full question in one sentence (used to rank the passages)'},
    'queries': {'type': 'array', 'items': {'type': 'string'},
                'description': '3-6 search queries derived from the question. Empty: the question itself is searched'},
    'max_pages': {'type': 'integer', 'description': f'Pages to open and read, default {DEFAULT_PAGES}, at most {MAX_PAGES}'},
    'max_passages': {'type': 'integer',
                     'description': f'Best passages to return, default {DEFAULT_PASSAGES}, at most {MAX_PASSAGES}'},
    'region': {'type': 'string', 'description': 'Search region such as de-de or us-en; default wt-wt (worldwide)'},
    'time': {'type': 'string', 'enum': ['day', 'week', 'month', 'year'],
             'description': 'Optional: only results from the last day, week, month or year'},
}
RESEARCH_OPERATOR_TOOLS = [
    {'name': 'nt_research', 'description': DESCRIPTION, 'annotations': {'readOnlyHint': True, 'destructiveHint': False},
     'inputSchema': {'type': 'object', 'properties': _PROPS, 'required': ['question']}},
]
RESEARCH_OPERATOR_NAMES = {t['name'] for t in RESEARCH_OPERATOR_TOOLS}


# ---- network -----------------------------------------------------------------
def http(method, url, data=None, timeout=10.0):
    """(status, content type, body bytes, final url). Reads at most MAX_BYTES."""
    body = urllib.parse.urlencode(data).encode('utf-8') if data else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        'User-Agent': UA, 'Accept-Language': 'de,en;q=0.8',
        'Accept': 'text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5'})
    if body:
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(MAX_BYTES)
        return resp.status, resp.headers.get('Content-Type', '') or '', raw, resp.geturl()


def decode(raw, ctype):
    match = re.search(r'charset=([\w-]+)', ctype or '', re.I)
    if not match:
        match = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:4000], re.I)
        charset = match.group(1).decode('ascii', 'ignore') if match else 'utf-8'
    else:
        charset = match.group(1)
    try:
        return raw.decode(charset, errors='replace')
    except LookupError:
        return raw.decode('utf-8', errors='replace')


# ---- search ------------------------------------------------------------------
class _Results(HTMLParser):
    """Result links and snippets of the DuckDuckGo HTML and lite pages."""
    LINKS = ('result__a', 'result-link')
    SNIPS = ('result__snippet', 'result-snippet')

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.cap = [], None

    def handle_starttag(self, tag, attrs):
        if self.cap:
            if tag == self.cap[1]:
                self.cap[2] += 1
            return
        attrs = dict(attrs)
        classes = (attrs.get('class') or '').split()
        if tag == 'a' and any(c in self.LINKS for c in classes):
            self.rows.append({'title': '', 'href': attrs.get('href') or '', 'snippet': ''})
            self.cap = ['title', tag, 1, []]
        elif self.rows and any(c in self.SNIPS for c in classes):
            self.cap = ['snippet', tag, 1, []]

    def handle_endtag(self, tag):
        if self.cap and tag == self.cap[1]:
            self.cap[2] -= 1
            if self.cap[2] <= 0:
                self.rows[-1][self.cap[0]] = ' '.join(''.join(self.cap[3]).split())
                self.cap = None

    def handle_data(self, data):
        if self.cap:
            self.cap[3].append(data)


def real_url(href):
    """Target of a result link ('' for ads, DuckDuckGo's own pages and non-web links)."""
    href = (href or '').strip()
    if href.startswith('//'):
        href = 'https:' + href
    parts = urllib.parse.urlsplit(href)
    if parts.netloc.endswith('duckduckgo.com') and parts.path.startswith('/l/'):
        target = urllib.parse.parse_qs(parts.query).get('uddg')
        href = target[0] if target else ''
        parts = urllib.parse.urlsplit(href)
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.netloc.endswith('duckduckgo.com'):
        return ''
    return href


def parse_results(page):
    parser = _Results()
    parser.feed(page)
    out, seen = [], set()
    for row in parser.rows:
        url = real_url(row['href'])
        if url and normalize(url) not in seen:
            seen.add(normalize(url))
            out.append({'title': row['title'] or url, 'url': url, 'snippet': row['snippet']})
    return out


def normalize(url):
    """Dedupe key: host without www, no fragment, no tracking parameters, no trailing slash."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower()
    host = host[4:] if host.startswith('www.') else host
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if not TRACKING.match(k)])
    return host + (parts.path.rstrip('/') or '') + ('?' + query if query else '')


def domain(url):
    host = urllib.parse.urlsplit(url).netloc.lower()
    return host[4:] if host.startswith('www.') else host


def fuse(lists):
    """Reciprocal rank fusion of several result lists; a page found by many queries ranks high."""
    merged = {}
    for results in lists:
        for rank, row in enumerate(results):
            key = normalize(row['url'])
            hit = merged.setdefault(key, dict(row, score=0.0, hits=0))
            hit['score'] += 1.0 / (RRF_K + rank + 1)
            hit['hits'] += 1
            if len(row.get('snippet') or '') > len(hit.get('snippet') or ''):
                hit['snippet'] = row['snippet']
    return sorted(merged.values(), key=lambda r: -r['score'])


def pick(fused, count, per_domain=PER_DOMAIN):
    chosen, extra, per = [], [], Counter()
    for row in fused:
        if len(chosen) < count and per[domain(row['url'])] < per_domain:
            per[domain(row['url'])] += 1
            chosen.append(row)
        else:
            extra.append(row)
    return chosen, extra


# ---- reading -----------------------------------------------------------------
class _Text(HTMLParser):
    SKIP = frozenset(('script', 'style', 'noscript', 'svg', 'nav', 'footer', 'aside', 'form', 'iframe',
                      'template', 'button', 'select', 'canvas'))
    BLOCK = frozenset(('p', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'td', 'th', 'pre', 'blockquote', 'dd', 'dt',
                       'div', 'section', 'article', 'tr', 'figcaption', 'br', 'main', 'table', 'ul', 'ol', 'header'))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip, self.buf, self.blocks, self.title, self.in_title = 0, [], [], '', False

    def flush(self):
        text = ' '.join(''.join(self.buf).split())
        self.buf = []
        if text:
            self.blocks.append(text)

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == 'title':
            self.in_title = True
        elif tag in self.BLOCK:
            self.flush()

    def handle_startendtag(self, tag, attrs):
        if tag in self.BLOCK:
            self.flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == 'title':
            self.in_title = False
        elif tag in self.BLOCK:
            self.flush()

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.buf.append(data)


def page_text(raw, ctype):
    """(title, text blocks) of an HTML or plain-text page; None for other types (PDF, images)."""
    ctype = (ctype or '').lower()
    text = decode(raw, ctype)
    if 'html' in ctype or (not ctype and text.lstrip()[:1] == '<'):
        parser = _Text()
        parser.feed(text)
        parser.flush()
        return ' '.join(parser.title.split()), parser.blocks
    if ctype.startswith('text/'):
        return '', [' '.join(p.split()) for p in re.split(r'\n\s*\n', text) if p.strip()]
    return None


def passages(blocks, words=PASSAGE_WORDS):
    """Blocks of real prose (menus and buttons dropped) grouped into ~words-long passages."""
    out, cur, n, seen = [], [], 0, set()
    for block in blocks:
        parts = block.split()
        if len(parts) < 6 or block in seen:
            continue
        seen.add(block)
        while len(parts) > words * 2:
            if cur:
                out.append(' '.join(cur))
                cur, n = [], 0
            out.append(' '.join(parts[:words]))
            parts = parts[words:]
        cur.append(' '.join(parts))
        n += len(parts)
        if n >= words:
            out.append(' '.join(cur))
            cur, n = [], 0
    if cur:
        out.append(' '.join(cur))
    return out[:PAGE_PASSAGES]


# ---- ranking -----------------------------------------------------------------
def terms(text):
    return [w for w in WORD.findall((text or '').lower()) if w not in STOP]


def bm25(docs, query, k1=1.4, b=0.75):
    """BM25 score per token list, times term coverage (passages that answer more of the question win)."""
    qset = set(query)
    if not docs or not qset:
        return [0.0] * len(docs)
    df = Counter()
    for tokens in docs:
        df.update(set(tokens) & qset)
    avg = (sum(len(t) for t in docs) / len(docs)) or 1.0
    out = []
    for tokens in docs:
        tf = Counter(t for t in tokens if t in qset)
        size = len(tokens) or 1
        score = 0.0
        for term, freq in tf.items():
            idf = math.log(1 + (len(docs) - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * freq * (k1 + 1) / (freq + k1 * (1 - b + b * size / avg))
        out.append(score * (0.5 + len(tf) / len(qset)))
    return out


def clip(text, limit=PASSAGE_CHARS):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(' ', 1)[0]
    return cut + ' ...'


def _clamp(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def clean_queries(queries, question):
    out = []
    for query in queries if isinstance(queries, list) else []:
        query = ' '.join(str(query or '').split())[:200]
        if query and query.lower() not in (q.lower() for q in out):
            out.append(query)
    return out[:MAX_QUERIES] or [question[:200]]


# ---- pipeline ----------------------------------------------------------------
class Research:
    def __init__(self, fetch=None, clock=None):
        self.fetch = fetch or http
        self.clock = clock or time.monotonic

    def search(self, query, region, when):
        data = {'q': query, 'kl': region}
        if when:
            data['df'] = when
        last = ''
        for url in (SEARCH_URL, LITE_URL):  # the lite page answers when the HTML page throttles
            try:
                status, ctype, raw, _final = self.fetch('POST', url, data, SEARCH_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - try the other page
                last = type(exc).__name__ + ': ' + str(exc)[:120]
                continue
            rows = parse_results(decode(raw, ctype)) if status == 200 else []
            if rows:
                return rows[:20]
            last = 'HTTP %s, no results' % status
        raise RuntimeError(last or 'no results')

    def read(self, row):
        status, ctype, raw, final = self.fetch('GET', row['url'], None, FETCH_TIMEOUT)
        if status != 200:
            raise RuntimeError('HTTP %s' % status)
        page = page_text(raw, ctype)
        if page is None:
            raise RuntimeError('not a text page (%s)' % (ctype.split(';')[0] or 'unknown'))
        return page[0], passages(page[1]), final

    def _pool(self, fn, items, workers, deadline):
        """[(item, result, error)] in input order; unfinished items get a timeout error."""
        pool = cf.ThreadPoolExecutor(max_workers=max(1, min(workers, len(items))))
        try:
            futures = [pool.submit(fn, item) for item in items]
            cf.wait(futures, timeout=max(1.0, deadline - self.clock()))
            out = []
            for item, future in zip(items, futures):
                if not future.done():
                    out.append((item, None, 'timeout'))
                elif future.exception() is not None:
                    exc = future.exception()
                    out.append((item, None, (str(exc) or type(exc).__name__)[:160]))
                else:
                    out.append((item, future.result(), ''))
            return out
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def call(self, args):
        args = args if isinstance(args, dict) else {}
        question = ' '.join(str(args.get('question') or '').split())[:500]
        if not question:
            raise ValueError('nt_research needs a question')
        queries = clean_queries(args.get('queries'), question)
        pages = _clamp(args.get('max_pages'), DEFAULT_PAGES, 1, MAX_PAGES)
        want = _clamp(args.get('max_passages'), DEFAULT_PASSAGES, 1, MAX_PASSAGES)
        region = re.sub(r'[^a-z-]', '', str(args.get('region') or 'wt-wt').lower())[:8] or 'wt-wt'
        when = TIMES.get(str(args.get('time') or '').lower(), '')
        start = self.clock()
        deadline = start + BUDGET_S

        searched = self._pool(lambda q: self.search(q, region, when), queries, SEARCH_WORKERS, start + BUDGET_S / 3)
        search_errors = [dict(query=q, error=err) for q, _rows, err in searched if err]
        fused = fuse([rows for _q, rows, err in searched if not err])
        chosen, extra = pick(fused, pages)
        read = self._pool(self.read, chosen, FETCH_WORKERS, deadline) if chosen else []

        query_terms = terms(question + ' ' + ' '.join(queries))
        top = max([row['score'] for row in chosen] or [1.0])
        sources, items = [], []
        for n, (row, result, err) in enumerate(read, 1):
            title, texts, final = result if result else ('', [], row['url'])
            source = dict(n=n, title=title or row['title'], url=final or row['url'], snippet=row.get('snippet', ''),
                          read=bool(texts), found_by=row['hits'])
            if err:
                source['error'] = err
            sources.append(source)
            if not texts and row.get('snippet'):
                texts = [row['snippet']]  # unreadable page: its search snippet still counts
            for text in texts:
                items.append(dict(n=n, text=text, tokens=terms(text), prior=row['score'] / top))

        scores = bm25([item['tokens'] for item in items], query_terms)
        best = max(scores or [0.0]) or 1.0
        for item, score in zip(items, scores):
            item['score'] = 0.8 * score / best + 0.2 * item['prior']
        result, per, seen = [], Counter(), set()
        for item in sorted(items, key=lambda i: -i['score']):
            sign = ' '.join(item['tokens'][:12])
            if sign in seen or per[item['n']] >= PER_SOURCE or item['score'] <= 0.0:
                continue
            seen.add(sign)
            per[item['n']] += 1
            result.append(dict(n=item['n'], score=round(item['score'], 3), text=clip(item['text'])))
            if len(result) >= want:
                break

        note = ('Answer only from these passages and cite them as [n]. Missing facts: call nt_research again '
                'with new, more specific queries (other words, English, site:domain).')
        if not fused:
            note = ('No search results. The search may be throttled for a minute; try fewer or different '
                    'queries later.')
        return dict(ok=bool(result), question=question, queries=queries, passages=result, sources=sources,
                    more=[dict(title=r['title'], url=r['url'], snippet=r.get('snippet', '')) for r in extra[:6]],
                    search_errors=search_errors, took_s=round(self.clock() - start, 1), note=note)


_DEFAULT = None


def call(args):
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Research()
    return _DEFAULT.call(args)
