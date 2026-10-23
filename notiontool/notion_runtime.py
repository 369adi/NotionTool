"""Pinned private Notion runtime for the NotionTool window (fix 2026-10-23).

Root cause: NotionTool always launched the SYSTEM Notion (Store/MSIX). When Notion updated itself the
NotionTool window was suddenly a different Notion build and the overlay/MCP stopped working.

Now the NotionTool window runs from its OWN copy of Notion:
  %LOCALAPPDATA%\\NotionTool-App\\runtime\\<version>\\Notion\\Notion.exe
- The copy is made once from the installed Notion; a later Store update of the system Notion does
  not change it. Its own updater is switched off (resources/app-update.yml points to a dead local
  address; the original is kept as app-update.yml.orig).
- A new Notion version is only taken over by an explicit upgrade(). It becomes 'current' but not
  'last_good' until the NotionTool overlay really loaded on it (mark_good). If the overlay does not
  load, rollback() switches back to the last good version.
- At most KEEP versions are kept (always current and last_good).
- The user's normal Notion install and profile are never changed: only files under runtime_root().
run and copy are injectable; tests use fakes only.
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

PACKAGE = 'com.notion.app.desktop.notion'
UPDATE_FILE = 'app-update.yml'
DEAD_URL = 'http://127.0.0.1:9/notiontool-pinned'
STATE_FILE = 'state.json'
KEEP = 2


def runtime_root():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'NotionTool-App' / 'runtime'


def _root(root):
    return Path(root) if root else runtime_root()


def safe_version(version):
    text = re.sub(r'[^0-9A-Za-z._-]', '_', str(version or '').strip())[:40]
    return text or 'unknown'


def exe_of(version, root=None):
    return _root(root) / safe_version(version) / 'Notion' / 'Notion.exe'


# ---- system install ---------------------------------------------------------
def system_install(run=subprocess.run):
    """(notion_dir, version) of the installed system Notion (Store first, then classic) or (None, '')."""
    try:
        out = run(['powershell.exe', '-NoProfile', '-Command',
                   '$p = Get-AppxPackage %s; if ($p) { $p.Version; $p.InstallLocation }' % PACKAGE],
                  capture_output=True, text=True, timeout=20)
        lines = [l.strip() for l in (out.stdout or '').splitlines() if l.strip()]
        if len(lines) >= 2:
            folder = Path(lines[-1]) / 'Notion'
            if (folder / 'Notion.exe').exists():
                return folder, lines[-2]
    except Exception:
        pass
    classic = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Notion'
    if (classic / 'Notion.exe').exists():
        version = ''
        try:
            out = run(['powershell.exe', '-NoProfile', '-Command',
                       '(Get-Item -LiteralPath "%s").VersionInfo.ProductVersion' % (classic / 'Notion.exe')],
                      capture_output=True, text=True, timeout=20)
            version = (out.stdout or '').strip().splitlines()[-1].strip() if (out.stdout or '').strip() else ''
        except Exception:
            pass
        return classic, version or 'classic'
    return None, ''


def system_exe(run=subprocess.run):
    folder, _ = system_install(run)
    if folder is None:
        raise FileNotFoundError('The Notion desktop app is not installed')
    return str(folder / 'Notion.exe')


# ---- state ------------------------------------------------------------------
def load_state(root=None):
    try:
        state = json.loads((_root(root) / STATE_FILE).read_text(encoding='utf-8'))
        if isinstance(state, dict):
            state.setdefault('versions', [])
            return state
    except (OSError, ValueError):
        pass
    return {'current': '', 'last_good': '', 'versions': []}


def save_state(state, root=None):
    root = _root(root)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (STATE_FILE + '.tmp')
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding='utf-8')
    os.replace(tmp, root / STATE_FILE)
    return state


def status(root=None, run=None):
    state = load_state(root)
    out = dict(state, root=str(_root(root)),
               present=[v for v in state.get('versions') or [] if exe_of(v, root).exists()])
    if run is not None:
        _, system = system_install(run)
        out['system_version'] = system
        out['upgrade_available'] = bool(system) and system != state.get('current')
    return out


# ---- updater ----------------------------------------------------------------
def disable_updater(notion_dir):
    """Point the copy's own updater at a dead local address. True if the file was changed now."""
    path = Path(notion_dir) / 'resources' / UPDATE_FILE
    if not path.exists():
        return False
    text = path.read_text(encoding='utf-8', errors='replace')
    if DEAD_URL in text:
        return False
    orig = path.with_name(UPDATE_FILE + '.orig')
    if not orig.exists():
        orig.write_text(text, encoding='utf-8')
    lines = [("url: '%s'" % DEAD_URL) if re.match(r'\s*url\s*:', line) else line for line in text.splitlines()]
    if not any(DEAD_URL in line for line in lines):
        lines.append("url: '%s'" % DEAD_URL)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return True


