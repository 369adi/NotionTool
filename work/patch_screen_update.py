"""Screen capture without storage + whole screen + no token in own chats; wire the auto-updater."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
fails = []


def edit(rel, steps):
    path = ROOT / rel
    raw = path.read_bytes().decode('utf-8')
    crlf = '\r\n' in raw
    text = raw.replace('\r\n', '\n')
    for old, new in steps:
        if callable(old):
            text = old(text)
            continue
        if old not in text:
            fails.append(f'{rel}: missing {old[:70]!r}')
            continue
        text = text.replace(old, new, 1)
    if crlf:
        text = text.replace('\n', '\r\n')
    path.write_bytes(text.encode('utf-8'))


def replace_method(name, new):
    def run(text):
        start = text.find(f'    def {name}(')
        if start < 0:
            fails.append(f'screen_capture: no method {name}')
            return text
        lines = text[start:].split('\n')
        end = 1
        while end < len(lines):
            line = lines[end]
            if line.startswith('    def ') or (line and not line.startswith(' ')):
                break
            end += 1
        rest = '\n'.join(lines[end:])
        return text[:start] + new + ('\n\n' + rest.lstrip('\n') if rest.strip() else '\n')
    return run


DOC = '''"""Screen capture for desktop work such as Roblox Studio and Blender (Windows only).

Default: the whole screen (every monitor, the virtual desktop) is copied with BitBlt.
With target or hwnd one top-level window is captured with PrintWindow and
PW_RENDERFULLCONTENT, which also renders DirectX/OpenGL content and works while the
window is covered; a blank result falls back to copying that screen area.

Images are never written to disk: a capture exists only in memory until its image
data has been encoded into the tool response, then it is gone. Only a 64-bit
difference hash per target stays in memory, so only_if_changed can skip unchanged
images. A capture_taken event logs time, reason, task and hashes (never pixels).
Captures stored on disk by older versions are deleted on first use.
"""
'''

GRAB_SCREEN = '''def grab_screen():
    """Return (PIL image, 'screen') of the whole virtual desktop (all monitors)."""
    W = _win()
    with _dpi_aware(W):
        left, top = W.u.GetSystemMetrics(76), W.u.GetSystemMetrics(77)      # SM_X/YVIRTUALSCREEN
        width, height = W.u.GetSystemMetrics(78), W.u.GetSystemMetrics(79)  # SM_CX/CYVIRTUALSCREEN
        if width < 2 or height < 2:
            raise CaptureError('The screen size is unavailable')
        screen = W.u.GetDC(None)
        memory = W.g.CreateCompatibleDC(screen)
        bitmap = W.g.CreateCompatibleBitmap(screen, width, height)
        try:
            old = W.g.SelectObject(memory, bitmap)
            W.g.BitBlt(memory, 0, 0, width, height, screen, left, top, 0x00CC0020 | 0x40000000)  # SRCCOPY|CAPTUREBLT
            W.g.SelectObject(memory, old)
            image = _read(W, screen, bitmap, width, height)
        finally:
            W.g.DeleteObject(bitmap)
            W.g.DeleteDC(memory)
            W.u.ReleaseDC(None, screen)
    return image, 'screen'


# ---- pure helpers (tested without Win32) --------------------------------------
'''

PURGE = '''    def _purge(self):
        """Delete captures stored by older versions (they kept PNG files for 7 days)."""
        if self.purged:
            return
        self.purged = True
        try:
            if self.folder.is_dir():
                shutil.rmtree(self.folder, ignore_errors=True)
        except OSError:
            pass'''


def new_doc(text):
    end = text.find('"""', 3) + 4
    return DOC + text[end:].lstrip('\n') if text.startswith('"""') else text


