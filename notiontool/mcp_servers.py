"""User-added HTTPS MCP endpoints; bearer credentials remain DPAPI encrypted."""
from urllib.parse import urlsplit
from .store import uid
import time


class McpServers:
    def __init__(self, adapter):
        self.adapter=adapter
        self.store=adapter.store

    def public(self):
        return [{'id':'builtin_rea','name':'REA (Reverse Engineer Anything)','url':'Local stdio: npx rea-agents@6.5.0 mcp',
                 'builtin':True,'installed':self._node(),'enabled':bool(self.store.settings().get('rea_enabled',False)),
                 'toggle':'rea_enabled','read_auto':True,'write_auto':False}]+self.store.all('mcp_servers')

    @staticmethod
    def _node():
        from .rea import node_available
        return node_available()

    def add(self, body):
        name=str(body.get('name','')).strip()[:100]
        url=str(body.get('url','')).strip()
        parsed=urlsplit(url)
        if not name or parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError('Use a name and HTTPS MCP URL without embedded credentials; enter a bearer token separately')
        existing=next((s for s in self.store.all('mcp_servers') if s['url']==url),None)
        ident=existing['id'] if existing else uid('mcp')
        token=str(body.get('token',''))
        if token or not existing:
            self.adapter.sessions.save('mcp_server_'+ident,{'token':token})
        entry={'id':ident,'name':name,'url':url,'enabled':True,
               'read_auto':True,'write_auto':str(body.get('write_auto',False)).lower() in ('true','1','on','yes')}
        return self.store.put('mcp_servers',entry)

    def connect(self, account_id=''):
        connected=0;errors=[]
        if getattr(self.adapter, 'single_account_harness', False):
            from .harness_account import selected_account
            selected = selected_account(self.store)
            if account_id and account_id != selected:
                raise ValueError('Connect tools only to the selected harness account')
            if not selected:
                return {'connected':0, 'errors':[]}
            account_id = selected
        for account in self.store.all('accounts'):
            if account.get('state')!='ready' or account_id and account['id']!=account_id:
                continue
            for server in self.store.all('mcp_servers'):
                if not server.get('enabled'):
                    continue
                if account.get('mcp_connections',{}).get(server['id'],{}).get('connected'):
                    continue
                if account.get('mcp_connections',{}).get(server['id'],{}).get('needs_review'):
                    continue
                secret=self.adapter.sessions.load('mcp_server_'+server['id']) or {}
                try:
                    self.adapter.connect_bridge(account['id'],server['url'],secret.get('token',''),
                        name=server['name'],read_auto=server['read_auto'],write_auto=server['write_auto'],registry_id=server['id'])
                    connected+=1
                except Exception:
                    fresh=self.store.get('accounts',account['id'])
                    fresh.setdefault('mcp_connections',{})[server['id']]={'connected':False,'needs_review':True}
                    self.store.put('accounts',fresh)
                    errors.append({'slot':account['slot'],'server_id':server['id'],
                                   'error':'Notion could not connect this server; verify its URL, authentication and workspace permissions'})
        return {'connected':connected,'errors':errors}
