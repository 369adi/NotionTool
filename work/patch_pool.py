import hashlib, pathlib, sys
ROOT = pathlib.Path(r'C:\Users\liket\Downloads\NotionTool-harness\NotionTool\notiontool')

def load(name, sha=None):
    raw = (ROOT / name).read_bytes()
    if sha and hashlib.sha256(raw).hexdigest() != sha:
        sys.exit(name + ' changed since reading; abort')
    text = raw.decode('utf-8')
    crlf = '\r\n' in text
    return text.replace('\r\n', '\n'), crlf

def once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        sys.exit('%s: anchor found %d times; abort' % (label, n))
    return text.replace(old, new)

def between(text, start, end, new, label):
    a = text.find(start); b = text.find(end, a + 1)
    if a < 0 or b < 0 or text.count(start) != 1:
        sys.exit(label + ': block anchors missing; abort')
    return text[:a] + new + text[b:]

notion, n_crlf = load('notion.py', '7cedbfb5db5397e38f4cf3379f09720dd2fb39b0cc31d736632e76b66871d2d5')
server, s_crlf = load('server.py', '9e4777cbc8d20caa8c6ec4eafbbfab466c1e5237834fc8946cf35024fa4738b1')
setup, u_crlf = load('setup_status.py')

# ---- notion.py: refresh applies the pool rules
notion = once(notion, "        return dict(account_id=account_id,threads=len(seen),state=account['state'])\n", r'''        try:
            pool = self.apply_chat_pool(account_id, client)
        except Exception as exc:
            pool = {}
            self.store.event('chat_pool_error', message=str(exc)[:200])
        return dict(account_id=account_id,threads=len(seen),state=account['state'],coworkers=pool.get('coworkers',0))
''', 'refresh')

