"""Open a user's existing Notion session in an isolated, visible Edge window."""
import subprocess
import threading
import os
import time


def show_owned_window(process):
    """Restore only this account browser's native windows on the main monitor."""
    if os.name != 'nt':
        return
    import ctypes
    from ctypes import wintypes
    user32=ctypes.WinDLL('user32',use_last_error=True)
    callback=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    def show(window,_):
        pid=wintypes.DWORD()
        user32.GetWindowThreadProcessId(window,ctypes.byref(pid))
        if pid.value==process.pid and user32.IsWindowVisible(window):
            user32.ShowWindowAsync(window,9)
            user32.SetWindowPos(window,0,80,80,1280,900,0x0040)
            user32.SetForegroundWindow(window)
        return True
    user32.EnumWindows(callback(show),0)


class AccountPages:
    def __init__(self, adapter, browser_factory=None):
        self.adapter = adapter
        self.factory = browser_factory
        self.lock = threading.RLock()
        self.pages = {}

    def open(self, account_id):
        account = self.adapter.store.get('accounts', account_id)
        if not account or account.get('state') != 'ready':
            raise ValueError('Finish signing in before opening this Notion account')
        client = self.adapter.client(account_id)
        if client.identity().get('email', '').lower() != account['email'].lower():
            raise ValueError('Notion session does not match this account')
        with self.lock:
            if account_id in self.pages:
                show_owned_window(self.pages[account_id].process)
                return {'opened': True, 'already_open': True, 'account_id': account_id}
            from .edge_profile import EdgeProfile
            browser = (self.factory or EdgeProfile)(self.adapter.store.directory,
                                                     self.adapter.sessions.load(account_id))
            try:
                browser.prepare()  # Closes the temporary loopback session-import browser.
                process = subprocess.Popen([str(browser.edge), '--user-data-dir='+str(browser.profile),
                    '--no-first-run', '--no-default-browser-check', '--window-position=80,80',
                    '--window-size=1280,900', '--new-window', 'https://app.notion.com/'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                browser.process = process
                self.pages[account_id] = browser
            except Exception:
                browser.__exit__(None, None, None)
                raise
        def monitor():
            for _ in range(30):
                if process.poll() is not None: break
                show_owned_window(process)
                time.sleep(.1)
            process.wait()
            self._close(account_id, browser)
        threading.Thread(target=monitor, daemon=True, name='notion-account-page').start()
        return {'opened': True, 'account_id': account_id}

    def _close(self, account_id, browser):
        with self.lock:
            if self.pages.get(account_id) is not browser:
                return
            self.pages.pop(account_id)
            browser.__exit__(None, None, None)

    def close(self):
        for account_id, browser in list(self.pages.items()):
            self._close(account_id, browser)
