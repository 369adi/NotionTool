"""Window-limited screen capture for desktop apps such as Roblox Studio and Blender.

Windows only. One top-level window is captured with PrintWindow and
PW_RENDERFULLCONTENT, which also renders DirectX/OpenGL content and works while
the window is covered by other windows. If that yields a blank image, the
window area is copied from the screen instead. The whole desktop is never
captured.

Every stored capture is a full-resolution PNG under <data>/captures/<day>/ and
is logged as a capture_taken event (time, reason, task, hashes; never pixels).
A 64-bit difference hash marks captures that did not change since the previous
capture of the same window, so frequent checks stay cheap.
"""
import base64
import contextlib
import ctypes
import hashlib
import io
import os
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ALIASES = {
    'roblox': ('robloxstudiobeta', 'robloxstudio', 'robloxplayerbeta'),
    'roblox studio': ('robloxstudiobeta', 'robloxstudio'),
    'blender': ('blender', 'blender-launcher'),
}
DEFAULT_SIDE = 1600
MAX_SIDE = 3840
MIN_SIDE = 64
MAX_PER_TASK = 500        # runaway-loop guard only; normal work never gets near it
SAME_DISTANCE = 3         # dHash bits; at most this many differing bits = unchanged
RETENTION_DAYS = 7
MAX_FILES = 2000
PRUNE_EVERY = 600
LIST_LIMIT = 80

_PROPS = {
    'target': {'type': 'string', 'description': 'App or window title, for example "roblox", "blender" or part of a window title'},
    'hwnd': {'type': 'integer', 'description': 'Exact window handle from screen_windows'},
    'region': {'type': 'object', 'description': 'Optional crop in window pixels {x, y, width, height}; use it to zoom into details'},
    'max_side': {'type': 'integer', 'description': f'Longest side of the returned image, default {DEFAULT_SIDE}, at most {MAX_SIDE}'},
    'format': {'type': 'string', 'enum': ['jpeg', 'png'], 'description': 'jpeg (default, small) or png (sharp text and UI)'},
    'only_if_changed': {'type': 'boolean', 'description': 'Leave out the image when nothing changed since the previous capture of this window'},
    'restore': {'type': 'boolean', 'description': 'Restore the window first if it is minimized'},
    'reason': {'type': 'string', 'description': 'Short reason, written to the activity log'},
}
SCREEN_TOOLS = {
    'screen_windows': ('List open desktop app windows (hwnd, title, process, size) that screen_capture can target', {}),
    'screen_capture': ('Capture one desktop app window (for example Roblox Studio or Blender) and return the image. '
                       'Use it as often as needed to check what you are building. Never captures the whole screen.', _PROPS),
}
_RO = {'readOnlyHint': True, 'destructiveHint': False}
SCREEN_OPERATOR_TOOLS = [
    {'name': 'nt_screen_windows', 'description': 'Screen: ' + SCREEN_TOOLS['screen_windows'][0] + '.', 'annotations': _RO,
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'nt_screen_capture', 'description': 'Screen: ' + SCREEN_TOOLS['screen_capture'][0], 'annotations': _RO,
     'inputSchema': {'type': 'object', 'properties': _PROPS}},
]
SCREEN_OPERATOR_NAMES = {t['name'] for t in SCREEN_OPERATOR_TOOLS}


class CaptureError(ValueError):
    """A capture request that cannot be served (no matching window, minimized, ...)."""


# ---- Win32 -------------------------------------------------------------------
_W = None


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', ctypes.c_uint32), ('biWidth', ctypes.c_int32), ('biHeight', ctypes.c_int32),
                ('biPlanes', ctypes.c_uint16), ('biBitCount', ctypes.c_uint16), ('biCompression', ctypes.c_uint32),
                ('biSizeImage', ctypes.c_uint32), ('biXPelsPerMeter', ctypes.c_int32), ('biYPelsPerMeter', ctypes.c_int32),
                ('biClrUsed', ctypes.c_uint32), ('biClrImportant', ctypes.c_uint32)]


