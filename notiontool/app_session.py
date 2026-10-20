"""Reuse the NotionTool app's own Notion sign-in for the coworker account.

The NotionTool desktop app is already signed in. Instead of a separate sign-in window, its Notion cookies are
read over the app's local DevTools port and stored for the harness account. All Notion API calls keep running
through the owned Edge robots.txt tab (real Edge fingerprint); the cookies are installed there on the next
request. The identity is checked through that Edge tab; a different Notion user is rejected and the old
session is restored.
"""
import json
import time
import urllib.request

from .vendor.transport import _cookie_domain_allowed

DEFAULT_APP_PORT = 9333


def app_cookies(port=DEFAULT_APP_PORT, http_get=None, connect=None):
    """Notion cookies of the signed-in NotionTool app window (only Notion domains)."""
    get = http_get or (lambda url: json.loads(urllib.request.urlopen(url, timeout=3).read().decode()))
    targets = get('http://127.0.0.1:%d/json/list' % int(port))
    page = next((t for t in targets if t.get('type') == 'page' and 'notion' in (t.get('url') or '')), None)
    if not page:
        raise ValueError('The NotionTool app window is not open')
    if connect is None:
        from .notion_shell import CdpSession
        connect = CdpSession
    session = connect(page['webSocketDebuggerUrl'])
    try:
        found = session.send('Network.getAllCookies', {}, 10).get('cookies', [])
    finally:
        try:
            session.close()
        except Exception:
            pass
    cookies = [c for c in found if _cookie_domain_allowed(c.get('domain', ''))]
    if not any(c.get('name') == 'token_v2' and c.get('value') for c in cookies):
        raise ValueError('The NotionTool app is not signed in to Notion')
    return cookies


def adopt_app_session(adapter, account_id, port=DEFAULT_APP_PORT, cookies=None):
    """Store the app's sign-in for account_id, resume its pause and verify it through the Edge tab."""
    account = adapter.store.get('accounts', account_id)
    if not account:
        raise ValueError('Unknown account')
    cookies = cookies if cookies is not None else app_cookies(port)
    old = adapter.sessions.load(account_id)
    session = dict(old or {})
    session.update(cookies=cookies, source='app_session', created=time.time())

    def install(value):
        with adapter.lock:
            if value is None:
                return
            adapter.sessions.save(account_id, value)
            adapter.transports.pop(account_id, None)
            edge = adapter.edge_transports.get(account_id)
            if edge is not None:
                edge.ready_process = None  # reinstall the cookies in the robots.txt tab before the next request

    install(session)
    adapter.clear_account_pause(account_id)
    client = adapter.client(account_id)
    transport = type(client.transport).__name__
    try:
        info = client.identity()
    except Exception:
        install(old)
        raise
    expected = (account.get('identity') or {}).get('user_id')
    if expected and info.get('user_id') != expected:
        install(old)
        raise ValueError('The NotionTool app is signed in with a different Notion user')
    account = adapter.store.get('accounts', account_id)
    account.update(identity=info, error='')
    adapter.store.put('accounts', account)
    adapter.store.event('account_session_adopted', account_id=account_id, transport=transport)
    return {'account_id': account_id, 'transport': transport, 'verified': True}
