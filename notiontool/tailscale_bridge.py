"""Dedicated authenticated MCP listener through Tailscale Funnel."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit


class TailscaleBridge:
    def __init__(self, store, runner=subprocess.run):
        self.store,self.runner=store,runner
        self.executable=shutil.which('tailscale') or r'C:\Program Files\Tailscale\tailscale.exe'
        self.owned_port=0
        self.target=''

    def command(self,*args):
        result=self.runner([self.executable,*args],capture_output=True,text=True,timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode:
            raise ValueError('Tailscale command failed; check that Tailscale is signed in and Funnel is enabled.')
        return result.stdout

    def start(self,local_port):
        if not Path(self.executable).is_file():raise ValueError('Install and sign in to Tailscale first.')
        status=json.loads(self.command('status','--json'))
        if status.get('BackendState')!='Running':raise ValueError('Sign in to Tailscale before starting the MCP bridge.')
        host=(status.get('Self') or {}).get('DNSName','').rstrip('.')
        if not re.fullmatch(r'[a-zA-Z0-9.-]+\.ts\.net',host):raise ValueError('Tailscale HTTPS hostname is unavailable.')
        config=json.loads(self.command('funnel','status','--json') or '{}')
        settings=self.store.settings();target=f'http://127.0.0.1:{local_port}'
        previous=settings.get('tailscale_port',0)
        candidates=[previous]+[p for p in (10000,8443,443) if p!=previous]
        port=0; mount=''
        saved=settings.get('public_mcp_url','')
        if saved:
            parsed=urlsplit(saved)
            if parsed.scheme!='https' or parsed.hostname!=host or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('/mcp','/nt-harness/mcp'):
                raise ValueError('The saved public MCP URL does not match this Tailscale device; retaining it instead of silently changing client settings.')
            port=parsed.port or 443
            if port not in (443,8443,10000):raise ValueError('Saved MCP URL uses an unsupported Funnel port.')
            mount=parsed.path.removesuffix('/mcp')
            handlers=(config.get('Web',{}).get(f'{host}:{port}',{}) or {}).get('Handlers',{})
            tcp=config.get('TCP',{}).get(str(port),{})
            if tcp and not tcp.get('HTTPS'):raise ValueError('The saved Funnel port belongs to a non-HTTPS service; not changing it.')
            current=(handlers.get(mount or '/') or {}).get('Proxy')
            owned_previous=settings.get('tailscale_backend')
            if current not in (None,target) and not (mount=='/nt-harness' and current==owned_previous):
                raise ValueError('The saved MCP route is occupied by another service; retaining the URL and leaving other services unchanged.')
        for candidate in ([] if port else candidates):
            if candidate not in (443,8443,10000):continue
            listener=f'{host}:{candidate}'
            existing=(config.get('Web',{}).get(listener,{}) or {}).get('Handlers',{})
            occupied=str(candidate) in config.get('TCP',{}) or bool(existing)
            ours=existing.get('/',{}).get('Proxy')==target and candidate==previous  # only our live target; a copied setting is no proof of ownership
            if not occupied or (ours and set(existing)=={'/'}):
                port=candidate;break
        if not port:
            # Every Funnel port serves another app at '/': share one under our own path and never touch their routes.
            mount='/nt-harness'
            for candidate in candidates:
                if candidate not in (443,8443,10000):continue
                if str(candidate) in config.get('TCP',{}) and not config['TCP'][str(candidate)].get('HTTPS'):continue  # raw TCP forward of another app
                handlers=(config.get('Web',{}).get(f'{host}:{candidate}',{}) or {}).get('Handlers',{})
                current=(handlers.get(mount) or {}).get('Proxy')
                if current in (None,target) or settings.get('tailscale_mount')==mount:  # the mount path is only ever created by this app
                    port=candidate;break
            if not port:raise ValueError('All Funnel ports are occupied by other services; their configuration was retained.')
        # Re-applying an identical Funnel route every maintenance minute rewrites the serve config;
        # only a missing or changed route is applied.
        listener=f'{host}:{port}'
        handlers=(config.get('Web',{}).get(listener,{}) or {}).get('Handlers',{})
        live=((handlers.get(mount or '/') or {}).get('Proxy')==target
              and bool((config.get('AllowFunnel') or {}).get(listener))
              and bool((config.get('TCP',{}).get(str(port)) or {}).get('HTTPS')))
        if not live:
            self.command('funnel','--bg',f'--https={port}',*([f'--set-path={mount}'] if mount else []),target)
        settings.update(tailscale_port=port,tailscale_backend=target,
                        public_mcp_url=f'https://{host}'+(f':{port}' if port!=443 else '')+mount+'/mcp',tailscale_mount=mount)
        self.store.put('settings',settings)
        self.owned_port,self.target,self.mount=port,target,mount
        return {'url':settings['public_mcp_url'],'provider':'tailscale'}

    def stop(self):
        if not self.owned_port:return
        if getattr(self,'mount',''):
            try:self.command('funnel',f'--https={self.owned_port}',f'--set-path={self.mount}','off')
            except ValueError:pass
            self.owned_port=0;return
        # Never reset shared Tailscale state or remove another app's route.
        config=json.loads(self.command('funnel','status','--json') or '{}')
        for listener,web in config.get('Web',{}).items():
            handlers=web.get('Handlers',{})
            if listener.endswith(':'+str(self.owned_port)) and set(handlers)=={'/'} and handlers['/'].get('Proxy')==self.target:
                self.command('funnel',f'--https={self.owned_port}','off')
                break
        self.owned_port=0