METHODS = r'''    @staticmethod
    def thread_key(thread):
        tid = thread.get('id') or thread.get('thread_id') or thread.get('threadId')
        return str(uuid.UUID(tid)) if tid else ''

    def thread_records(self, client, ids):
        out = {}
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), 50):
            result = client.call('syncRecordValuesSpaceInitial', {'requests': [
                {'pointer': {'table': 'thread', 'id': t, 'spaceId': client.info['space_id']}, 'version': -1} for t in ids[i:i+50]]})
            out.update({k: unwrap(v) for k, v in result.get('recordMap', {}).get('thread', {}).items()})
        return out

    def apply_chat_pool(self, account_id, client):
        """Register the Main and apply the Coworker rule; marker renames happen only after a confirmed join."""
        from .chat_pool import COWORK_TITLE, MAIN_TITLES, pool_decision, pick_main
        account = self.store.get('accounts', account_id)
        workers = [w for w in self.store.all('workers') if w['account_id'] == account_id and w.get('state') != 'missing']
        ledger = account.get('team_threads') or {}
        main = pick_main(workers, (ledger.get('main') or {}).get('id', ''))
        if main and (main.get('purpose') != 'main' or not main.get('enabled')):
            main.update(purpose='main', enabled=True, welcome=True)
            self.store.put('workers', main)
            self.store.event('main_registered', message='Main chat registered: ' + main.get('label', ''), slot=account.get('slot'))
        if main and not (ledger.get('main') or {}).get('id'):
            ledger['main'] = {'id': main['thread_id'], 'pending': False}
            account['team_threads'] = ledger
            self.store.put('accounts', account)
        pool = [w for w in workers if w.get('purpose', 'worker') == 'worker' and (not main or w['id'] != main['id'])]
        records = self.thread_records(client, [w['thread_id'] for w in pool
            if w.get('label') == COWORK_TITLE or w.get('join_state') in ('pending', 'joined')])
        usable = 0
        for worker in pool:
            if worker.get('label') in MAIN_TITLES:
                changes, rename, event = dict(enabled=False), None, None
            else:
                changes, rename, event = pool_decision(worker, records.get(worker['thread_id']))
            if rename:
                try:
                    self.rename_thread(client, worker['thread_id'], rename)
                    changes['label'] = rename
                except Exception:
                    changes.update(enabled=False, join_state=worker.get('join_state') or 'pending')
                    event = 'coworker_rename_failed'
            if any(worker.get(k) != v for k, v in changes.items()):
                worker.update(changes)
                self.store.put('workers', worker)
                if event:
                    self.store.event(event, message=worker.get('label', '') + ' ' + worker['thread_id'], slot=account.get('slot'))
            usable += bool(worker.get('enabled') and worker.get('join_state') == 'joined')
        return {'main': main['id'] if main else '', 'coworkers': usable}

    def ensure_main_chat(self, account_id):
        """Local chat record for the account's Main; existing history is kept, never deleted."""
        from .chat_pool import pick_main
        from .store import uid
        account = self.store.get('accounts', account_id)
        workers = [w for w in self.store.all('workers') if w['account_id'] == account_id]
        main = pick_main(workers, ((account.get('team_threads') or {}).get('main') or {}).get('id', ''))
        if not main or main.get('purpose') != 'main':
            return None
        chat = next((c for c in self.store.all('chats') if c.get('main_worker') == main['id']), None)
        if not chat:
            chat = dict(id=uid('c'), title=account.get('label', account.get('email', '')) + ' · Main', main_worker=main['id'],
                        folder_id='', project_path='', created=time.time())
            self.store.put('chats', chat)
            self.store.event('main_chat_registered', message=chat['title'], slot=account.get('slot'))
        return chat

    def create_welcome(self, client, tid, model):
        """Create one Welcome chat. Returns (listed, clean). No retry: a timeout may still have created it."""
        steps, failed = [], False
        try:
            for line in client.transport.stream('runInferenceTranscript', self.welcome_body(client, tid, model), client.headers(), timeout=180):
                event = json.loads(line)
                if event.get('type') in ('patch-start', 'patch-sync'):
                    steps = (event.get('data') or {}).get('s') or steps
                elif event.get('type') == 'patch':
                    steps = apply_patch(steps, event.get('v'))
        except (OSError, json.JSONDecodeError):
            failed = True
        # HTTP 200 does not confirm a persisted chat; only Notion's chat list does.
        listed = any(self.thread_key(t) == str(uuid.UUID(tid)) for t in client.threads())
        clean = not failed and not any(isinstance(s, dict) and s.get('type') == 'error' for s in steps)
        return listed, clean

    def ensure_main(self, account_id):
        """Load the account's Main (title Main or Main Chat); create one Welcome chat named Main only if none exists."""
        from .chat_pool import MAIN_TITLE
        self.refresh(account_id)
        chat = self.ensure_main_chat(account_id)
        if chat:
            return {'account_id': account_id, 'chat_id': chat['id'], 'created': False}
        account = self.store.get('accounts', account_id)
        if account.get('state') != 'ready':
            raise ValueError('Sign in to this account again before loading its Main chat')
        client = self.client(account_id)
        self.require_workspace_ai(account_id, client.identity())
        ledger = account.get('team_threads') or {}
        item = ledger.get('main') or {}
        known = {w['thread_id'] for w in self.store.all('workers') if w['account_id'] == account_id and w.get('state') != 'missing'}
        if item.get('pending') and item.get('id') in known:
            tid = item['id']
        elif item.get('pending'):
            raise ValueError('A previous Main creation is unconfirmed. Refresh the account before creating again.')
        else:
            model = client.model()
            tid = self.new_id(client.info['space_id'], '00a9')
            ledger['main'] = {'id': tid, 'pending': True}
            account['team_threads'] = ledger
            self.store.put('accounts', account)
            listed, clean = self.create_welcome(client, tid, model)
            if not listed:
                raise ValueError('Main creation is unconfirmed; refresh to reconcile before creating again')
            if not clean:
                self.store.event('welcome_warning', message='Main exists, but its greeting returned an upstream error', slot=account.get('slot'))
        self.rename_thread(client, tid, MAIN_TITLE)
        account = self.store.get('accounts', account_id)
        account['team_threads']['main'] = {'id': tid, 'pending': False}
        self.store.put('accounts', account)
        self.transaction(client, 'newUserHomeActions.savePostOnboardingAgentConfig', [{
            'pointer': {'table': 'user_settings', 'id': client.info['user_id']}, 'path': ['settings'], 'command': 'update',
            'args': {'post_onboarding_agent_config': {'thread_id': tid, 'has_clicked_all_set': False, 'space_id': client.info['space_id']}}}])
        self.refresh(account_id)
        chat = self.ensure_main_chat(account_id)
        if not chat:
            raise ValueError('Created Main chat is absent from the Notion chat list')
        self.store.event('team_setup', message='Main Welcome chat created', slot=account.get('slot'))
        return {'account_id': account_id, 'chat_id': chat['id'], 'created': True}

    def bridge_alive(self, account_id, url):
        """True while the account still has an enabled, live NotionTool MCP module for this URL."""
        client = self.client(account_id)
        client.identity()
        view = next((unwrap(v) for v in client.info.get('records', {}).get('space_view', {}).values()
                     if unwrap(v).get('space_id') == client.info['space_id']), None)
        if not view:
            return False
        ids = [m['pointer']['id'] for m in ((view.get('settings') or {}).get('agent_chat_modules') or [])
               if (m.get('pointer') or {}).get('table') == 'workflow_module' and m.get('defaultEnabled') is not False]
        if not ids:
            return False
        synced = client.call('syncRecordValuesSpaceInitial', {'requests': [
            {'pointer': {'table': 'workflow_module', 'id': i, 'spaceId': client.info['space_id']}, 'version': -1} for i in ids]})
        return any(value.get('alive', True) and ((value.get('data') or {}).get('serverUrl') or value.get('server_url')) == url
                   for value in (unwrap(v) for v in synced.get('recordMap', {}).get('workflow_module', {}).values()))

    def flag_bridge_unavailable(self, account_id):
        account = self.store.get('accounts', account_id)
        if not account:
            return
        account.update(bridge_connected=False, bridge_next_at=0, bridge_checked_at=0,
                       bridge_error='A worker reported the NotionTool MCP connection as unavailable')
        self.store.put('accounts', account)
        self.store.event('mcp_unavailable', message='NotionTool MCP missing in a worker chat; reconnecting automatically', slot=account.get('slot'))

'''