edit('notiontool/screen_capture.py', [
    (new_doc, None),
    ('import re\n', 'import re\nimport shutil\n'),
    ('RETENTION_DAYS = 7\nMAX_FILES = 2000\nPRUNE_EVERY = 600\n', ''),
    ("'target': {'type': 'string', 'description': 'App or window title, for example \"roblox\", \"blender\" or part of a window title'},",
     "'target': {'type': 'string', 'description': 'Optional: app or window title (\"roblox\", \"blender\" or part of a title) to capture only that window. Leave target and hwnd empty to capture the whole screen'},"),
    ("'hwnd': {'type': 'integer', 'description': 'Exact window handle from screen_windows'},",
     "'hwnd': {'type': 'integer', 'description': 'Optional: exact window handle from screen_windows'},"),
    ("'Optional crop in window pixels {x, y, width, height}; use it to zoom into details'",
     "'Optional crop in captured pixels {x, y, width, height}; use it to zoom into details'"),
    ("since the previous capture of this window'", "since the previous capture of the same screen or window'"),
    ("'screen_windows': ('List open desktop app windows (hwnd, title, process, size) that screen_capture can target', {}),",
     "'screen_windows': ('List open desktop app windows (hwnd, title, process, size); screen_capture can target one of them', {}),"),
    ("'screen_capture': ('Capture one desktop app window (for example Roblox Studio or Blender) and return the image. '\n                       'Use it as often as needed to check what you are building. Never captures the whole screen.', _PROPS),",
     "'screen_capture': ('Capture the whole screen (all monitors) and return the image; pass target or hwnd to capture only '\n                       'one app window (for example Roblox Studio or Blender). No assignment token or code is needed. '\n                       'Use it as often as needed to check what you are building. Images are never stored.', _PROPS),"),
    ('    sig(u.GetForegroundWindow, w.HWND)\n', '    sig(u.GetForegroundWindow, w.HWND)\n    sig(u.GetSystemMetrics, ctypes.c_int, ctypes.c_int)\n'),
    ('# ---- pure helpers (tested without Win32) --------------------------------------\n', GRAB_SCREEN),
    ("        raise CaptureError('Pass target (app name or window title) or hwnd; the whole screen is never captured')\n",
     '        return None  # no target: the caller captures the whole screen\n'),
    ('    def __init__(self, store=None, folder=None, lister=None, grabber=None):',
     '    def __init__(self, store=None, folder=None, lister=None, grabber=None, screen_grabber=None):'),
    ('        self.grabber = grabber or grab\n', '        self.grabber = grabber or grab\n        self.screen_grabber = screen_grabber or grab_screen\n        self.purged = False\n'),
    ('        self.pruned = 0.0\n', ''),
    ("        window = pick(self.lister(), args.get('target', ''), args.get('hwnd') or 0)\n        image, method = self.grabber(window['hwnd'], bool(args.get('restore')))\n",
     "        self._purge()\n        window = pick(self.lister(), args.get('target', ''), args.get('hwnd') or 0) if (args.get('target') or args.get('hwnd')) else None\n"
     "        if window is None:\n            window = dict(hwnd=0, title='Whole screen', process='screen')\n            image, method = self.screen_grabber()\n"
     "        else:\n            image, method = self.grabber(window['hwnd'], bool(args.get('restore')))\n"),
    ("        changed = previous is None or distance > SAME_DISTANCE or not Path(previous['path']).is_file()\n        path, sha = self._save(image, window) if changed else (previous['path'], previous['sha256'])\n        with self.lock:\n            self.last[key] = dict(hash=digest, path=path, sha256=sha, ts=time.time())\n",
     "        changed = previous is None or distance > SAME_DISTANCE\n        sha = hashlib.sha256(image.tobytes()).hexdigest()  # identity only; the pixels are not kept\n        with self.lock:\n            self.last[key] = dict(hash=digest, sha256=sha, ts=time.time())\n"),
    ("region=list(region) if region else None, method=method,", "region=list(region) if region else None, scope='window' if window['hwnd'] else 'screen', method=method,"),
    ("sha256=sha, path=path)", "sha256=sha, stored=False)"),
    ("'Unchanged since the previous capture of this window; no image attached'", "'Unchanged since the previous capture of the same screen or window; no image attached'"),
    ('        self._prune()\n', ''),
    ("sha256=info['sha256'], path=info['path'],", "sha256=info['sha256'],"),
    (lambda t: (lambda s, e: t[:s] + t[e:] if s >= 0 and e > s else (fails.append('screen_capture: _save not removed') or t))(t.find('    def _save(self, image, window):'), t.find('    def _log(')), None),
    (replace_method('_prune', PURGE), None),
])

edit('notiontool/bridge.py', [
    ('    def assignment(self, token):\n',
     "    def has_assignment(self, token):\n        return bool(token) and any(t.get('lease')==token and t.get('state')=='running' for t in self.store.all('tasks'))\n\n    def assignment(self, token):\n"),
    ('    def call(self, name, args, token):\n        cwd = None\n',
     "    def call(self, name, args, token):\n        if name in SCREEN_TOOLS and self.screen_access() and not self.has_assignment(token):\n"
     "            # Read-only screen tools also work in the user's own chats (Main), which have no assignment token.\n"
     "            return self.screen().call(name, args, role='chat')\n        cwd = None\n"),
    ("'Execute a scoped tool using the assignment token from your current prompt'",
     "'Execute a NotionTool tool. Coworker tools need the assignment token from your current prompt; screen_windows and screen_capture need no token or code (leave assignment_token empty)'"),
    ("'required':['tool_name','assignment_token']", "'required':['tool_name']"),
    ("value.append({'id':'Screen','tools':list(SCREEN_TOOLS)})", "value.append({'id':'Screen','tools':list(SCREEN_TOOLS),'assignment_token':'not needed'})"),
])

edit('main.py', [
    ("        sys.exit(gateway_main(sys.argv))\n",
     "        sys.exit(gateway_main(sys.argv))\n    from notiontool.updater import handoff\n    sys.argv = handoff(sys.argv)  # waits for the previous instance; installs a downloaded update\n"),
])


def wire_server(text):
    marker = "name='chat-context').start()"
    at = text.find(marker)
    if at < 0:
        fails.append('server: chat-context thread not found')
        return text
    line_start = text.rfind('\n', 0, at) + 1
    indent = text[line_start:len(text[:line_start]) + len(text[line_start:]) - len(text[line_start:].lstrip())]
    end = text.find('\n', at)
    text = text[:end + 1] + indent + 'self.updater = Updater(self)\n' + indent + 'self.updater.start()\n' + text[end + 1:]
    first = text.find('\nfrom .')
    return text[:first + 1] + 'from .updater import Updater\n' + text[first + 1:]


edit('notiontool/server.py', [(wire_server, None)])
edit('NotionTool.spec', [("hiddenimports=['webview.platforms.edgechromium']", "hiddenimports=['webview.platforms.edgechromium', 'notiontool._build']")])

for gi in (ROOT / '.gitignore', ROOT.parent / 'NotionTool-publish' / '.gitignore'):
    if gi.is_file() and 'notiontool/_build.py' not in gi.read_text(encoding='utf-8'):
        with open(gi, 'a', encoding='utf-8', newline='') as f:
            f.write('notiontool/_build.py\n')

print('FAILS:', fails or 'none')
sys.exit(1 if fails else 0)