def updater_disabled(notion_dir):
    path = Path(notion_dir) / 'resources' / UPDATE_FILE
    return (not path.exists()) or DEAD_URL in path.read_text(encoding='utf-8', errors='replace')


# ---- pin / upgrade / rollback ------------------------------------------------
def pin(src_dir, version, root=None, copy=shutil.copytree):
    """Copy the Notion folder to runtime/<version>/Notion (once) and make it current. Returns the exe."""
    root = _root(root)
    name = safe_version(version)
    dest = root / name
    exe = dest / 'Notion' / 'Notion.exe'
    if not exe.exists():
        partial = root / (name + '.partial')
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir(parents=True, exist_ok=True)
        copy(str(src_dir), str(partial / 'Notion'))
        if not (partial / 'Notion' / 'Notion.exe').exists():
            shutil.rmtree(partial, ignore_errors=True)
            raise RuntimeError('The Notion copy is incomplete (no Notion.exe)')
        disable_updater(partial / 'Notion')
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(partial, dest)
    disable_updater(dest / 'Notion')
    state = load_state(root)
    state['versions'] = [v for v in state.get('versions') or [] if v != version] + [version]
    state['current'] = version
    state.setdefault('pinned_at', {})[version] = state.get('pinned_at', {}).get(version) or int(time.time())
    save_state(state, root)
    return str(exe)


def pinned_exe(root=None):
    """Exe of the current pinned version; falls back to last_good, then the newest present copy."""
    state = load_state(root)
    order = [state.get('current'), state.get('last_good')] + list(reversed(state.get('versions') or []))
    for version in order:
        if version and exe_of(version, root).exists():
            return str(exe_of(version, root))
    return None


def current_version(root=None):
    return load_state(root).get('current') or ''


def ensure(run=subprocess.run, root=None, copy=shutil.copytree):
    """Exe for the NotionTool window. First start pins the installed Notion. Never follows an update alone."""
    exe = pinned_exe(root)
    if exe:
        return exe
    src, version = system_install(run)
    if src is None:
        raise FileNotFoundError('The Notion desktop app is not installed')
    return pin(src, version, root, copy)


def upgrade(run=subprocess.run, root=None, copy=shutil.copytree):
    """Take over the installed Notion version as 'current' (on trial until mark_good). None if nothing new."""
    src, version = system_install(run)
    if src is None or not version or version == current_version(root):
        return None
    return pin(src, version, root, copy)


def prune(root=None, keep=KEEP):
    state = load_state(root)
    protect = {state.get('current'), state.get('last_good')}
    versions = list(state.get('versions') or [])
    removed = []
    while len(versions) > keep:
        victim = next((v for v in versions if v not in protect), None)
        if victim is None:
            break
        versions.remove(victim)
        shutil.rmtree(_root(root) / safe_version(victim), ignore_errors=True)
        removed.append(victim)
    state['versions'] = versions
    save_state(state, root)
    return removed


def mark_good(version=None, root=None):
    """The overlay loaded on this version: it becomes last_good; older copies beyond KEEP go."""
    state = load_state(root)
    version = version or state.get('current')
    if not version or version not in (state.get('versions') or []):
        return False
    if state.get('last_good') != version:
        state['last_good'] = version
        save_state(state, root)
    prune(root)
    return True


def rollback(root=None):
    """Switch back to last_good when current failed. True if the version changed."""
    state = load_state(root)
    good, current = state.get('last_good'), state.get('current')
    if not good or good == current or not exe_of(good, root).exists():
        return False
    state['current'] = good
    state.setdefault('failed', []).append(dict(version=current, at=int(time.time())))
    state['failed'] = state['failed'][-10:]
    save_state(state, root)
    return True


def ua_version(user_agent):
    """'7.40.0' from a Notion user agent ('... Notion/7.40.0 Chrome/...'), else ''."""
    match = re.search(r'Notion/([0-9][0-9.]*)', str(user_agent or ''))
    return match.group(1) if match else ''


def matches(version, user_agent):
    """True when a pinned version (7.40.0.0) is the build in the user agent (7.40.0)."""
    ua = ua_version(user_agent)
    return bool(ua) and (str(version) == ua or str(version).startswith(ua + '.'))


def main(argv=None):
    """python -m notiontool.notion_runtime status|pin|upgrade|rollback"""
    import sys
    args = list(sys.argv[1:] if argv is None else argv) or ['status']
    command = args[0]
    if command == 'pin':
        print(ensure())
    elif command == 'upgrade':
        print(upgrade() or 'No newer Notion installed; nothing to do')
    elif command == 'rollback':
        print('rolled back' if rollback() else 'nothing to roll back')
    print(json.dumps(status(run=subprocess.run), indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
