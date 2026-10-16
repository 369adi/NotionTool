"""Browser-free adapter built from Refresher's native transport and REA captures."""
from __future__ import annotations
from pathlib import Path
import copy
import datetime
import json
import os
import re
import secrets
import threading
import time
import uuid
import hashlib

from .engine import PendingRemote, RateLimited, ResetNotAccepted
from .vendor.sessions import SessionStore
from .vendor.transport import NativeNotionTransport, NotionHttpError
from .network_budget import NetworkBudget, request_priority
from .model_verification import verify_execution,ModelVerificationError,turn_status
from .telemetry import http_class


def unwrap(value):
    while isinstance(value, dict) and isinstance(value.get('value'), dict):
        value = value['value']
    return value or {}


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(text_content(x) for x in value)
    if isinstance(value, dict) and value.get('type') == 'text':
        return text_content(value.get('content', value.get('text', '')))
    return ''


def apply_patch(steps, operations):
    root = {'s':steps}
    for op in operations or []:
        path = [x.replace('~1','/').replace('~0','~') for x in op.get('p','').split('/')[1:]]
        if not path:
            continue
        try:
            current = root
            for key in path[:-1]:
                current = current[int(key)] if isinstance(current,list) else current[key]
            key = path[-1]
            if op.get('o') == 'r':
                current.pop(int(key)) if isinstance(current,list) else current.pop(key,None)
            elif key == '-':
                current.append(op['v'])
            elif op.get('o') == 'x':
                if isinstance(current,list):
                    current[int(key)] += op['v']
                else:
                    current[key] += op['v']
            elif isinstance(current,list):
                index = int(key)
                current.append(op['v']) if index == len(current) else current.__setitem__(index,op['v'])
            else:
                current[key] = op['v']
        except (KeyError,IndexError,ValueError,TypeError):
            continue
    return root['s']


def stream_text(steps):
    return '\n\n'.join(text_content(s.get('value',[])) for s in steps
                       if isinstance(s,dict) and s.get('type') in ('agent-inference','assistant_message'))

def not_accepted(task, transcript, fresh, grace=30):
    """True only when Notion completed a later turn without committing this step."""
    session = transcript.get('session') or {}
    sent = task.get('sent_at', 0)
    if transcript.get('session_pending') or session.get('status') not in ('idle', 'completed'):
        return False
    if not sent or time.time() - sent < grace:
        return False

    def stamp(entity):
        value = entity.get('created_at') or entity.get('createdAt') or entity.get('created_time') or 0
        if isinstance(value, (int, float)):
            return value / 1000 if value > 1e11 else value
        try:
            return datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        except ValueError:
            return 0
    return any(e.get('kind') == 'assistant_message' and stamp(e) > sent for e in fresh)


MODEL_CATALOG_TTL = 600  # seconds; a cached model catalog is read again after this or on a miss


def own_turn_steps(records, ids, step_id, chunk=20):
    # Read thread messages newest first until our own user step is loaded. Only the
    # own turn is checked, so older history is skipped; without a match all is read.
    steps, start, read = [], len(ids), 0
    while start > 0:
        part = ids[max(0, start - chunk):start]
        messages = records('thread_message', part)
        steps = [dict((messages[ident] or {}).get('step', {})) for ident in part if ident in messages] + steps
        read += len(part)
        start -= len(part)
        if step_id and any(s.get('type') == 'user' and s.get('id') == step_id for s in steps):
            break
        chunk = min(chunk * 5, 100)
    return steps, read


class VerifiedReply(str):
    """Own committed turn; proof flags cannot be supplied by model JSON."""
    def __new__(cls,text,tool_activity):
        value=super().__new__(cls,text)
        value.verified_terminal_reply=True
        value.tool_activity=bool(tool_activity)
        return value


class NotionClient:
    def __init__(self, transport, identity=None):
        self.transport = transport
        self.info = identity or {}

    def headers(self):
        # Login endpoints reject unversioned clients even before authentication.
        # Bootstrap version comes from the bundled REA capture; identity/session
        # can override it without changing the adapter.
        origin = getattr(self.transport,'_base','https://app.notion.com')
        result = {'notion-audit-log-platform':'web','Origin':origin,'Referer':origin+'/login',
                  'notion-client-version':self.info.get('client_version') or '23.13.20261011.1430'}
        for key, header in (('space_id','x-notion-space-id'),('user_id','x-notion-active-user-header'),('client_version','notion-client-version')):
            if self.info.get(key):
                result[header] = self.info[key]
        return result

    def call(self, path, body, timeout=45):
        status, text = self.transport.request(path,body,headers=self.headers(),timeout=timeout)
        if status == 429:
            raise RateLimited(getattr(self.transport, 'retry_after', 30))
        if status != 200:
            raise ValueError(f'Notion {path}: {text}')
        return json.loads(text)

    def identity(self):
        data = self.call('loadUserContent',{})
        records = data.get('recordMap',{})
        users = records.get('notion_user',{})
        if not users:
            raise ValueError('Notion login required')
        user_id = next(iter(users))
        user = unwrap(users[user_id])
        spaces = [dict(id=ident, name=unwrap(value).get('name','Workspace'))
                  for ident,value in records.get('space',{}).items() if unwrap(value).get('alive',True)]
        self.info = dict(user_id=user_id, email=user.get('email',''), user_name=user.get('name',''),
                         client_version=self.info.get('client_version') or '23.13.20261011.1430',
                         space_id=self.info.get('space_id') or (spaces[0]['id'] if spaces else ''),
                         spaces=spaces, onboarded=user.get('onboarding_completed'), records=records)
        return self.info

    def model(self, selected='opus'):
        from .model_routing import MODELS, choice
        label = MODELS[choice(selected)]
        def pick(models):
            return next((m for m in models if m.get('modelProvider')=='anthropic' and not m.get('isDisabled')
                         and re.sub(r'\s+',' ',m.get('modelMessage','')).strip().lower()==label.lower()), None)
        # The catalog is cached per workspace on the long-lived transport of the account.
        space = self.info['space_id']
        cache = getattr(self.transport, 'model_catalog', None)
        if not isinstance(cache, dict):
            cache = {}
            try:
                self.transport.model_catalog = cache
            except AttributeError:
                pass
        entry = cache.get(space)
        model = pick(entry[1]) if entry and time.monotonic() - entry[0] < MODEL_CATALOG_TTL else None
        if not model or not model.get('model'):
            # Missing, expired or label not in the cached list: read the catalog once.
            models = self.call('getAvailableModels',{'spaceId':space,'surface':'workspace_model_settings'}).get('models',[])
            cache[space] = (time.monotonic(), models)
            model = pick(models)
        if not model or not model.get('model'):
            raise ValueError(label+' is not available for this account. No model substitution was made.')
        return model['model']

    def threads(self):
        return self.call('getInferenceTranscriptsForUser',{'threadParentPointer':{
            'table':'space','id':self.info['space_id'],'spaceId':self.info['space_id']},
            'includeWorkflowThreads':True,'includeWriterChats':False}).get('transcripts',[])

    def transcript(self, thread):
        return self.call('getThreadTranscript',{'spaceId':self.info['space_id'],'threadId':thread,
                                             'direction':'backward','limit':100})


