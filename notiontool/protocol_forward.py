"""Route notion:// deep links to the NotionTool desktop window while it waits for a sign-in.

Root cause (2026-10-21): the NotionTool window is a second Notion.exe with its own
--user-data-dir. 'Continue in browser' signs in on the web and returns with a notion:// link.
Windows starts the registered handler `Notion.exe "%1"` WITHOUT our --user-data-dir, so the
link reaches the user's normal Notion (other single-instance lock) and our window never signs in.

Fix: NotionTool registers itself as the notion:// handler (HKCU only) and forwards each link:
- NotionTool window running and still on a login/handoff page -> Notion.exe with OUR
  --user-data-dir (Electron hands the link to the running NotionTool instance).
- otherwise -> the original handler command, so the user's normal Notion keeps working.
The original command is saved once under HKCU\\Software\\NotionTool\\ProtocolForward and
restore() puts it back. Registry access, spawn and DevTools reads are injectable for tests.
"""
import json
import os
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

CLASS_KEY = r'Software\Classes\notion'
COMMAND_KEY = CLASS_KEY + r'\shell\open\command'
SAVE_KEY = r'Software\NotionTool\ProtocolForward'
FLAG = '--protocol-forward'
PORT = 9333
LOGIN_PATHS = ('/login', '/logout', '/signup', '/desktopwithbrowserlogin', '/onboarding', '/blank')


# ---- registry (HKCU only) ----------------------------------------------------
class Registry:
    def get(self, path, name=''):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                return str(winreg.QueryValueEx(key, name)[0])
        except OSError:
            return None

    def set(self, path, value, name=''):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def own_command(executable=None, script=None):
    """Handler command that runs this NotionTool build with --protocol-forward."""
    executable = executable or sys.executable
    if getattr(sys, 'frozen', False) or script is None:
        return '"%s" %s "%%1"' % (executable, FLAG)
    pythonw = Path(executable).with_name('pythonw.exe')
    runner = str(pythonw) if pythonw.exists() else executable
    return '"%s" "%s" %s "%%1"' % (runner, script, FLAG)


def is_ours(command):
    return bool(command) and FLAG in command


def install(command=None, registry=None):
    """Make NotionTool the notion:// handler. Idempotent; returns what changed."""
    registry = registry or Registry()
    command = command or own_command(script=_main_script())
    current = registry.get(COMMAND_KEY)
    if current == command:
        return dict(changed=False, command=command)
    if current and not is_ours(current):
        registry.set(SAVE_KEY, current, 'OriginalCommand')
    if not registry.get(SAVE_KEY, 'OriginalCommand'):
        raise RuntimeError('No original notion:// handler found; nothing was changed')
    registry.set(CLASS_KEY, 'URL:notion')
    registry.set(CLASS_KEY, '', 'URL Protocol')
    registry.set(COMMAND_KEY, command)
    return dict(changed=True, command=command, previous=current)


def restore(registry=None):
    registry = registry or Registry()
    original = registry.get(SAVE_KEY, 'OriginalCommand')
    if original and is_ours(registry.get(COMMAND_KEY)):
        registry.set(COMMAND_KEY, original)
        return dict(restored=True, command=original)
    return dict(restored=False)


def _main_script():
    script = Path(__file__).resolve().parent.parent / 'main.py'
    return str(script) if script.exists() else None


def guard(stop_event, interval=1.0, installer=None, on_change=None, on_error=None):
    """Keep NotionTool as notion:// handler until stop_event is set.

    Every Notion.exe start (also our own window) re-registers itself as handler, so a one-shot
    install is undone within seconds. A registry read per interval is cheap."""
    installer = installer or install
    reported = False
    while not stop_event.is_set():
        try:
            result = installer()
            reported = False
            if result.get('changed') and on_change:
                on_change(result)
        except Exception as exc:  # noqa: BLE001
            if not reported and on_error:
                on_error(exc)
            reported = True
        stop_event.wait(interval)


def start_guard(stop_event, **kwargs):
    import threading
    thread = threading.Thread(target=guard, args=(stop_event,), kwargs=kwargs, daemon=True,
                              name='notion-protocol-guard')
    thread.start()
    return thread


# ---- forwarding ----------------------------------------------------------------
def split_command(command, url):
    """argv of a registry handler command with %1 replaced by the URL."""
    parts = shlex.split(command, posix=False)
    argv = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]
    if any('%1' in a for a in argv):
        return [a.replace('%1', url) for a in argv]
    return argv + [url]


def _http_json(url):
    if urlsplit(url).hostname != '127.0.0.1':
        raise ValueError('DevTools only on 127.0.0.1')
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.loads(response.read().decode('utf-8'))
    except (OSError, ValueError):
        return None


def app_waits_for_login(http_get=_http_json, port=PORT):
    """True when the NotionTool window is up and shows no signed-in workspace page."""
    from .desktop_app import is_notion_page
    targets = http_get('http://127.0.0.1:%d/json/list' % port)
    if not isinstance(targets, list):
        return False  # NotionTool window not running
    pages = [t for t in targets if isinstance(t, dict) and t.get('type') == 'page']
    return not any(is_notion_page(t) for t in pages)


def decide(url, registry=None, http_get=_http_json, profile_dir=None, port=PORT):
    """Return (target, argv) for one notion:// link."""
    registry = registry or Registry()
    original = registry.get(SAVE_KEY, 'OriginalCommand') or ''
    if not original or is_ours(original):
        raise RuntimeError('Original notion:// handler unknown')
    if app_waits_for_login(http_get, port):
        from .desktop_shell import default_profile
        exe = split_command(original, url)[0]
        profile = profile_dir or default_profile()
        return 'notiontool', [exe, '--user-data-dir=' + profile, '--remote-debugging-port=' + str(port), url]
    return 'normal', split_command(original, url)


def _log(data):
    try:
        base = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'NotionTool-App'
        base.mkdir(parents=True, exist_ok=True)
        with open(base / 'protocol-forward.log', 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(data) + '\n')
    except OSError:
        pass


def main(argv, spawn=None, registry=None, http_get=_http_json):
    import time
    url = argv[argv.index(FLAG) + 1] if FLAG in argv and argv.index(FLAG) + 1 < len(argv) else ''
    if not url.lower().startswith('notion:'):
        return 2
    target, command = decide(url, registry, http_get)
    # Log only the scheme + path, never query tokens.
    parts = urlsplit(url)
    _log(dict(ts=time.time(), target=target, link=parts.scheme + '://' + parts.netloc + parts.path))
    (spawn or (lambda a: subprocess.Popen(a, shell=False, close_fds=True)))(command)
    return 0