PROVISION = r'''    def provision(self, account_id, count):
        count = int(count)
        if not 1<=count<=40:
            raise ValueError('Choose between 1 and 40 new worker chats')
        client = self.client(account_id)
        self.require_workspace_ai(account_id, client.identity())
        model = client.model()
        created, new_ids = 0, []
        for _ in range(count):
            tid = self.new_id(client.info['space_id'],'00a9')
            # No automatic retry: a creation timeout could still have created the thread.
            listed, clean = self.create_welcome(client, tid, model)
            if not listed:
                self.store.event('welcome_warning', message='Coworker creation is unconfirmed; refresh before creating again')
                break
            if not clean:
                self.store.event('welcome_warning', message='Coworker exists, but its greeting returned an upstream error')
            created += 1
            new_ids.append(str(uuid.UUID(tid)))
        self.refresh(account_id)
        # New Welcome chats keep their name and stay unused until Notion confirms the join.
        for worker in self.store.all('workers'):
            if worker['account_id'] == account_id and worker['thread_id'] in new_ids:
                worker.update(join_state='pending', enabled=False, welcome=True, purpose='worker')
                self.store.put('workers', worker)
        self.apply_chat_pool(account_id, client)
        joined = sum(1 for w in self.store.all('workers') if w['account_id'] == account_id
                     and w['thread_id'] in new_ids and w.get('join_state') == 'joined')
        return {'created': created, 'joined': joined, 'pending': created - joined}

'''
notion = between(notion, '    def provision(self, account_id, count):\n', '    def welcome_body(', METHODS + PROVISION, 'provision')

