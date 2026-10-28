"""Keeps the NotionTool window usable when the PC is short on RAM (user 2026-10-28 06:19).

A light daemon thread, no polling of the page:
- our window processes (profile dir only, never the user's Notion) run at ABOVE_NORMAL priority, so the
  window still answers while a game or Roblox Studio uses the PC;
- when free RAM is low, Chromium in our window gets a memory pressure signal and frees its caches
  (Memory.simulatePressureNotification), at most once per PRESSURE_EVERY_S;
- the window gets the purple NotionTool taskbar icon and its own taskbar button (desktop_icon.py, 06:27).
"""
import ctypes
import sys
import threading

CHECK_S = 10
PRIORITY_EVERY_S = 300
PIDS_RETRY_S = 30
PRESSURE_EVERY_S = 120
LOW_FREE_MB = 1500
ABOVE_NORMAL = 0x00008000
PROCESS_SET_INFORMATION = 0x0200


class _MemStatus(ctypes.Structure):
    _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]


def free_mb():
    """Free physical RAM in MB, or None off Windows."""
    if sys.platform != 'win32':
        return None
    st = _MemStatus()
    st.dwLength = ctypes.sizeof(_MemStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    return int(st.ullAvailPhys // (1024 * 1024))


def raise_priority(pids):
    done = 0
    if sys.platform != 'win32':
        return done
    k32 = ctypes.windll.kernel32
    for pid in pids:
        h = k32.OpenProcess(PROCESS_SET_INFORMATION, False, int(pid))
        if not h:
            continue
        try:
            if k32.SetPriorityClass(h, ABOVE_NORMAL):
                done += 1
        finally:
            k32.CloseHandle(h)
    return done


def _default_icon():
    if sys.platform != 'win32':
        return None
    from .desktop_icon import TaskbarIcon
    return TaskbarIcon()


class DesktopGuard:
    def __init__(self, controller, list_pids, stop_event, clock=None, icon='auto'):
        import time
        self.controller, self.list_pids, self.stop_event = controller, list_pids, stop_event
        self.clock = clock or time.monotonic
        self.icon = _default_icon() if icon == 'auto' else icon
        self.icon_ok = False
        self.pids = []
        self._prio_at = None
        self._press_at = None
        self.pressure_sent = 0
        self.thread = None

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name='desktop-guard')
        self.thread.start()

    def _session(self):
        shell = getattr(self.controller, 'shell', None)
        return getattr(shell, 'session', None)

    def step(self):
        now = self.clock()
        due = self._prio_at is None or now - self._prio_at >= PRIORITY_EVERY_S
        if not due and self.icon is not None and not self.icon_ok:  # window not found yet / restarted
            due = now - self._prio_at >= PIDS_RETRY_S
        if due:
            self._prio_at = now
            try:
                self.pids = list(self.list_pids())
                raise_priority(self.pids)
            except Exception:  # noqa: BLE001
                pass
        if self.icon is not None and self.pids:
            try:
                self.icon_ok = self.icon.apply(self.pids) > 0
            except Exception:  # noqa: BLE001
                self.icon_ok = False
        free = free_mb()
        if free is not None and free < LOW_FREE_MB and (self._press_at is None or now - self._press_at >= PRESSURE_EVERY_S):
            session = self._session()
            if session is not None:
                self._press_at = now
                try:
                    session.send('Memory.simulatePressureNotification', {'level': 'critical'}, 5)
                    self.pressure_sent += 1
                except Exception:  # noqa: BLE001
                    pass

    def _run(self):
        if self.stop_event.wait(3):  # let the window open first
            return
        while not self.stop_event.is_set():
            self.step()
            if self.stop_event.wait(CHECK_S):
                return
