"""Tab bar skin for the NotionTool desktop window (v6 redesign, mockup 010922).

The Notion desktop window draws its top bar (App Menu, sidebar toggle, Inbox, New, Back/Forward,
tabs) in a separate DevTools page target titled 'Tab Bar' (a file:// page of the frozen runtime).
The v6 mockup has no Notion bar: this paints the bar in the app background and hides Notion's
buttons. The native window controls (minimise/maximise/close) are drawn by Electron and stay.

Safety: only a 'Tab Bar' file:// page on our own 127.0.0.1 DevTools port is touched; nothing here
ever raises into the caller (apply() returns a short status dict); no clicks, no reloads.
"""
import json
import threading
import time
from urllib.parse import urlsplit

HOST = '127.0.0.1'
TITLE = 'Tab Bar'
STYLE_ID = 'nt-tabbar-skin'
BG = '#04060c'
CSS = (
    'html,body{background:' + BG + ' !important}'
    'body *{background-color:transparent !important;border-color:transparent !important;'
    'box-shadow:none !important}'
    '[role="button"],[role="tab"]{opacity:0 !important;pointer-events:none !important}'
)
RETRIES = 15
RETRY_S = 2.0


def skin_source(css=CSS):
    """JS that puts (or refreshes) one <style id=nt-tabbar-skin>; safe to run any number of times."""
    return ('(function(){var id=%s,css=%s;function put(){var s=document.getElementById(id);'
            'if(!s){s=document.createElement("style");s.id=id;'
            '(document.head||document.documentElement).appendChild(s);}'
            'if(s.textContent!==css)s.textContent=css;return true;}'
            'if(document.documentElement)put();else document.addEventListener("DOMContentLoaded",put);'
            'return document.title;})()') % (json.dumps(STYLE_ID), json.dumps(css))


def is_tab_bar(target, port):
    if not isinstance(target, dict) or target.get('type') != 'page' or target.get('title') != TITLE:
        return False
    if urlsplit(str(target.get('url') or '')).scheme != 'file':
        return False
    ws = urlsplit(str(target.get('webSocketDebuggerUrl') or ''))
    return ws.scheme == 'ws' and ws.hostname == HOST and ws.port == int(port)


def apply(http_get, connect, port, holder=None):
    """Skin the tab bar once. Keeps the session in holder['session'] so the new-document script
    stays registered (CDP drops it when its session closes). Returns {ok, target_id|reason}."""
    try:
        targets = http_get('http://%s:%d/json/list' % (HOST, int(port)))
        target = next((t for t in (targets if isinstance(targets, list) else []) if is_tab_bar(t, port)), None)
        if target is None:
            return dict(ok=False, reason='no tab bar target')
        source = skin_source()
        session = connect(target['webSocketDebuggerUrl'])
        session.send('Page.enable')
        session.send('Page.addScriptToEvaluateOnNewDocument', {'source': source})
        session.send('Runtime.evaluate', {'expression': source})
        if holder is not None:
            old = holder.get('session')
            holder['session'] = session
            if old is not None and old is not session:
                try:
                    old.close()
                except Exception:  # noqa: BLE001 - an old socket that is already gone
                    pass
        return dict(ok=True, target_id=str(target.get('id') or ''))
    except Exception as exc:  # noqa: BLE001 - the skin is cosmetic; it never breaks the shell
        return dict(ok=False, reason=str(exc)[:200])


def start(app, *, retries=RETRIES, interval=RETRY_S, sleep=time.sleep, thread=True):
    """Apply the skin for a DesktopApp, retrying while the tab bar target is not there yet.
    Runs in a daemon thread unless thread=False (tests). Returns the holder dict
    ({'result': ..., 'session': ...})."""
    holder = {'result': None, 'session': None}
    http_get, connect, port = getattr(app, '_http_get', None), getattr(app, '_connect', None), getattr(app, 'port', None)
    if not callable(http_get) or not callable(connect) or not isinstance(port, int):
        holder['result'] = dict(ok=False, reason='no desktop app')
        return holder

    def run():
        for attempt in range(max(1, int(retries))):
            result = apply(http_get, connect, port, holder)
            holder['result'] = dict(result, attempt=attempt + 1)
            if result.get('ok'):
                return
            sleep(interval)

    if thread:
        threading.Thread(target=run, name='nt-tabbar-skin', daemon=True).start()
    else:
        run()
    return holder