TEAM = r'''    def ensure_team(self,account_id):
        """Idempotent desired team: one Main and five Welcome Coworkers; Coworkers are renamed only after joining."""
        self.ensure_main(account_id)
        client=self.client(account_id)
        model=client.model()
        account=self.store.get('accounts',account_id)
        ledger=account.get('team_threads') or {}
        known={self.thread_key(t) for t in client.threads() if self.thread_key(t)}
        roles=['worker1','worker2','worker3','worker4','worker5']
        # Existing pool chats fill empty slots first; nothing is created twice.
        taken={(item or {}).get('id') for item in ledger.values()}
        spare=[w for w in self.store.all('workers') if w['account_id']==account_id and w.get('purpose','worker')=='worker'
               and w.get('state')!='missing' and w.get('join_state') in ('joined','pending') and w['thread_id'] not in taken]
        for role in roles:
            if not ledger.get(role) and spare:
                ledger[role]={'id':spare.pop(0)['thread_id'],'pending':False}
        account['team_threads']=ledger
        self.store.put('accounts',account)
        new_ids=[]
        for role in roles:
            item=ledger.get(role)
            if item and item['id'] not in known:
                if item.get('pending'):
                    raise ValueError('A previous Welcome creation is unconfirmed. Refresh the account before creating again.')
                raise ValueError('A managed Welcome chat is missing; existing chats were preserved')
            if item:
                continue
            tid=self.new_id(client.info['space_id'],'00a9')
            item={'id':tid,'pending':True}
            ledger[role]=item
            account['team_threads']=ledger
            self.store.put('accounts',account)
            listed,clean=self.create_welcome(client,tid,model)
            if not listed:
                raise ValueError('Welcome creation is unconfirmed; refresh to reconcile before creating again')
            if not clean:
                self.store.event('welcome_warning',message=role+' exists, but its initial greeting returned an upstream error',slot=account['slot'])
            item['pending']=False
            account['team_threads']=ledger
            self.store.put('accounts',account)
            new_ids.append(str(uuid.UUID(tid)))
            self.store.event('team_setup',message=role+' Welcome chat created; waiting for its join',slot=account['slot'])
        self.refresh(account_id)
        for worker in self.store.all('workers'):
            if worker['account_id']==account_id and worker['thread_id'] in new_ids:
                worker.update(join_state='pending',enabled=False,welcome=True,purpose='worker')
                self.store.put('workers',worker)
        self.apply_chat_pool(account_id,client)
        managed={ledger[r]['id'] for r in roles}
        team=[w for w in self.store.all('workers') if w['account_id']==account_id and w['thread_id'] in managed]
        if len(team)<len(managed):
            raise ValueError('Created Welcome chat is absent from the Notion chat list')
        joined=sum(1 for w in team if w.get('join_state')=='joined' and w.get('enabled'))
        from .setup_status import account_setup
        status=account_setup(self.store.get('accounts',account_id),self.store.all('workers'))
        return {'account_id':account_id,'welcome_chats':len(managed)+1,'workers':joined,'pending_workers':len(managed)-joined,
                'main_chats':1,'verified':status['complete'],'chat_verified':True,'setup':status}

'''
notion = between(notion, '    def ensure_team(self,account_id):\n', '    def run(', TEAM, 'ensure_team')

notion = once(notion, "            errors = [s for s in steps if isinstance(s,dict) and s.get('type')=='error']\n            text = stream_text(steps)\n",
    "            errors = [s for s in steps if isinstance(s,dict) and s.get('type')=='error']\n            text = stream_text(steps)\n"
    "            from .chat_pool import script_mode_failure\n"
    "            if script_mode_failure(steps):\n"
    "                self.flag_bridge_unavailable(worker['account_id'])\n", 'run')

# ---- setup_status: only usable (enabled) chats count
setup = once(setup, "own=[w for w in workers if w.get('account_id')==account.get('id') and w.get('welcome')]",
    "own=[w for w in workers if w.get('account_id')==account.get('id') and w.get('welcome') and w.get('enabled')]", 'setup')

# ---- server.py
server = once(server, """            with self.engine.lock:
                return select_account(self.store, account_id)
""", """            with self.engine.lock:
                result = select_account(self.store, account_id)
            # The chosen account's Main is loaded (created once if missing); nothing is deleted.
            self.job_once('Load Main chat and MCP', lambda: self.prepare_account(account_id), 'main:'+account_id)
            return result
""", 'select')

