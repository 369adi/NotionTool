"""Perplexity Sonar API client used by the `perplexity_search` tool.

Everything is injectable for tests (http_post, now). No API key is stored in this
module; the key comes from Settings (perplexity_api_key) or the PPLX_API_KEY /
PERPLEXITY_API_KEY environment variables. The key is never logged.
"""
import json
import urllib.request

API_URL = 'https://api.perplexity.ai/chat/completions'
DEFAULT_MODEL = 'sonar'
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT_S = 60


def api_key(settings=None):
    """Return the Perplexity API key from settings, else the environment. '' when unset."""
    if isinstance(settings, dict):
        key = str(settings.get('perplexity_api_key') or '').strip()
        if key:
            return key
    import os
    return str(os.environ.get('PPLX_API_KEY') or os.environ.get('PERPLEXITY_API_KEY') or '').strip()


def http_post(url, payload, api_key_):
    """POST JSON to url with a Bearer token; returns the decoded JSON body."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), method='POST')
    req.add_header('Authorization', 'Bearer ' + api_key_)
    req.add_header('Content-Type', 'application/json')
    req.add_header('Accept', 'application/json')
    with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode('utf-8', 'replace'))


def search(query, api_key_, model=DEFAULT_MODEL, max_tokens=DEFAULT_MAX_TOKENS, http_post=http_post):
    """Run one Perplexity Sonar query and return {answer, citations, model}."""
    query = str(query or '').strip()
    if not query:
        raise ValueError('perplexity_search requires a non-empty query')
    payload = {
        'model': model,
        'messages': [{'role': 'user', 'content': query}],
        'max_tokens': int(max_tokens),
        'stream': False,
    }
    data = http_post(API_URL, payload, api_key_)
    try:
        answer = (data.get('choices') or [{}])[0].get('message', {}).get('content', '')
        citations = data.get('citations') or []
    except (AttributeError, IndexError, TypeError, ValueError):
        answer, citations = '', []
    return {'answer': answer, 'citations': citations, 'model': data.get('model') or model}
