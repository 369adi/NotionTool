"""Purple NotionTool icon in the taskbar for the NotionTool window (user 2026-10-28 06:27).

The window is a Notion.exe copy, so Windows shows Notion's icon and groups it with the user's normal
Notion. For our own top-level windows only (pids of our profile) this sets a separate AppUserModelID
(own taskbar button) and the NotionTool icon (WM_SETICON). Hung windows are skipped (SendMessageTimeout).
"""
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

AUMID = 'NotionTool.Desktop'
WM_SETICON, WM_GETICON, ICON_SMALL, ICON_BIG = 0x0080, 0x007F, 0, 1
SMTO_ABORTIFHUNG = 0x0002
VT_LPWSTR = 31


class GUID(ctypes.Structure):
    _fields_ = [('Data1', ctypes.c_ulong), ('Data2', ctypes.c_ushort), ('Data3', ctypes.c_ushort),
                ('Data4', ctypes.c_ubyte * 8)]


def _guid(s):
    import uuid
    u = uuid.UUID(s)
    g = GUID()
    g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [('fmtid', GUID), ('pid', ctypes.c_ulong)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [('vt', ctypes.c_ushort), ('r1', ctypes.c_ushort), ('r2', ctypes.c_ushort), ('r3', ctypes.c_ushort),
                ('p', ctypes.c_void_p), ('p2', ctypes.c_void_p)]


def icon_source():
    """('exe', path) when frozen (NotionTool.exe carries the icon), else ('ico', assets/notiontool.ico)."""
    if getattr(sys, 'frozen', False):
        return 'exe', sys.executable
    ico = Path(__file__).resolve().parent.parent / 'assets' / 'notiontool.ico'
    return ('ico', str(ico)) if ico.is_file() else ('', '')


class TaskbarIcon:
    def __init__(self):
        self.big = self.small = None
        self.done_aumid = set()
        self.applied = 0
        self._com = False

    def _load(self):
        if self.big:
            return True
        kind, path = icon_source()
        u32 = ctypes.windll.user32
        if kind == 'exe':
            big, small = wintypes.HICON(), wintypes.HICON()
            if ctypes.windll.shell32.ExtractIconExW(path, 0, ctypes.byref(big), ctypes.byref(small), 1) > 0:
                self.big, self.small = big.value, small.value
        elif kind == 'ico':
            u32.LoadImageW.restype = wintypes.HANDLE
            u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            self.big = u32.LoadImageW(None, path, 1, u32.GetSystemMetrics(11), u32.GetSystemMetrics(12), 0x10)
            self.small = u32.LoadImageW(None, path, 1, u32.GetSystemMetrics(49), u32.GetSystemMetrics(50), 0x10)
        return bool(self.big)

    @staticmethod
    def windows(pids):
        """Visible, unowned top-level windows of these processes."""
        u32 = ctypes.windll.user32
        want, found = set(int(p) for p in pids), []
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _):
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in want and u32.IsWindowVisible(hwnd) and not u32.GetWindow(hwnd, 4):
                found.append(hwnd)
            return True
        u32.EnumWindows(proto(cb), 0)
        return found

    def _aumid(self, hwnd):
        if hwnd in self.done_aumid:
            return
        ole32, shell32 = ctypes.windll.ole32, ctypes.windll.shell32
        if not self._com:
            ole32.CoInitialize(None)
            self._com = True
        store = ctypes.c_void_p()
        iid = _guid('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99')
        if shell32.SHGetPropertyStoreForWindow(wintypes.HWND(hwnd), ctypes.byref(iid), ctypes.byref(store)) != 0 or not store:
            return
        vtbl = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(vtbl[6])
        commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[7])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        try:
            key = PROPERTYKEY(_guid('9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3'), 5)  # PKEY_AppUserModel_ID
            buf = ctypes.create_unicode_buffer(AUMID)
            pv = PROPVARIANT()
            pv.vt, pv.p = VT_LPWSTR, ctypes.cast(buf, ctypes.c_void_p).value
            if set_value(store, ctypes.byref(key), ctypes.byref(pv)) == 0 and commit(store) == 0:
                self.done_aumid.add(hwnd)
        finally:
            release(store)

    def apply(self, pids):
        """Number of windows that now carry the NotionTool icon."""
        if sys.platform != 'win32' or not self._load():
            return 0
        u32 = ctypes.windll.user32
        done = 0
        for hwnd in self.windows(pids):
            try:
                self._aumid(hwnd)
            except Exception:  # noqa: BLE001
                pass
            res = ctypes.c_size_t()
            cur = u32.SendMessageTimeoutW(wintypes.HWND(hwnd), WM_GETICON, ICON_BIG, 0, SMTO_ABORTIFHUNG, 300, ctypes.byref(res))
            if cur and res.value == self.big:
                done += 1
                continue
            u32.SendMessageTimeoutW(wintypes.HWND(hwnd), WM_SETICON, ICON_BIG, wintypes.LPARAM(self.big), SMTO_ABORTIFHUNG, 300, ctypes.byref(res))
            if self.small:
                u32.SendMessageTimeoutW(wintypes.HWND(hwnd), WM_SETICON, ICON_SMALL, wintypes.LPARAM(self.small), SMTO_ABORTIFHUNG, 300, ctypes.byref(res))
            done += 1
        self.applied = done
        return done