def _win():
    """Own WinDLL instances with full 64-bit signatures (shared windll stays untouched)."""
    global _W
    if _W is not None:
        return _W
    if os.name != 'nt':
        raise CaptureError('Screen capture needs Windows')
    from ctypes import wintypes as w
    u = ctypes.WinDLL('user32', use_last_error=True)
    g = ctypes.WinDLL('gdi32', use_last_error=True)
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    try:
        d = ctypes.WinDLL('dwmapi')
    except OSError:
        d = None

    def sig(f, res, *args):
        f.restype, f.argtypes = res, list(args)

    enum_proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    sig(u.EnumWindows, w.BOOL, enum_proc, w.LPARAM)
    for name in ('IsWindowVisible', 'IsWindow', 'IsIconic'):
        sig(getattr(u, name), w.BOOL, w.HWND)
    sig(u.GetWindowTextLengthW, ctypes.c_int, w.HWND)
    sig(u.GetWindowTextW, ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
    sig(u.GetWindowThreadProcessId, w.DWORD, w.HWND, ctypes.POINTER(w.DWORD))
    sig(u.GetWindowRect, w.BOOL, w.HWND, ctypes.POINTER(w.RECT))
    sig(u.GetWindowLongW, ctypes.c_long, w.HWND, ctypes.c_int)
    sig(u.GetForegroundWindow, w.HWND)
    sig(u.GetDC, w.HDC, w.HWND)
    sig(u.ReleaseDC, ctypes.c_int, w.HWND, w.HDC)
    sig(u.PrintWindow, w.BOOL, w.HWND, w.HDC, w.UINT)
    sig(u.ShowWindow, w.BOOL, w.HWND, ctypes.c_int)
    sig(g.CreateCompatibleDC, w.HDC, w.HDC)
    sig(g.CreateCompatibleBitmap, w.HBITMAP, w.HDC, ctypes.c_int, ctypes.c_int)
    sig(g.SelectObject, w.HGDIOBJ, w.HDC, w.HGDIOBJ)
    sig(g.BitBlt, w.BOOL, w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HDC, ctypes.c_int, ctypes.c_int, w.DWORD)
    sig(g.GetDIBits, ctypes.c_int, w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT)
    sig(g.DeleteObject, w.BOOL, w.HGDIOBJ)
    sig(g.DeleteDC, w.BOOL, w.HDC)
    sig(k.OpenProcess, w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
    sig(k.CloseHandle, w.BOOL, w.HANDLE)
    sig(k.QueryFullProcessImageNameW, w.BOOL, w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD))
    if d is not None:
        sig(d.DwmGetWindowAttribute, ctypes.c_long, w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD)
    dpi = getattr(u, 'SetThreadDpiAwarenessContext', None)
    if dpi is not None:
        sig(dpi, ctypes.c_void_p, ctypes.c_void_p)
    _W = SimpleNamespace(u=u, g=g, k=k, d=d, w=w, enum_proc=enum_proc, dpi=dpi)
    return _W


@contextlib.contextmanager
def _dpi_aware(W):
    """Per-monitor DPI awareness for this thread only, so sizes are physical pixels."""
    old = None
    if W.dpi is not None:
        try:
            old = W.dpi(ctypes.c_void_p(-4))  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        except OSError:
            old = None
    try:
        yield
    finally:
        if old:
            try:
                W.dpi(ctypes.c_void_p(old))
            except OSError:
                pass


def _process(W, pid):
    handle = W.k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ''
    try:
        size = W.w.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        return os.path.basename(buf.value) if W.k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ''
    finally:
        W.k.CloseHandle(handle)


def _cloaked(W, hwnd):
    if W.d is None:
        return False
    value = W.w.DWORD(0)
    ok = W.d.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(value), ctypes.sizeof(value)) == 0  # DWMWA_CLOAKED
    return ok and value.value != 0


