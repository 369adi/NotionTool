"""Account controls for the dedicated NotionTool window; never touch normal Notion."""
from .harness_account import selected_account
from .store import redact


def open_main(server, account_id):
    """After login/setup, open the persisted Main in the dedicated NotionTool window."""
    shell = getattr(getattr(server, 'desktop', None), 'shell', None)
    target = getattr(getattr(shell, 'app', None), 'session', None)
    if target is None or selected_account(server.store) != account_id:
        return False
    account = server.store.get('accounts', account_id, {}) or {}
    # A signed-out account must never re-open its Main chat (the old account's chats would stay visible).
    if account.get('state') == 'signin':
        return False
    thread = ((account.get('team_threads') or {}).get('main') or {}).get('id')
    if not thread:
        return False
    import uuid
    thread = str(uuid.UUID(thread))
    current = target.send('Runtime.evaluate', {'expression': 'location.href', 'returnByValue': True})
    url = (current.get('result') or {}).get('value') if isinstance(current, dict) else ''
    if isinstance(url, str):
        from urllib.parse import urlsplit, parse_qs
        parts = urlsplit(url)
        if parts.path == '/chat' and parse_qs(parts.query).get('t', [''])[0].replace('-', '') == thread.replace('-', ''):
            return True
    target.send('Page.navigate', {'url': 'https://app.notion.com/chat?t=' + thread.replace('-', '')})
    return True


def account_handlers(server):
    store, adapter = server.store, getattr(server, 'adapter', None)

    def session():
        shell = getattr(getattr(server, 'desktop', None), 'shell', None)
        found = getattr(getattr(shell, 'app', None), 'session', None)
        if found is None:
            raise ValueError('The NotionTool window is not connected. Reopen NotionTool and try again.')
        return found

    def idle(ident):
        workers = {w['id'] for w in store.all('workers') if w.get('account_id') == ident}
        if any(t.get('worker_id') in workers and
               (t.get('state') in ('running', 'preparing', 'cancelling', 'uncertain', 'checkpoint') or t.get('remote_unknown'))
               for t in store.all('tasks')):
            raise ValueError('Finish or stop this account\'s active tasks before changing its sign-in.')
        if any(r.get('account_id') == ident and r.get('state') in
               ('queued', 'approach', 'planning', 'executing', 'reviewing', 'fixing', 'integrating', 'paused')
               for r in store.all('runs')):
            raise ValueError('Finish or stop this account\'s runs before changing its sign-in.')

    def status(args):
        rows = []
        pause = getattr(getattr(adapter, 'network_budget', None), 'paused', None) or {}
        for a in store.all('accounts'):
            info = a.get('identity') or {}
            space = next((s.get('name', '') for s in info.get('spaces', []) if s.get('id') == info.get('space_id')), '')
            error = str(a.get('error') or '')[:250]
            if pause.get('account_id') == a['id']:
                error = 'Sign in required: ' + str(pause.get('reason') or 'Notion session expired')
            rows.append(dict(id=a['id'], name=info.get('user_name') or a.get('label') or a.get('email', ''),
                             email=a.get('email', ''), space_name=space, state=a.get('state', ''), error=redact(error)))
        return dict(accounts=rows, harness_account_id=selected_account(store) or '')

    def install(ident):
        from .window_login import to_cookie_params
        saved = adapter.sessions.load(ident) or {}
        cookies = saved.get('cookies') or []
        if not any(c.get('name') == 'token_v2' and c.get('value') for c in cookies):
            raise ValueError('This account needs to sign in again.')
        target = session()
        target.send('Network.clearBrowserCookies', {})
        target.send('Network.enable', {})
        target.send('Network.setCookies', {'cookies': to_cookie_params(cookies)})
        return target

    def sign_in(args):
        idle(selected_account(store))

        def login():
            result = adapter.browser_login()
            ident = result['account_id']
            target = install(ident)
            server.action('/api/account/select', {'account_id': ident})
            server.resume_after_sign_in(ident)
            setup = server.first_login_setup(ident)
            if setup.get('team_error'):
                raise ValueError('Could not finish Main and Coworker setup: ' + setup['team_error'])
            return dict(ok=True, account_id=ident)

        return server.job('Sign in to Notion', login, 'login')

    def select(args):
        ident = str(args.get('account_id') or '')
        if not store.get('accounts', ident):
            raise ValueError('Choose an existing account.')
        idle(selected_account(store))
        # Validate the saved login before changing selection or clearing window cookies.
        saved = adapter.sessions.load(ident) or {}
        if not any(c.get('name') == 'token_v2' and c.get('value') for c in saved.get('cookies', [])):
            raise ValueError('This account needs to sign in again.')
        server.action('/api/account/select', {'account_id': ident})
        install(ident)
        server.first_login_setup(ident)
        return dict(ok=True, account_id=ident)

    def sign_out(args):
        ident = str(args.get('account_id') or '')
        if not ident or ident != selected_account(store):
            raise ValueError('Choose the active account to sign out.')
        # Sign-out only clears the local session. Notion-side chats and runs continue over the API,
        # so nothing must ever block signing out (user rule).
        with server.engine.lock, adapter.lock:
            target = session()
            target.send('Network.clearBrowserCookies', {})
            # Force the window back to the login page. We deliberately do NOT wipe Notion's full site
            # data: that would strip the NotionTool shell state and leave a plain Notion page.
            try:
                target.send('Page.navigate', {'url': 'https://app.notion.com/login'})
            except Exception:
                pass
            for key in (ident, 'login_' + ident):
                adapter.sessions.delete(key)
            account = dict(store.get('accounts', ident))
            account.update(state='signin', error='Signed out. Sign in to continue.')
            store.put('accounts', account)
            for transports in (adapter.transports, adapter.edge_transports):
                old = transports.pop(ident, None)
                if old is not None and hasattr(old, 'close'):
                    try:
                        old.close()
                    except Exception as exc:
                        store.event('account_transport_close_failed', account_id=ident,
                                    message=type(exc).__name__)
            store.event('account_signed_out', account_id=ident, message='Signed out; chats and projects kept')
        return dict(ok=True, account_id=ident, navigate='https://app.notion.com/login')

    def setup(args):
        ident = selected_account(store)
        if not ident:
            raise ValueError('Sign in to an account first.')
        return server.job('Set up Main and five Welcome Coworkers', lambda: server.first_login_setup(ident), ident)

    return dict(account_status=status, account_sign_in=sign_in, account_sign_out=sign_out,
                account_select=select, account_setup=setup)
