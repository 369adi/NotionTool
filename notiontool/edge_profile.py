"""Owned Edge profile that opens the signed-in Notion account in a normal browser window."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
import websocket


def browser_cookies(session):
    result=[]
    for cookie in session.get('cookies',[]):
        if cookie.get('name') in ('__cf_bm','_cfuvid'):continue  # Browser obtains its own normal service checks.
        domain=str(cookie.get('domain',''))
        if domain.lstrip('.').lower() not in ('notion.com','app.notion.com','notion.so','www.notion.so'):
            continue
        if not cookie.get('name') or not cookie.get('value'):continue
        expiry=cookie.get('expires') or time.time()+600
        if expiry>0 and expiry<time.time():continue
        client_readable=cookie['name'] in ('notion_users','notion_user_id','notion_browser_id',
            'notion_locale','NEXT_LOCALE','notion_cookie_consent','notion_check_cookie_consent')
        imported=dict(name=cookie['name'],value=cookie['value'],domain=domain,
            path=cookie.get('path') or '/',secure=True,httpOnly=cookie.get('httpOnly',not client_readable),
            expires=min(expiry if expiry>0 else time.time()+600,time.time()+600))
        if cookie['name'].startswith('__Host-'):
            imported.pop('domain');imported['url']='https://'+domain.lstrip('.')+'/'
            imported['path']='/'
        result.append(imported)
    if not any(c['name']=='token_v2' for c in result):
        raise ValueError('An authenticated native Notion session is required.')
    return result


class EdgeProfile:
    def __init__(self,directory,session):
        if os.name!='nt':raise ValueError('Opening the account in Edge requires Windows.')
        self.base=(Path(directory)/'account_browsers').resolve();self.base.mkdir(exist_ok=True)
        self.profile=(self.base/uuid.uuid4().hex).resolve()
        if not self.profile.is_relative_to(self.base):raise ValueError('Invalid owned browser profile path.')
        self.profile.mkdir();self.session=session;self.process=None
        self.edge=next((Path(p) for p in (
            r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
            r'C:\Program Files\Microsoft\Edge\Application\msedge.exe') if Path(p).is_file()),None)
        if not self.edge:raise ValueError('Microsoft Edge is required to open the account.')
    def __enter__(self):return self
    def __exit__(self,*_):
        if self.process and self.process.poll() is None:self.process.terminate()
        # Stop only Edge processes that use this owned profile directory.
        needle=str(self.profile).replace("'","''")
        subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
            "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | Where-Object { $_.CommandLine -like '*"+needle+"*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }"],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW)
        target=self.profile.resolve()
        if target.is_relative_to(self.base) and target!=self.base:
            shutil.rmtree(target,ignore_errors=True)
    @staticmethod
    def hidden():
        startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=0
        return startup
    def prepare(self):
        cookies=browser_cookies(self.session)
        # A fresh private profile is exposed on a temporary loopback debug socket only to
        # import the existing session; it is closed before the visible window starts.
        proc=subprocess.Popen([str(self.edge),'--user-data-dir='+str(self.profile),
            '--remote-debugging-port=0','--remote-debugging-address=127.0.0.1',
            '--no-first-run','--no-default-browser-check','--window-position=-32000,-32000',
            'about:blank'],startupinfo=self.hidden(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.process=proc
        port_file=self.profile/'DevToolsActivePort';deadline=time.monotonic()+20
        while not port_file.exists() and time.monotonic()<deadline:time.sleep(.2)
        if not port_file.exists():raise ValueError('The owned browser session could not initialize.')
        lines=port_file.read_text().splitlines();port=int(lines[0]);path=lines[1]
        if not 1<=port<=65535 or not path.startswith('/devtools/browser/'):
            raise ValueError('Invalid owned browser endpoint.')
        ws=websocket.create_connection(f'ws://127.0.0.1:{port}{path}',timeout=10,suppress_origin=True)
        try:
            ws.send(json.dumps({'id':1,'method':'Storage.setCookies','params':{'cookies':cookies}}))
            while True:
                reply=json.loads(ws.recv())
                if reply.get('id')==1:break
            if reply.get('error'):raise ValueError('The session could not be imported into the owned profile.')
            ws.send(json.dumps({'id':2,'method':'Browser.close'}))
        finally:ws.close()
        try:proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            raise ValueError('The temporary session-import browser did not close.')
        self.process=None