def _bounds(W, hwnd):
    """Visible frame without the invisible resize border/shadow."""
    rect = W.w.RECT()
    if W.d is not None and W.d.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
        return rect.left, rect.top, rect.right, rect.bottom  # DWMWA_EXTENDED_FRAME_BOUNDS
    W.u.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def list_windows():
    W = _win()
    out, names = [], {}
    foreground = int(W.u.GetForegroundWindow() or 0)

    def visit(hwnd, _):
        try:
            if not hwnd or not W.u.IsWindowVisible(hwnd):
                return True
            length = W.u.GetWindowTextLengthW(hwnd)
            if length <= 0 or W.u.GetWindowLongW(hwnd, -20) & 0x80 or _cloaked(W, hwnd):  # WS_EX_TOOLWINDOW
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            W.u.GetWindowTextW(hwnd, buf, length + 1)
            pid = W.w.DWORD()
            W.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in names:
                names[pid.value] = _process(W, pid.value)
            left, top, right, bottom = _bounds(W, hwnd)
            out.append(dict(hwnd=int(hwnd), title=buf.value, process=names[pid.value], pid=pid.value,
                            width=right - left, height=bottom - top, minimized=bool(W.u.IsIconic(hwnd)),
                            foreground=int(hwnd) == foreground))
        except Exception:
            pass
        return True

    callback = W.enum_proc(visit)
    with _dpi_aware(W):
        W.u.EnumWindows(callback, 0)
    return out


def _read(W, dc, bitmap, width, height):
    from PIL import Image
    header = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
    info = ctypes.create_string_buffer(ctypes.sizeof(_BITMAPINFOHEADER) + 16)
    ctypes.memmove(info, ctypes.byref(header), ctypes.sizeof(header))
    data = ctypes.create_string_buffer(width * height * 4)
    if W.g.GetDIBits(dc, bitmap, 0, height, data, info, 0) != height:
        raise CaptureError('Reading the window image failed')
    raw = data.raw
    try:
        return Image.frombuffer('RGB', (width, height), raw, 'raw', 'BGRX', 0, 1).copy()
    except ValueError:
        return Image.frombuffer('RGBA', (width, height), raw, 'raw', 'BGRA', 0, 1).convert('RGB')


def _blank(image):
    low, high = image.convert('L').getextrema()
    return high - low < 4


def grab(hwnd, restore=False):
    """Return (PIL image, method) for one window; raises CaptureError with a clear reason."""
    W = _win()
    if not W.u.IsWindow(hwnd):
        raise CaptureError('The window was closed; call screen_windows again')
    if W.u.IsIconic(hwnd):
        if not restore:
            raise CaptureError('The window is minimized; restore it or pass restore=true')
        W.u.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.4)
    with _dpi_aware(W):
        rect = W.w.RECT()
        if not W.u.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise CaptureError('The window size is unavailable')
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < 2 or height < 2:
            raise CaptureError('The window has no visible area')
        frame = _bounds(W, hwnd)
        screen = W.u.GetDC(None)
        memory = W.g.CreateCompatibleDC(screen)
        bitmap = W.g.CreateCompatibleBitmap(screen, width, height)
        try:
            method = 'print_window'
            old = W.g.SelectObject(memory, bitmap)
            ok = W.u.PrintWindow(hwnd, memory, 2)  # PW_RENDERFULLCONTENT
            W.g.SelectObject(memory, old)
            image = _read(W, screen, bitmap, width, height) if ok else None
            if image is None or _blank(image):
                method = 'screen_copy'
                old = W.g.SelectObject(memory, bitmap)
                W.g.BitBlt(memory, 0, 0, width, height, screen, rect.left, rect.top, 0x00CC0020 | 0x40000000)
                W.g.SelectObject(memory, old)
                image = _read(W, screen, bitmap, width, height)
        finally:
            W.g.DeleteObject(bitmap)
            W.g.DeleteDC(memory)
            W.u.ReleaseDC(None, screen)
    box = (max(0, frame[0] - rect.left), max(0, frame[1] - rect.top),
           min(width, frame[2] - rect.left), min(height, frame[3] - rect.top))
    if box[2] - box[0] > 1 and box[3] - box[1] > 1 and box != (0, 0, width, height):
        image = image.crop(box)
    return image, method


