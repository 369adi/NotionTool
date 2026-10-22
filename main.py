"""Windows desktop entry point. --serve is useful for local UI verification."""
from pathlib import Path
import argparse
import sys
import time
import threading
import hashlib
import os
from notiontool.server import Application
from notiontool.startup import default_data_dir


def instance_lock(directory):
    if os.name!='nt':return None
    import ctypes
    library=ctypes.WinDLL('kernel32',use_last_error=True)
    library.CreateMutexW.restype=ctypes.c_void_p
    name='Local\\NotionTool-'+hashlib.sha256(str(Path(directory).resolve()).casefold().encode()).hexdigest()[:24]
    handle=library.CreateMutexW(None,False,name)
    if ctypes.get_last_error()==183:
        # Second start (e.g. the Documents shortcut): bring the running window to the front instead of an error box.
        user32=ctypes.windll.user32
        window=user32.FindWindowW(None,'NotionTool')
        if window:
            user32.ShowWindow(window,9)  # SW_RESTORE
            user32.SetForegroundWindow(window)
        elif not reopen_desktop_window(directory):
            user32.MessageBoxW(None,'NotionTool is already running. Use the open NotionTool window.','NotionTool',0x40)
        return False
    return handle


def reopen_desktop_window(data_dir):
    """Harness runs headless but its Notion window was closed (tray quit): open the window again.

    Same argv as DesktopApp, so Electron focuses an already open window instead of a second one;
    the running harness attaches to it again (DesktopController._await_reopen)."""
    if not new_app_is_ui(data_dir):
        return False
    try:
        import subprocess
        from notiontool.desktop_app import DesktopApp
        from notiontool.desktop_shell import default_profile, find_exe
        subprocess.Popen(DesktopApp(find_exe(), default_profile()).argv(), close_fds=True)
        return True
    except Exception:
        return False


def new_app_is_ui(data_dir):
    """True when the NotionTool desktop app opens on start (data/desktop_app.json app_autostart)."""
    try:
        from notiontool.desktop_shell import load_app_settings
        return bool(load_app_settings(data_dir).get('app_autostart'))
    except Exception:
        return False


def main():
    if '--gateway' in sys.argv:  # process 2: MCP gateway / backup MCP, no window, no instance lock
        from notiontool.gateway import main as gateway_main
        sys.exit(gateway_main(sys.argv))
    if '--protocol-forward' in sys.argv:  # notion:// link from Windows: route it, no window, no instance lock
        from notiontool.protocol_forward import main as forward_main
        sys.exit(forward_main(sys.argv))
    from notiontool.updater import handoff
    sys.argv = handoff(sys.argv)  # waits for the previous instance; installs a downloaded update
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--headless',action='store_true',help='No classic window; the NotionTool desktop app is the UI')
    parser.add_argument('--classic',action='store_true',help='Force the classic window')
    parser.add_argument('--data-dir',default=default_data_dir(sys.executable,Path(__file__).parent,getattr(sys,'frozen',False)))
    parser.add_argument('--port',type=int,default=None,help='Host API port (default: setting host_api_port, 8790)')
    args = parser.parse_args()
    instance=instance_lock(args.data_dir)
    if instance is False:return
    app = Application(args.data_dir)
    app.exit_event = threading.Event()  # set when the user closes the NotionTool window (desktop_shell)
    url = app.launch(args.port)
    try:
        if args.serve or (not args.classic and (args.headless or new_app_is_ui(args.data_dir))):
            # The NotionTool desktop app (separate Notion instance) is the UI: no classic window.
            if args.serve:
                print(url,flush=True)
            while not app.exit_event.wait(1):
                pass
        else:
            import webview
            app.window = webview.create_window('NotionTool',url,width=1480,height=940,min_size=(1050,700),background_color='#161719')
            webview.start(gui='edgechromium')
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        if app.exit_event.is_set():
            stop_gateway(app)


def stop_gateway(app):
    """Full quit (window closed): also stop process 2, the MCP gateway; restarts keep it running."""
    try:
        import urllib.request
        from notiontool import gateway as gw
        port = int(app.store.settings().get('mcp_gateway_port') or gw.DEFAULT_PORT)
        control = gw.read_json(gw.runtime_dir(app.data_dir) / 'gateway.json').get('control', '')
        request = urllib.request.Request(f'http://127.0.0.1:{port}/__gateway/stop', data=b'{}', method='POST',
                                         headers={'X-Gateway-Control': control})
        urllib.request.urlopen(request, timeout=5).read()
    except Exception:
        pass


if __name__=='__main__':
    main()
