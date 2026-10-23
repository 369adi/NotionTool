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


def self_test_imports(report_path=None, importer=None, package='notiontool'):
    """Import every module of the package (--self-test-imports). Returns 0, or 1 when a module fails.

    The report (count and one 'FAILED <module>: <error>' line per failure) goes to stdout and, when given, to
    report_path; the EXE has no console, so the release workflow reads the file. importer is for tests."""
    import importlib
    import pkgutil
    import traceback
    importer = importer or importlib.import_module
    names = [package]
    failed = []

    def on_error(name):
        failed.append((name, traceback.format_exc(limit=3).strip().splitlines()[-1]))

    try:
        root = importer(package)
        for info in pkgutil.walk_packages(root.__path__, package + '.', onerror=on_error):
            names.append(info.name)
    except BaseException as exc:
        failed.append((package, f'{type(exc).__name__}: {exc}'))
    for name in names:
        try:
            importer(name)
        except BaseException as exc:  # SystemExit from a module counts as a failure too
            failed.append((name, f'{type(exc).__name__}: {exc}'))
    if len(names) < 2 and not failed:
        failed.append((package, 'no submodules found'))
    lines = [f'imported {len(names) - len(failed)} of {len(names)} modules'] + [f'FAILED {n}: {e}' for n, e in failed]
    text = '\n'.join(lines)
    if report_path:
        Path(report_path).write_text(text + '\n', encoding='utf-8')
    if sys.stdout:
        print(text, flush=True)
    return 1 if failed else 0


def main():
    if '--self-test-imports' in sys.argv:  # hidden: release check on the built EXE, no window, no data dir, no lock
        position = sys.argv.index('--self-test-imports') + 1
        report = sys.argv[position] if position < len(sys.argv) and not sys.argv[position].startswith('-') else None
        sys.exit(self_test_imports(report))
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