# ---- pure helpers (tested without Win32) --------------------------------------
def pick(windows, target='', hwnd=0):
    if hwnd:
        for window in windows:
            if window['hwnd'] == int(hwnd):
                return window
        raise CaptureError('No open window has this hwnd; call screen_windows')
    query = str(target or '').strip().lower()
    if not query:
        raise CaptureError('Pass target (app name or window title) or hwnd; the whole screen is never captured')
    names = ALIASES.get(query, (query.removesuffix('.exe'),))

    def score(window):
        process = window['process'].lower().removesuffix('.exe')
        if process in names:
            return 3
        if any(name in process for name in names):
            return 2
        return 1 if query in window['title'].lower() else 0

    found = [w for w in windows if score(w) and w['width'] > 1 and w['height'] > 1]
    if not found:
        apps = sorted({w['process'] or w['title'][:40] for w in windows if w['process'] or w['title']})
        raise CaptureError(f'No open window matches "{target}". Open apps: ' + ', '.join(apps[:25]))
    found.sort(key=lambda w: (-score(w), w['minimized'], -(w['width'] * w['height'])))
    return found[0]


def dhash(image):
    from PIL import Image
    resample = getattr(Image, 'Resampling', Image).BILINEAR
    pixels = image.convert('L').resize((9, 8), resample).tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | int(pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return bits


def _region(value, size):
    if not value:
        return None
    if not isinstance(value, dict):
        raise CaptureError('region must be an object with x, y, width and height')
    try:
        x, y, w, h = int(value.get('x', 0)), int(value.get('y', 0)), int(value['width']), int(value['height'])
    except (KeyError, TypeError, ValueError):
        raise CaptureError('region needs integer x, y, width and height') from None
    left, top, right, bottom = max(0, x), max(0, y), min(size[0], x + w), min(size[1], y + h)
    if right - left < 2 or bottom - top < 2:
        raise CaptureError(f'region is outside the window ({size[0]}x{size[1]} pixels)')
    return left, top, right, bottom


def _encode(image, max_side=None, fmt=None):
    from PIL import Image
    side = max(MIN_SIDE, min(MAX_SIDE, int(max_side or DEFAULT_SIDE)))
    out = image
    if max(image.size) > side:
        out = image.copy()
        out.thumbnail((side, side), getattr(Image, 'Resampling', Image).LANCZOS)
    kind = 'png' if str(fmt or '').lower() == 'png' else 'jpeg'
    buf = io.BytesIO()
    if kind == 'png':
        out.save(buf, 'PNG', compress_level=6)
    else:
        out.save(buf, 'JPEG', quality=85)
    data = buf.getvalue()
    return dict(data=base64.b64encode(data).decode('ascii'), mimeType='image/' + kind,
                width=out.width, height=out.height, bytes=len(data))


class ScreenCapture:
    def __init__(self, store=None, folder=None, lister=None, grabber=None):
        self.store = store
        base = getattr(store, 'directory', None)
        self.folder = Path(folder) if folder else (Path(base) / 'captures' if base else Path('data') / 'captures')
        self.lister = lister or list_windows
        self.grabber = grabber or grab
        self.lock = threading.Lock()
        self.last = {}
        self.counts = {}
        self.pruned = 0.0

    def call(self, name, args, task_id='', run_id='', role=''):
        args = args or {}
        if name == 'screen_windows':
            items = [w for w in self.lister() if w['width'] > 1 and w['height'] > 1]
            items.sort(key=lambda w: (not w['foreground'], w['minimized'], w['process'].lower()))
            return {'windows': items[:LIST_LIMIT], 'count': len(items)}
        if name == 'screen_capture':
            return self.capture(args, task_id=task_id, run_id=run_id, role=role)
        raise ValueError('Unknown screen tool')

    def capture(self, args, task_id='', run_id='', role=''):
        started = time.monotonic()
        if task_id:
            with self.lock:
                if self.counts.get(task_id, 0) >= MAX_PER_TASK:
                    raise CaptureError(f'Capture limit of {MAX_PER_TASK} per task reached')
        window = pick(self.lister(), args.get('target', ''), args.get('hwnd') or 0)
        image, method = self.grabber(window['hwnd'], bool(args.get('restore')))
        size = image.size
        region = _region(args.get('region'), size)
        if region:
            image = image.crop(region)
        digest = dhash(image)
        key = (window['hwnd'], region)
        with self.lock:
            previous = self.last.get(key)
        distance = bin(digest ^ previous['hash']).count('1') if previous else None
        changed = previous is None or distance > SAME_DISTANCE or not Path(previous['path']).is_file()
        path, sha = self._save(image, window) if changed else (previous['path'], previous['sha256'])
        with self.lock:
            self.last[key] = dict(hash=digest, path=path, sha256=sha, ts=time.time())
            if len(self.last) > 256:
                del self.last[min(self.last, key=lambda k: self.last[k]['ts'])]
            if task_id:
                self.counts[task_id] = self.counts.get(task_id, 0) + 1
        info = dict(window=dict(hwnd=window['hwnd'], title=window['title'], process=window['process']),
                    width=image.width, height=image.height, window_size=list(size),
                    region=list(region) if region else None, method=method, changed=changed,
                    distance=distance, phash=f'{digest:016x}', sha256=sha, path=path)
        part = None
        if changed or not args.get('only_if_changed'):
            part = _encode(image, args.get('max_side'), args.get('format'))
            info['image'] = {k: part[k] for k in ('mimeType', 'width', 'height', 'bytes')}
        else:
            info['note'] = 'Unchanged since the previous capture of this window; no image attached'
        info['ms'] = round((time.monotonic() - started) * 1000)
        self._log(info, args.get('reason', ''), task_id, run_id, role)
        self._prune()
        if part:
            info['_image'] = {'data': part['data'], 'mimeType': part['mimeType']}
        return info

    def _save(self, image, window):
        folder = self.folder / time.strftime('%Y-%m-%d')
        folder.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r'[^A-Za-z0-9_-]+', '-', (window['process'] or 'window').removesuffix('.exe'))[:40] or 'window'
        path = folder / f"{time.strftime('%H%M%S')}-{time.time_ns() % 10**9:09d}-{stem}.png"
        buf = io.BytesIO()
        image.save(buf, 'PNG', compress_level=1)
        data = buf.getvalue()
        path.write_bytes(data)
        return str(path), hashlib.sha256(data).hexdigest()

    def _log(self, info, reason, task_id, run_id, role):
        if self.store is None:
            return
        window = info['window']
        try:
            self.store.event('capture_taken', run_id or '', task_id=task_id or '', role=role or '',
                             reason=str(reason or '')[:200], process=window['process'], title=window['title'][:120],
                             hwnd=window['hwnd'], width=info['width'], height=info['height'], method=info['method'],
                             changed=info['changed'], distance=info['distance'], phash=info['phash'],
                             sha256=info['sha256'], path=info['path'], image_bytes=(info.get('image') or {}).get('bytes', 0),
                             ms=info['ms'], message='Screen capture: ' + (window['process'] or window['title'][:60]))
        except Exception:
            pass

    def _prune(self):
        now = time.time()
        with self.lock:
            if now - self.pruned < PRUNE_EVERY:
                return
            self.pruned = now
        try:
            files = sorted(self.folder.glob('*/*.png'), key=lambda p: p.stat().st_mtime)
            extra, cutoff = len(files) - MAX_FILES, now - RETENTION_DAYS * 86400
            for index, file in enumerate(files):
                if index < extra or file.stat().st_mtime < cutoff:
                    file.unlink(missing_ok=True)
                else:
                    break
            for folder in self.folder.iterdir():
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
        except OSError:
            pass