class NotionAdapter:
    single_account_harness = True
    semantic_context = True
    response_mode='plain'
    task_model_routing=True
    def __init__(self, store):
        self.store = store
        self.sessions = SessionStore(data_dir=str(store.directory / 'sessions'))
        self.transports = {}
        self.edge_transports = {}
        self.lock = threading.RLock()
        self.setup_locks = {}
        self.template = json.loads((Path(__file__).parent / 'template.json').read_text(encoding='utf-8'))
        self.welcome = json.loads((Path(__file__).parent / 'welcome.json').read_text(encoding='utf-8'))
        self.network_budget = NetworkBudget(store)
        self.confirmed_configs = {}  # thread_id -> (thread_id, generation, model) after a confirmed config check

    def client(self, account_id, anonymous=False):
        with self.lock:
            if account_id not in self.transports:
                session = self.sessions.load(account_id) or {}
                if not anonymous and not session.get('cookies'):
                    raise ValueError('Sign in to this Notion account first')
                self.transports[account_id] = NativeNotionTransport(session,session_store=self.sessions,session_key=account_id,budget=self.network_budget)
            account = self.store.get('accounts',account_id,{})
            transport = self.transports[account_id]
            from .harness_account import selected_account
            if (not anonymous and account.get('identity', {}).get('user_id')
                    and selected_account(self.store) == account_id
                    and self.store.settings().get('notion_transport', 'edge') == 'edge'):
                transport = self.edge_transport(account_id)
            return NotionClient(transport,account.get('identity'))

    def edge_transport(self, account_id):
        with self.lock:
            if account_id not in self.edge_transports:
                from .edge_transport import EdgeTransport
                self.edge_transports[account_id] = EdgeTransport(self.store, self.sessions, account_id, self.network_budget)
            return self.edge_transports[account_id]

    def create_account(self, email, slot=None):
        email = email.strip().lower()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):
            raise ValueError('Enter a valid email address')
        with self.lock:
            existing = next((a for a in self.store.all('accounts') if a['email']==email),None)
            if existing:
                return existing
            used = {int(a['slot']) for a in self.store.all('accounts')}
            slot = slot or next(n for n in range(1,1001) if n not in used)
            account = dict(id=uuid.uuid4().hex,slot=slot,email=email,label=f'Slot {slot}',
                           state='login',identity={},model='',bridge_connected=False,cooldown_until=0)
            self.store.put('accounts',account)
            settings = self.store.settings()
            settings['harness_account_id'] = account['id']
            self.store.put('settings', settings)
            return account

    def onboard(self, account_id):
        client = self.client(account_id)
        info = client.identity()
        if not info['spaces']:
            client.call('recordCookieConsent',{'permission':{'necessary':True,'performance':False,
                'preference':False,'targeting':False},'document_version':'v10','userId':info['user_id']})
            result = client.call('createSpace',dict(name='NotionTool Workspace',icon='💼',planType='team',
                planSelection='team',initialPersona='unfilled',domainType='professional',deviceId=str(uuid.uuid4()),
                deviceType='web-desktop',source='handle_root_redirect',createSpaceView=True))
            info['space_id'] = result['spaceId']
        if not info['onboarded']:
            client.call('completeCurrentUserOnboarding',{'completion':'standard'})
        account = self.store.get('accounts',account_id)
        account['identity'] = client.identity()
        self.store.put('accounts',account)
        return self.refresh(account_id)

    @staticmethod
    def workspace_ai_access(info):
        space = unwrap(info.get('records', {}).get('space', {}).get(info.get('space_id'), {}))
        settings = space.get('settings', {})
        flags = {key: settings.get(key) for key in (
            'enable_ai_feature', 'disable_ai_feature', 'notion_admin_disabled_ai_feature')}
        disabled = flags['disable_ai_feature'] is True or flags['notion_admin_disabled_ai_feature'] is True
        return dict(state='disabled' if disabled else 'enabled' if flags['enable_ai_feature'] is True else 'unknown', **flags)

    def require_workspace_ai(self, account_id, info):
        access = self.workspace_ai_access(info)
        if access['state'] == 'disabled':
            account = self.store.get('accounts', account_id)
            account.update(state='ai_disabled', ai_access=access,
                           error='Notion AI is disabled for this workspace.')
            self.store.put('accounts', account)
            raise ValueError(account['error'])

    def refresh(self, account_id):
        client = self.client(account_id)
        info = client.identity()
        account = self.store.get('accounts',account_id)
        if info['email'].lower()!=account['email'].lower():
            raise ValueError('Account identity mismatch')
        account.update(identity=info,state='ready',error='')
        account['ai_access'] = self.workspace_ai_access(info)
        if account['ai_access']['state'] == 'disabled':
            account.update(state='ai_disabled', model='',
                           error='Notion AI is disabled for this workspace.')
        else:
            try:
                account['model'] = client.model()
            except ValueError as exc:
                account.update(state='model_unavailable',error=str(exc),model='')
        self.store.put('accounts',account)
        existing = {w['thread_id']:w for w in self.store.all('workers') if w['account_id']==account_id}
        archived = {g['id'] for g in self.store.all('thread_generations') if g.get('account_id') == account_id}
        seen = set()
        for thread in client.threads():
            tid = thread.get('id') or thread.get('thread_id') or thread.get('threadId')
            if not tid:
                continue
            tid = str(uuid.UUID(tid))
            seen.add(tid)
            if tid in archived:
                continue
            worker = existing.get(tid) or dict(id=account_id+':'+tid,account_id=account_id,thread_id=tid,
                purpose='worker',enabled=False,state='ready',lease='',last_used=0)
            worker['label'] = thread.get('title') or 'Untitled chat'
            if worker['state'] in ('missing','error') and not worker.get('lease'):
                worker['state'] = 'ready'
            self.store.put('workers',worker)
        for tid,worker in existing.items():
            if tid not in seen and worker['state'] not in ('busy','recovering'):
                worker.update(state='missing',enabled=False)
                self.store.put('workers',worker)
        try:
            pool = self.apply_chat_pool(account_id, client)
        except Exception as exc:
            pool = {}
            self.store.event('chat_pool_error', message=str(exc)[:200])
        return dict(account_id=account_id,threads=len(seen),state=account['state'],coworkers=pool.get('coworkers',0))

    NOTION_COOKIE_DOMAINS=('notion.so','notion.com')

    def browser_login(self, timeout=1200):
        """Open the real Notion login page in an owned Edge window and keep the session the user signs in with."""
        from .browser import Browser
        import shutil
        if getattr(self,'_browser_login_active',False):raise ValueError('A Notion sign-in window is already open')
        self._browser_login_active=True
        browser=Browser(self.store,profile_name='login-'+uuid.uuid4().hex[:12],start_url='https://www.notion.so/login',edge_only=True)
        cookies=[]
        try:
            browser.ensure()
            deadline=time.time()+timeout
            while time.time()<deadline:
                if browser.process is None or browser.process.poll() is not None:break
                try:
                    if not [t for t in browser.http('/json/list') if t.get('type')=='page']:break
                    found=browser.send(browser.http('/json/version'),'Storage.getCookies',{},timeout=10).get('cookies',[])
                except Exception:
                    break
                notion=[c for c in found if str(c.get('domain','')).lstrip('.').lower().endswith(self.NOTION_COOKIE_DOMAINS)]
                if any(c.get('name')=='token_v2' and c.get('value') for c in notion):
                    cookies=notion
                    try:
                        NotionClient(NativeNotionTransport({'cookies':notion})).identity()
                        break  # signed in: finish without waiting for the user to close the window
                    except Exception:
                        pass
                time.sleep(1.5)
        finally:
            self._browser_login_active=False
            profile=getattr(browser,'profile',None)
            browser.close()
            if profile:shutil.rmtree(profile,ignore_errors=True)
        if not cookies:raise ValueError('No Notion sign-in was detected. Sign in completely before closing the window.')
        session={'cookies':cookies,'source':'browser_login','created':time.time()}
        info=NotionClient(NativeNotionTransport(session)).identity()
        email=info['email'].lower()
        existing=next((a for a in self.store.all('accounts') if a['email'].lower()==email),None)
        had_accounts=bool(self.store.all('accounts'))
        account=existing or self.create_account(email)
        self.sessions.save(account['id'],session)
        self.transports.pop(account['id'],None)
        account=self.store.get('accounts',account['id'])
        account.update(identity=info,state='signed_in',error='')
        self.store.put('accounts',account)
        try:self.refresh(account['id'])
        except Exception as exc:
            account=self.store.get('accounts',account['id']);account['error']=str(exc)[:200];self.store.put('accounts',account)
        from .harness_account import select_account
        if not had_accounts or existing:
            try:select_account(self.store,account['id'])
            except Exception:pass
        return {'account_id':account['id'],'email':email,'state':self.store.get('accounts',account['id']).get('state')}

    def remove_account(self, account_id):
        """Forget an account locally: saved sign-in, workers and local chat list. Nothing is deleted in Notion."""
        with self.lock:
            account=self.store.get('accounts',account_id)
            if not account:raise ValueError('Account not found')
            workers=[w for w in self.store.all('workers') if w.get('account_id')==account_id]
            if any(w.get('state')=='busy' for w in workers):raise ValueError('A task is still running on this account; stop it first')
            ids={w['id'] for w in workers}
            for chat in list(self.store.all('chats')):
                if chat.get('main_worker') in ids:self.store.delete('chats',chat['id'])
            for worker in workers:self.store.delete('workers',worker['id'])
            transport=self.transports.pop(account_id,None)
            try:
                if transport is not None and hasattr(transport,'close'):transport.close()
            except Exception:pass
            for key in (account_id,'login_'+account_id):
                try:self.sessions.delete(key)
                except Exception:pass
            self.store.delete('accounts',account_id)
            settings=self.store.settings()
            if settings.get('harness_account_id')==account_id:
                settings['harness_account_id']='';self.store.put('settings',settings)
        return {'removed':account_id,'email':account['email']}

    def body(self, client, thread_id, step_id, prompt, model, create=False, computer=False):
        body = copy.deepcopy(self.template)
        info = client.info
        config = body['transcript'][0]
        config['id'] = self.new_id(info['space_id'],'00aa')
        # Notion AI reaches MCP connections (NotionTool, Screen, Roblox, Blender) only with the
        # tool environment enabled; plain text turns stay without it to keep latency low.
        config['value'].update(model=model,modelFromUser=True,enableComputer=bool(computer),
                              agentMemorySettings={'useMemories':True,'excludeChatFromMemories':True})
        # Only the real Welcome chat creation (welcome_body) may start onboarding.
        config['value'].pop('agentMessageKey', None)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        views = info.get('records',{}).get('space_view',{})
        view = next((unwrap(v).get('id',k) for k,v in views.items() if unwrap(v).get('space_id')==info['space_id']), '')
        body['transcript'] += [dict(id=self.new_id(info['space_id'],'00aa'),type='context',value=dict(
            userId=info['user_id'],spaceId=info['space_id'],surface='full_page_chat',timezone='Europe/Berlin',
            userName=info.get('user_name',''),userEmail=info['email'],spaceViewId=view,currentDatetime=now)),
            dict(id=step_id,type='user',userId=info['user_id'],value=[[prompt]],createdAt=now)]
        body.update(spaceId=info['space_id'],threadId=thread_id,traceId=str(uuid.uuid4()),submittedUserStepId=step_id,
                    createThread=create,generateTitle=create)
        if create:
            body['threadParentPointer'] = {'table':'space','id':info['space_id'],'spaceId':info['space_id']}
        return body

    @staticmethod
    def new_id(space_id, tail='00aa'):
        space = uuid.UUID(space_id)
        # Legacy spaces do not encode a space short ID. The public client
        # falls back to UUIDv4 when no encoded short ID is available.
        if space.version != 8:
            return str(uuid.uuid4())
        # Notion IdCreator (module 817569) encodes UTC days since 2024-01-01
        # in the first 12 bits. A captured example is not a reusable prefix.
        day = int((time.time()-1704067200)//86400)
        if not 0 <= day < 4096:
            raise ValueError('Current time is outside the supported Notion ID date range')
        parts = str(space).split('-')
        return f'{day:03x}{parts[0][3:]}-{parts[1]}-80{secrets.token_hex(1)}-{secrets.choice("89ab")}{secrets.token_hex(2)[:3]}-{tail}{secrets.token_hex(4)}'

    @staticmethod
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

    def note_worker_mcp_failure(self, worker):
        """A worker chat could not use the MCP. Never reconnect from here: a reconnect could disturb the
        other Coworkers. Only schedule a read-only check of the account's MCP module for the next minute."""
        account = self.store.get('accounts', worker['account_id'])
        if not account:
            return
        account.update(bridge_checked_at=0, bridge_suspect_at=time.time())
        self.store.put('accounts', account)
        self.store.event('worker_mcp_unavailable', message='NotionTool MCP was unavailable in ' + str(worker.get('label') or 'a worker chat')
                         + '; checking the account connection without interrupting other Coworkers', slot=account.get('slot'))

    def provision(self, account_id, count):
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

    def welcome_body(self,client,tid,model):
        """Exactly the captured Refresher Welcome protocol; no browser onboarding clicks."""
        info=client.info
        config=copy.deepcopy(self.welcome['config'])
        config.update(agentMessageKey='unboxing_m5',model=model,modelFromUser=True,enableComputer=False,
                      agentMemorySettings={'useMemories':True,'excludeChatFromMemories':True})
        view=next((unwrap(v).get('id',k) for k,v in info.get('records',{}).get('space_view',{}).items()
                   if unwrap(v).get('space_id')==info['space_id']),'')
        body=copy.deepcopy(self.welcome['body'])
        body.update(traceId=str(uuid.uuid4()),spaceId=info['space_id'],threadId=tid,agentMessageKey='unboxing_m5',
            threadParentPointer={'table':'space','id':info['space_id'],'spaceId':info['space_id']},transcript=[
                dict(id=self.new_id(info['space_id']),type='config',value=config),
                dict(id=self.new_id(info['space_id']),type='context',value=dict(timezone='Europe/Berlin',
                    userName=info.get('user_name',''),userId=info['user_id'],userEmail=info['email'],
                    spaceName=next((s['name'] for s in info['spaces'] if s['id']==info['space_id']),''),
                    spaceId=info['space_id'],spaceViewId=view,currentDatetime=datetime.datetime.now().astimezone().isoformat(),
                    surface='post_onboarding_agent',agentAccessory='pencil')),
                dict(id=self.new_id(info['space_id']),type='title',value='Welcome to Notion'),
                dict(id=self.new_id(info['space_id']),type='user-injected',actualMessage=[[
                    'Start the onboarding conversation now. Send your first onboarding message. The user locale is en-US.']],
                    userId=info['user_id'],createdAt=datetime.datetime.now().astimezone().isoformat())])
        return body

    def transaction(self,client,action,operations):
        return client.call('saveTransactionsFanout',{'requestId':str(uuid.uuid4()),'transactions':[{
            'id':str(uuid.uuid4()),'spaceId':client.info['space_id'],
            'debug':{'userAction':action,'clientCommitTimeMs':int(time.time()*1000)},'operations':operations}]})

    def rename_thread(self,client,tid,title):
        self.transaction(client,'agentThreads.rename',[{'pointer':{'table':'thread','id':tid,'spaceId':client.info['space_id']},
            'path':['data','title'],'command':'set','args':title}])

    def ensure_thread_model(self, client, thread_id, model):
        """Verify persisted legacy config; a request's new config can be ignored."""
        space = client.info['space_id']
        def records(table, ids):
            if not ids:
                return {}
            result = client.call('syncRecordValuesSpaceInitial', {'requests': [
                {'pointer': {'table': table, 'id': ident, 'spaceId': space}, 'version': -1}
                for ident in ids]})
            return {ident: unwrap(value) for ident, value in result.get('recordMap', {}).get(table, {}).items()}
        thread = records('thread', [thread_id]).get(thread_id, {})
        if thread.get('space_id') != space or thread.get('type') != 'workflow' or thread.get('alive') is False:
            raise ValueError('Cannot verify the model of this owned workflow chat')
        messages = records('thread_message', thread.get('messages', []))
        configs = {ident: value['step']['value'] for ident, value in messages.items()
                   if value.get('parent_id') == thread_id and value.get('step', {}).get('type') == 'config'
                   and value['step'].get('value', {}).get('type') == 'workflow'}
        if not configs:
            raise ValueError('Missing saved workflow model configuration; no inference submitted')
        changed = [ident for ident, config in configs.items()
                   if config.get('model') != model or config.get('modelFromUser') is not True
                   or config.get('agentMemorySettings', {}).get('useMemories') is not True]
        if changed:
            self.transaction(client, 'NotionTool.selectExplicitModel', [
                {'pointer': {'table': 'thread_message', 'id': ident, 'spaceId': space},
                 'path': ['step', 'value'], 'command': 'update',
                 'args': {'model': model, 'modelFromUser': True,
                          'agentMemorySettings': {'useMemories': True, 'excludeChatFromMemories': True}}} for ident in changed])
            confirmed = records('thread_message', changed)
            if any(confirmed.get(ident, {}).get('step', {}).get('value', {}).get('model') != model
                   or confirmed.get(ident, {}).get('step', {}).get('value', {}).get('modelFromUser') is not True
                   or confirmed.get(ident, {}).get('step', {}).get('value', {}).get('agentMemorySettings', {}).get('useMemories') is not True
                   for ident in changed):
                raise ValueError('Notion did not confirm the selected explicit model; no inference submitted')
        return {'model': model, 'explicit': True, 'corrected': len(changed)}

    def ensure_team(self,account_id):
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

    def run(self, worker, task, prompt, cancel, update):
        account = self.store.get('accounts',worker['account_id'])
        if not account.get('model'):
            raise ValueError('Opus 5.5 is unavailable; refresh the account')
        client = self.client(account['id'])
        # Fetch head before sending so recovery cannot mistake old responses for this attempt.
        transcript = client.transcript(worker['thread_id'])
        session = transcript.get('session') or {}
        if session.get('status') not in (None,'idle','completed'):
            # Nothing was submitted. Treat an externally busy thread as safe backoff.
            raise RateLimited(10)
        if transcript.get('session_pending'):
            raise RateLimited(10)
        if self.thread_busy(client, worker['thread_id']):
            raise RateLimited(5, 'Notion is still finishing the previous turn in this chat; nothing was sent yet', free=True)
        from .chat_context import ChatContext
        original_thread, original_generation = worker['thread_id'], worker.get('generation', 0)
        if worker.get('id'):
            worker = ChatContext(self.store).rotate(self, client, worker, task, transcript, cancel)
        # A new generation may have run a checkpoint turn. Read the current head before sending.
        if worker['thread_id'] != original_thread or worker.get('generation', 0) != original_generation:
            transcript = client.transcript(worker['thread_id'])
            session = transcript.get('session') or {}
            if transcript.get('session_pending') or session.get('status') not in (None, 'idle', 'completed'):
                raise RateLimited(10)
            if self.thread_busy(client, worker['thread_id']):
                raise RateLimited(5, 'Notion is still finishing the context checkpoint in this chat; the assignment was not sent yet', free=True)
        handoff_included = bool(worker.get('handoff') and not worker.get('handoff_delivered'))
        if handoff_included:
            # Without a server-side reset the earlier history stays visible; mark it as superseded.
            note = '' if worker.get('context_reset') else ('\n\nEarlier messages in this chat are superseded by '
                                                          'this checkpoint; use them only as reference history.')
            prompt = worker['handoff'] + note + '\n\nCurrent assignment (continue from the checkpoint):\n' + prompt
        selected = 'opus' if worker.get('purpose') == 'main' else task.get('model', 'opus')
        model = client.model(selected)
        # The saved chat config changes only through our own confirmed write: check once per chat generation.
        config_key = (worker['thread_id'], worker.get('generation', 0), model)
        if getattr(self, 'confirmed_configs', {}).get(worker['thread_id']) != config_key:
            self.ensure_thread_model(client, worker['thread_id'], model)
            self.__dict__.setdefault('confirmed_configs', {})[worker['thread_id']] = config_key
        with self.store.lock:
            saved = self.store.get('tasks',task['id'])
            saved['before_seq'] = session.get('sequence',0)
            saved['thread_id'] = worker['thread_id']
            saved['submitted_prompt'] = prompt
            saved['handoff_included'] = handoff_included
            saved['step_id'] = self.new_id(client.info['space_id'])
            saved['sent_at'] = time.time()
            saved['request_fingerprint'] = hashlib.sha256(prompt.encode()).hexdigest()
            saved.update(model=selected, assigned_model_id=model)
            saved['submission_phase'] = 'inference'
            self.store.put('tasks',saved)
            task = saved
        if cancel.is_set():
            raise ValueError('Assignment cancelled before submission')
        # The assignment token is only offered when tools are needed (project, files, visual work).
        body = self.body(client,worker['thread_id'],task['step_id'],prompt,model,computer='assignment_token' in prompt)
        if worker.get('context_reset'):
            body['isPartialTranscript'] = False  # replace the context inside the same chat
            with self.store.lock:
                saved = self.store.get('tasks',task['id'])
                saved['context_reset'] = True
                self.store.put('tasks',saved)
                task = saved
        steps = []
        started,last_update = time.monotonic(),0
        trace = dict(task_id=task['id'], worker_id=worker.get('id', ''), attempt=task.get('attempts', 0))
        run_id = task.get('run_id', '')
        self.store.event('remote_sent', run_id, prompt_chars=len(prompt), **trace)
        seen_output, lines, reported = False, 0, []
        def remote_done(outcome, error_class=''):
            if not reported:
                reported.append(outcome)
                self.store.event('remote_done', run_id, outcome=outcome, error_class=error_class, lines=lines,
                                 seconds=round(time.monotonic()-started, 3), **trace)
        try:
            for line in client.transport.stream('runInferenceTranscript',body,client.headers(),timeout=60,cancel=cancel):
                if cancel.is_set() or time.monotonic()-started>self.store.settings()['task_timeout']:
                    raise PendingRemote()
                event = json.loads(line)
                lines += 1
                if event.get('type') in ('patch-start','patch-sync'):
                    steps = (event.get('data') or {}).get('s') or steps
                elif event.get('type')=='patch':
                    steps = apply_patch(steps,event.get('v'))
                if not seen_output and stream_text(steps).strip():
                    seen_output = True
                    self.mark_task(task['id'], first_output_at=time.time())
                    self.store.event('first_output', run_id, seconds=round(time.monotonic()-started, 3), **trace)
                if time.monotonic()-last_update>0.6:
                    update(stream_text(steps))
                    last_update = time.monotonic()
            errors = [s for s in steps if isinstance(s,dict) and s.get('type')=='error']
            text = stream_text(steps)
            from .chat_pool import script_mode_failure
            if script_mode_failure(steps):
                self.note_worker_mcp_failure(worker)
            self.mark_task(task['id'], remote_done_at=time.time())
            remote_done('stream_end')
            # Streaming steps can contain stale text or provider placeholders.
            # Only the committed answer to our own turn can finish an assignment.
            return self.recover(worker,task,cancel)
        except NotionHttpError as exc:
            remote_done('http_'+str(exc.status), http_class(exc.status))
            if exc.status==429:
                raise RateLimited(exc.retry_after) from None
            raise ValueError(f'Notion inference: HTTP {exc.status}') from None
        except (OSError,TimeoutError,json.JSONDecodeError):
            remote_done('unknown', 'REMOTE_UNKNOWN')
            raise PendingRemote() from None
        except PendingRemote:
            remote_done('unconfirmed', 'REMOTE_UNKNOWN')
            raise

    def mark_task(self, task_id, **fields):
        # Persist telemetry timestamps on the task without touching its state.
        with self.store.lock:
            saved = self.store.get('tasks', task_id)
            if saved:
                saved.update(fields)
                self.store.put('tasks', saved)

    def recover(self, worker, task, cancel):
        try:
            with request_priority('verify'):
                transcript = self.client(worker['account_id']).transcript(task.get('thread_id') or worker['thread_id'])
        except Exception:
            raise PendingRemote() from None
        from .chat_context import ChatContext
        observed_worker = dict(worker, thread_id=task.get('thread_id') or worker['thread_id'])
        ChatContext(self.store).observe(observed_worker, transcript)
        entities = [p.get('entity',{}) for p in transcript.get('patches',[]) if p.get('op')=='put']
        fresh = [e for e in entities if e.get('sequence',0)>task.get('before_seq',0)]
        session=transcript.get('session') or {}
        users=[e for e in fresh if e.get('kind')=='user_message']
        markers=[e for e in users if task.get('step_id') and
                 (e.get('id')==task['step_id'] or e.get('client_message_id')==task['step_id'])]
        if not markers:
            markers=[e for e in users if task.get('request_fingerprint') and
                hashlib.sha256(text_content(e.get('content',e.get('text',[]))).encode()).hexdigest()==task['request_fingerprint']]
        if not markers:
            if task.get('context_reset') and not_accepted(task, transcript, fresh):
                raise ResetNotAccepted()
            raise PendingRemote()
        marker=min(markers,key=lambda e:e.get('sequence',0))
        after=marker.get('sequence',0)
        until=min([e.get('sequence',0) for e in fresh if e.get('kind')=='user_message' and e.get('sequence',0)>after],default=float('inf'))
        own_turn=[e for e in fresh if after<e.get('sequence',0)<until]
        completed = any(e.get('kind')=='turn_completed' for e in own_turn) or (session.get('status')=='idle' and not transcript.get('session_pending'))
        text = '\n'.join(text_content(e.get('content',[])) for e in sorted(own_turn,key=lambda e:e.get('sequence',0))
                         if e.get('kind')=='assistant_message')
        if completed and text:
            with request_priority('verify'):
                self.verify_response_model(worker,task)
            if (task.get('handoff_included') or task.get('context_reset')) and worker.get('id'):
                with self.store.lock:
                    fresh_worker = self.store.get('workers', worker['id'])
                    if fresh_worker and fresh_worker['thread_id'] == task.get('thread_id'):
                        fresh_worker['handoff_delivered'] = True
                        fresh_worker['context_reset'] = False
                        self.store.put('workers', fresh_worker)
            tool_activity=any(any(word in str(e.get('kind',e.get('type',''))).lower() for word in ('tool','execution','computer')) or e.get('tool_use_id') for e in own_turn)
            return VerifiedReply(text,tool_activity)
        if completed and not text:
            # Notion may have ended this turn with an error step and no reply (confirmed retry).
            self.confirm_turn_error(worker,task)
        # A timeout is not evidence that the server didn't accept the POST.
        raise PendingRemote()

    def turn_failed(self, task, error):
        """Notion recorded our turn as failed before any output: record it and retry after a cooldown."""
        error = error or {}
        sub_type = str(error.get('subType') or 'error')
        message = ('Notion ended this turn with an error (' + sub_type + '): ' + str(error.get('message') or 'no reply') +
                   ' No reply was produced; a new turn follows after a cooldown.')
        saved = self.store.get('tasks', task['id'], dict(task))
        saved.update(model_verification='turn_error', reported_model_ids=[], remote_error=sub_type)
        self.store.put('tasks', saved)
        self.store.event('remote_turn_failed', task.get('run_id', ''), task_id=task['id'],
                         trace_id=error.get('traceId', ''), sub_type=sub_type, error_class='TRANSIENT', message=message)
        budget = getattr(self, 'network_budget', None)
        if isinstance(budget, NetworkBudget):
            budget.turn_failed(source='inference', sub_type=sub_type)
        raise RateLimited(20, message, confirmed=True)

    def confirm_turn_error(self, worker, task):
        """Best effort for a finished turn without a reply; unreadable records keep it pending."""
        client = self.client(worker['account_id'])
        thread_id = task.get('thread_id') or worker['thread_id']
        def records(table, ids):
            result = client.call('syncRecordValuesSpaceInitial', {'requests': [
                {'pointer': {'table': table, 'id': ident, 'spaceId': client.info['space_id']}, 'version': -1} for ident in ids]})
            return {ident: unwrap(value) for ident, value in result.get('recordMap', {}).get(table, {}).items()}
        try:
            thread = records('thread', [thread_id]).get(thread_id) or {}
            steps, _ = own_turn_steps(records, thread.get('messages', []), task.get('step_id'))
            state, error = turn_status(steps, task.get('step_id'), list(((thread.get('data') or {}).get('turn_outcomes') or {}).values()))
        except Exception:
            return
        if state == 'error':
            self.turn_failed(task, error)

    def thread_busy(self, client, thread_id):
        """True while Notion has not written the outcome of the latest turn in this chat."""
        try:
            result = client.call('syncRecordValuesSpaceInitial', {'requests': [
                {'pointer': {'table': 'thread', 'id': thread_id, 'spaceId': client.info['space_id']}, 'version': -1}]})
            found = result.get('recordMap', {}).get('thread', {})
            thread = unwrap(found[thread_id]) if thread_id in found else {}
            latest = float(thread.get('latest_user_or_trigger_time') or 0)
            done = float(((thread.get('data') or {}).get('last_turn_outcome') or {}).get('completed_time') or 0)
        except Exception:
            return False  # The session checks still apply when the record cannot be read.
        # A turn without an outcome for longer than the task timeout counts as abandoned.
        return latest > done and time.time() * 1000 - latest < self.store.settings()['task_timeout'] * 1000

    def verify_response_model(self,worker,task):
        # Check the committed own turn, framed by verify_started / verify_done events.
        trace=dict(task_id=task['id'],worker_id=worker.get('id',''),attempt=task.get('attempts',0))
        began=time.monotonic();outcome='error'
        self.store.event('verify_started',task.get('run_id',''),**trace)
        try:
            result=self.check_response_model(worker,task)
            outcome=(self.store.get('tasks',task['id']) or {}).get('model_verification') or 'verified'
            return result
        except PendingRemote:
            outcome='pending';raise
        except RateLimited:
            outcome='turn_error';raise
        except ModelVerificationError:
            outcome='unverified';raise
        finally:
            read=(self.store.get('tasks',task['id']) or {}).get('verify_messages_read',0)
            self.store.event('verify_done',task.get('run_id',''),outcome=outcome,seconds=round(time.monotonic()-began,3),
                             messages_read=read,**trace)

    def check_response_model(self,worker,task):
        client=self.client(worker['account_id'])
        def records(table,ids):
            result=client.call('syncRecordValuesSpaceInitial',{'requests':[
                {'pointer':{'table':table,'id':ident,'spaceId':client.info['space_id']},'version':-1} for ident in ids]})
            return {ident:unwrap(value) for ident,value in result.get('recordMap',{}).get(table,{}).items()}
        steps=None;thread={}
        try:
            thread=records('thread',[worker['thread_id']]).get(worker['thread_id'],{})
            if thread.get('space_id')!=client.info['space_id'] or thread.get('alive') is False:
                raise ModelVerificationError(task.get('assigned_model_id'),[],'missing')
            steps,read=own_turn_steps(records,thread.get('messages',[]),task.get('step_id'))
            self.mark_task(task['id'],verify_messages_read=read)
            reported=verify_execution(steps,task.get('step_id'),task.get('assigned_model_id'))
        except ModelVerificationError as exc:
            saved=self.store.get('tasks',task['id'],dict(task))
            if exc.reason=='mismatch' and exc.reported:
                # Notion ran a different model for this plan: continue and show it.
                saved.update(model_verification='fallback',reported_model_ids=exc.reported)
                self.store.put('tasks',saved)
                note=('Requested model '+str(exc.expected)+' is not used by Notion for this account; '
                      'chats run on '+', '.join(exc.reported)+'.')
                with self.store.lock:
                    account=self.store.get('accounts',worker.get('account_id'))
                    if account and account.get('model_fallback')!=note:
                        account['model_fallback']=note
                        self.store.put('accounts',account)
                self.store.event('model_fallback',task.get('run_id',''),task_id=task['id'],
                                 expected_model_id=exc.expected,reported_model_ids=exc.reported,message=note)
                return exc.reported
            if exc.reason=='missing' and steps is not None:
                state,error=turn_status(steps,task.get('step_id'),list(((thread.get('data') or {}).get('turn_outcomes') or {}).values()))
                if state=='error':
                    self.turn_failed(task,error)
                if state in ('running','missing'):
                    # Notion has not written the outcome of this turn yet: check again later, never resend.
                    raise PendingRemote() from None
            saved.update(model_verification='unverified',reported_model_ids=exc.reported)
            self.store.put('tasks',saved)
            raise
        except (OSError,TimeoutError,ValueError):
            raise PendingRemote() from None
        saved=self.store.get('tasks',task['id'],dict(task))
        saved.update(model_verification='verified',reported_model_ids=reported)
        self.store.put('tasks',saved)
        return reported

    def connect_bridge(self, account_id, url, token, name='NotionTool', read_auto=True, write_auto=True, registry_id=''):
        if not url.startswith('https://'):
            raise ValueError('Notion requires a reachable public HTTPS MCP URL')
        client = self.client(account_id)
        client.identity()
        from .mcp_policy import ensure_mcp_allowed
        ensure_mcp_allowed(self,client,url)
        view=next((unwrap(v) for v in client.info.get('records',{}).get('space_view',{}).values()
                   if unwrap(v).get('space_id')==client.info['space_id']),None)
        if not view or not view.get('id'):raise ValueError('Notion workspace view is unavailable for MCP connection')
        modules=list((view.get('settings') or {}).get('agent_chat_modules') or [])
        ids=[m['pointer']['id'] for m in modules if (m.get('pointer') or {}).get('table')=='workflow_module']
        records={}
        if ids:
            synced=client.call('syncRecordValuesSpaceInitial',{'requests':[{'pointer':{'table':'workflow_module','id':ident,'spaceId':client.info['space_id']},'version':-1} for ident in ids]})
            records={ident:unwrap(value) for ident,value in synced.get('recordMap',{}).get('workflow_module',{}).items()}
        integration=next((ident for ident,value in records.items() if value.get('alive',True) and
                          ((value.get('data') or {}).get('serverUrl') or value.get('server_url'))==url),None)
        auth = [{'name':'Authorization','value':'Bearer '+token}] if token else []
        validation = client.call('validateMcpConnection',dict(serverUrl=url,spaceId=client.info['space_id'],
            authHeaders=auth,initiationContext='connect'),timeout=90)
        if not validation.get('success'):
            raise ValueError('Notion cannot reach this MCP server')
        if not integration:
            integration=self.new_id(client.info['space_id'],'00b6')
            result = client.call('postWorkflowsMcpServerConnect',dict(integrationId=integration,spaceId=client.info['space_id'],
                authHeaders=auth,moduleDefinition={'name':name,'serverUrl':url,
                'runReadToolsAutomatically':read_auto,'runWriteToolsAutomatically':write_auto},initiationContext='connect'),timeout=90)
            if not result.get('success'):
                raise ValueError('Notion refused the MCP connection')
            modules.append({'pointer':{'table':'workflow_module','id':integration,'spaceId':client.info['space_id']},'defaultEnabled':True})
        # Idempotent: an existing, enabled module with the right permissions is left untouched, so a
        # reconnect never rewrites the account's chat modules while Coworkers are working.
        changed = integration not in records
        for module in modules:
            if (module.get('pointer') or {}).get('id')==integration and module.get('defaultEnabled') is not True:
                module['defaultEnabled']=True;changed=True
        if changed:
            self.transaction(client,'agentPersistenceHelpers.addAgentChatModule',[
                {'pointer':{'table':'space_view','id':view['id'],'spaceId':client.info['space_id']},'path':['settings'],
                 'command':'update','args':{'agent_chat_modules':modules}}])
        current=(records.get(integration) or {}).get('data') or {}
        if integration not in records or current.get('runReadToolsAutomatically')!=read_auto or current.get('runWriteToolsAutomatically')!=write_auto:
            self.transaction(client,'mcpServerSettings.updateToolPermissions',[
                {'pointer':{'table':'workflow_module','id':integration,'spaceId':client.info['space_id']},
                 'path':['data',key],'command':'set','args':value} for key,value in
                 (('runReadToolsAutomatically',read_auto),('runWriteToolsAutomatically',write_auto))])
        account = self.store.get('accounts',account_id)
        if registry_id:
            connections=account.setdefault('mcp_connections',{})
            connections[registry_id]={'connected':True,'integration_id':integration}
        else:
            account.update(bridge_connected=True,bridge_url=url,bridge_integration=integration,bridge_error='',bridge_blocked=False,bridge_next_at=0)
        self.store.put('accounts',account)
        return {'connected':True}
