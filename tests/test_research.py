from notiontool import research

DDG = '//' + 'duckduckgo' + '.com/l/?uddg='


def ddg_page(*rows):
    out = []
    for url, title, snip in rows:
        out.append('<div class="result"><a class="result__a" href="%s%s&rut=x">%s</a>'
                   '<a class="result__snippet" href="#">%s <b>bold</b></a></div>'
                   % (DDG, research.urllib.parse.quote(url, safe=''), title, snip))
    out.append('<a class="result__a" href="https://' + 'duckduckgo' + '.com/y.js?ad=1">Ad</a>')
    return '<html><body>' + ''.join(out) + '</body></html>'


def article(text):
    return ('<html><head><title>Page T</title><script>var x = "secret menu";</script></head><body>'
            '<nav>Home About Contact Login Menu Item Another</nav><p>' + text + '</p>'
            '<footer>Copyright footer text that is long enough to count</footer></body></html>').encode()


def test_parse_results_unwraps_links_and_drops_ads():
    rows = research.parse_results(ddg_page(('https://a.example/x?utm_source=q', 'A', 'snip a')))
    assert rows == [{'title': 'A', 'url': 'https://a.example/x?utm_source=q', 'snippet': 'snip a bold'}]


def test_normalize_and_fuse_merge_duplicates():
    assert research.normalize('https://www.a.example/x/?utm_source=1#top') == 'a.example/x'
    one = [{'url': 'https://a.example/x', 'title': 'A', 'snippet': ''}, {'url': 'https://b.example', 'title': 'B', 'snippet': ''}]
    two = [{'url': 'https://www.a.example/x/', 'title': 'A2', 'snippet': 'longer'}]
    fused = research.fuse([one, two])
    assert fused[0]['hits'] == 2 and fused[0]['snippet'] == 'longer' and len(fused) == 2


def test_pick_limits_pages_per_domain():
    rows = [{'url': 'https://a.example/%d' % i, 'score': 1.0} for i in range(4)] + [{'url': 'https://b.example', 'score': 1.0}]
    chosen, extra = research.pick(rows, 5)
    assert [r['url'] for r in chosen] == ['https://a.example/0', 'https://a.example/1', 'https://b.example']
    assert len(extra) == 2


def test_page_text_skips_scripts_and_navigation():
    title, blocks = research.page_text(article('The capital of France is Paris and it is large.'), 'text/html; charset=utf-8')
    assert title == 'Page T'
    assert 'The capital of France is Paris and it is large.' in blocks
    assert not any('secret' in b or 'Login' in b for b in blocks)
    assert research.page_text(b'%PDF', 'application/pdf') is None


def test_bm25_prefers_the_answering_passage():
    q = research.terms('capital of France')
    docs = [research.terms('Bananas are yellow fruit grown in warm places'), research.terms('Paris is the capital of France')]
    scores = research.bm25(docs, q)
    assert scores[1] > scores[0]


def test_call_end_to_end_with_fake_network():
    pages = {
        'https://a.example/paris': article('Paris is the capital of France and has about two million people living there.'),
        'https://b.example/banana': article('Bananas are yellow and grow in warm countries all over the world today.'),
    }
    calls = []

    def fetch(method, url, data, timeout):
        calls.append((method, url))
        if method == 'POST':
            return 200, 'text/html', ddg_page(('https://a.example/paris', 'Paris', 'capital'),
                                              ('https://b.example/banana', 'Banana', 'fruit')).encode(), url
        if url in pages:
            return 200, 'text/html', pages[url], url
        raise OSError('down')

    out = research.Research(fetch=fetch).call({'question': 'What is the capital of France?',
                                                'queries': ['capital France', 'Hauptstadt Frankreich', 'capital France']})
    assert out['ok'] and out['queries'] == ['capital France', 'Hauptstadt Frankreich']
    assert out['passages'][0]['n'] == 1 and 'Paris is the capital' in out['passages'][0]['text']
    assert out['sources'][0]['read'] and out['sources'][0]['found_by'] == 2
    assert sum(1 for m, _u in calls if m == 'POST') == 2


def test_call_without_results_explains_and_needs_question():
    out = research.Research(fetch=lambda *a: (200, 'text/html', b'<html></html>', a[1])).call({'question': 'x y'})
    assert not out['ok'] and 'No search results' in out['note'] and out['search_errors']
    try:
        research.Research(fetch=lambda *a: None).call({})
    except ValueError:
        pass
    else:
        raise AssertionError('question required')


def test_tool_is_published_read_only():
    spec = research.RESEARCH_OPERATOR_TOOLS[0]
    assert spec['name'] == 'nt_research' and spec['annotations'] == {'readOnlyHint': True, 'destructiveHint': False}