server = once(server, """            return self.job('Sign in to Notion in the browser window',self.adapter.browser_login,'login')
""", """            def login():
                from .harness_account import selected_account
                result = self.adapter.browser_login()
                if result.get('account_id') and result['account_id'] == selected_account(self.store):
                    result.update(self.prepare_account(result['account_id']))
                return result
            return self.job('Sign in to Notion in the browser window',login,'login')
""", 'login')

server = once(server, """    def finish_account_result(self, result):
""", """    def prepare_account(self, account_id):
        \"\"\"After sign-in or an account switch: load the Main and set up the Tailscale MCP bridge automatically.\"\"\"
        result = {'account_id': account_id}
        try:
            result['main'] = self.adapter.ensure_main(account_id)
        except Exception as exc:
            result['main_error'] = str(exc)[:250]
            self.store.event('main_load_failed', message=str(exc)[:250])
        try:
            bridge = self.start_tunnel()
            result['bridge'] = bridge
            account = self.store.get('accounts', account_id, {})
            self.store.event('mcp_auto_setup', message=('NotionTool MCP connected via Tailscale' if account.get('bridge_connected')
                             else 'Tailscale ready; MCP connection pending: ' + str(account.get('bridge_error', ''))[:200]))
        except Exception as exc:
            result['bridge_error'] = str(exc)[:250]
            account = self.store.get('accounts', account_id)
            if account:
                account['bridge_error'] = str(exc)[:250]
                self.store.put('accounts', account)
            self.store.event('mcp_auto_setup_failed', message=str(exc)[:250])
        return result

    def finish_account_result(self, result):
""", 'prepare')

server = once(server, """            if account['state']!='ready' or account.get('bridge_blocked') or account.get('bridge_next_at',0)>time.time():continue
            if account.get('bridge_connected') and account.get('bridge_url')==result['url']:
                connected+=1;continue
            try:
                self.adapter.connect_bridge(account['id'],result['url'],self.bridge_token)
                connected+=1
""", """            if account['state']=='ready' and time.time()-account.get('pool_checked_at',0)>120 and any(
                    w['account_id']==account['id'] and w.get('join_state')=='pending' and w.get('state')!='missing'
                    for w in self.store.all('workers')):
                fresh=self.store.get('accounts',account['id']);fresh['pool_checked_at']=time.time();self.store.put('accounts',fresh)
                try:self.adapter.refresh(account['id'])  # renames Coworkers whose join is now confirmed
                except Exception:pass
                account=self.store.get('accounts',account['id'])
            if account['state']!='ready' or account.get('bridge_blocked') or account.get('bridge_next_at',0)>time.time():continue
            if account.get('bridge_connected') and account.get('bridge_url')==result['url']:
                if time.time()-account.get('bridge_checked_at',0)<300:
                    connected+=1;continue
                try:alive=self.adapter.bridge_alive(account['id'],result['url'])
                except Exception:alive=True  # a failed check never tears down a working bridge
                fresh=self.store.get('accounts',account['id']);fresh['bridge_checked_at']=time.time();self.store.put('accounts',fresh)
                if alive:
                    connected+=1;continue
                self.store.event('mcp_missing',message='NotionTool MCP is no longer active in the account; reconnecting',slot=account.get('slot'))
            try:
                self.adapter.connect_bridge(account['id'],result['url'],self.bridge_token)
                connected+=1
                if account.get('bridge_url'):
                    self.store.event('mcp_reconnected',message='NotionTool MCP reconnected automatically',slot=account.get('slot'))
                fresh=self.store.get('accounts',account['id']);fresh['bridge_checked_at']=time.time();self.store.put('accounts',fresh)
""", 'tunnel')

def save(name, text, crlf):
    (ROOT / name).write_bytes((text.replace('\n', '\r\n') if crlf else text).encode('utf-8'))

for src, name in ((notion, 'notion.py'), (server, 'server.py'), (setup, 'setup_status.py')):
    compile(src, name, 'exec')
save('notion.py', notion, n_crlf); save('server.py', server, s_crlf); save('setup_status.py', setup, u_crlf)
print('patched', n_crlf, s_crlf, u_crlf)
