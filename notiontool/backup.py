"""Automatic backups and the read-only usage view (Settings > Backups / Details).

Backups
- backup_now(data_dir, store) writes data/backups/<YYYYmmdd-HHMMSS>/ with a consistent copy of
  notiontool.db (sqlite3 backup API, never a raw file copy), desktop_app.json when it exists and
  settings.json (store.settings()). A backup.json manifest marks a finished backup; the folder is
  built under a .partial- name and renamed at the end, so a crash never leaves a half backup.
- Only the newest KEEP (10) backups are kept.
- before_restart(app) runs from App.close() (every restart and update goes through it).
- daily_loop(...) makes one backup per DAY (checked at start and every CHECK seconds).

Usage (usage_report) reads the events, objects and counters tables through a separate read-only
SQLite connection (mode=ro). It never writes and never sends anything (I11).
"""
import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

KEEP = 10
DAY = 24 * 3600
CHECK = 600
FOLDER = 'backups'
DB_FILE = 'notiontool.db'
APP_FILE = 'desktop_app.json'
SETTINGS_FILE = 'settings.json'
MANIFEST = 'backup.json'
PARTIAL = '.partial-'
USAGE_DAYS = 7

_LOCK = threading.Lock()


def backup_root(data_dir):
    return Path(data_dir) / FOLDER


def _manifest(folder):
    try:
        data = json.loads((folder / MANIFEST).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_backups(data_dir):
    """Finished backups, newest first."""
    root = backup_root(data_dir)
    if not root.is_dir():
        return []
    found = []
    for folder in root.iterdir():
        if not folder.is_dir() or folder.name.startswith(PARTIAL):
            continue
        info = _manifest(folder)
        if info is not None:
            found.append(dict(info, name=folder.name, path=str(folder)))
    found.sort(key=lambda b: (float(b.get('time') or 0), b['name']), reverse=True)
    return found


def last_backup(data_dir):
    backups = list_backups(data_dir)
    return backups[0] if backups else None


def status(data_dir):
    last = last_backup(data_dir)
    return dict(last=last, count=len(list_backups(data_dir)), keep=KEEP,
                folder=str(backup_root(data_dir)),
                last_text=_when(last['time']) if last else 'No backup yet')


def _when(ts):
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(float(ts)))


def prune(data_dir, keep=KEEP):
    removed = []
    for info in list_backups(data_dir)[max(1, int(keep)):]:
        shutil.rmtree(info['path'], ignore_errors=True)
        removed.append(info['name'])
    root = backup_root(data_dir)
    if root.is_dir():
        for folder in root.iterdir():
            # Leftovers of an interrupted backup.
            if folder.name.startswith(PARTIAL) and time.time() - folder.stat().st_mtime > 3600:
                shutil.rmtree(folder, ignore_errors=True)
    return removed


def _copy_db(store, source, target):
    dst = sqlite3.connect(str(target))
    try:
        db = getattr(store, 'db', None) if store is not None else None
        if db is not None:
            lock = getattr(store, 'lock', None) or threading.Lock()
            with lock:
                db.backup(dst)
            return True
        if not source.is_file():
            return False
        src = sqlite3.connect('file:' + source.resolve().as_posix() + '?mode=ro', uri=True)
        try:
            src.backup(dst)
        finally:
            src.close()
        return True
    finally:
        dst.close()


def backup_now(data_dir, store=None, reason='manual', now=None, keep=KEEP):
    """Make one backup now and keep the newest `keep`. Returns the manifest."""
    data_dir = Path(data_dir)
    with _LOCK:
        now = time.time() if now is None else float(now)
        root = backup_root(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        base = time.strftime('%Y%m%d-%H%M%S', time.localtime(now))
        name, n = base, 1
        while (root / name).exists():
            n += 1
            name = f'{base}-{n}'
        work = root / (PARTIAL + name)
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        files = []
        try:
            if _copy_db(store, data_dir / DB_FILE, work / DB_FILE):
                files.append(DB_FILE)
            if (data_dir / APP_FILE).is_file():
                shutil.copy2(data_dir / APP_FILE, work / APP_FILE)
                files.append(APP_FILE)
            if store is not None and hasattr(store, 'settings'):
                (work / SETTINGS_FILE).write_text(json.dumps(store.settings(), indent=2, ensure_ascii=False),
                                                  encoding='utf-8')
                files.append(SETTINGS_FILE)
            info = dict(time=now, reason=str(reason or 'manual'), files=files)
            (work / MANIFEST).write_text(json.dumps(info, indent=2), encoding='utf-8')
            os.replace(work, root / name)
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise
        prune(data_dir, keep)
    info = dict(info, name=name, path=str(root / name), time_text=_when(now))
    if store is not None and hasattr(store, 'event'):
        try:
            store.event('backup_done', message='Backup made: ' + name, reason=info['reason'], backup=name)
        except Exception:
            pass
    return info


def maybe_daily(data_dir, store=None, now=None):
    """Back up when the newest backup is older than a day (or there is none)."""
    now = time.time() if now is None else float(now)
    last = last_backup(data_dir)
    if last and now - float(last.get('time') or 0) < DAY:
        return None
    return backup_now(data_dir, store, reason='daily', now=now)


def daily_loop(data_dir, store, stop_event, interval=CHECK, first=60):
    """Thread body: one backup per day while the app runs."""
    wait = first
    while not stop_event.wait(wait):
        wait = interval
        try:
            maybe_daily(data_dir, store)
        except Exception as exc:
            try:
                store.event('backup_failed', message='Daily backup failed: ' + str(exc)[:200])
            except Exception:
                return  # the store is closed: the app is shutting down


def before_restart(app):
    """Backup before the app stops or restarts; never blocks the stop on errors."""
    store = getattr(app, 'store', None)
    data_dir = getattr(app, 'data_dir', None) or getattr(store, 'directory', None)
    if not data_dir:
        return None
    try:
        return backup_now(data_dir, store, reason='restart')
    except Exception as exc:
        try:
            store.event('backup_failed', message='Backup before restart failed: ' + str(exc)[:200])
        except Exception:
            pass
        return None


# ---- usage (read-only) -------------------------------------------------------------
def _readonly(store):
    path = Path(getattr(store, 'directory', '.')) / DB_FILE
    return sqlite3.connect('file:' + path.resolve().as_posix() + '?mode=ro', uri=True)


def _budget():
    try:
        from .network_budget import SHARED_BUDGET
        return SHARED_BUDGET.status()
    except Exception:
        return {}


def _short(worker_id):
    text = str(worker_id or '')
    return text.split(':')[-1][:8] if text else 'Main chat'


def usage_report(store, budget=None, now=None, days=USAGE_DAYS):
    """Requests per minute vs budget, turns per model per day, tasks per coworker, average task time."""
    now = time.time() if now is None else float(now)
    budget = _budget() if budget is None else (budget or {})
    since = now - max(1, int(days)) * DAY
    db = _readonly(store)
    try:
        q = db.execute
        last_minute = q("SELECT COUNT(*) FROM events WHERE kind='remote_sent' AND ts>?", (now - 60,)).fetchone()[0]
        models = {}
        for (doc,) in q("SELECT doc FROM objects WHERE kind='tasks'"):
            try:
                task = json.loads(doc)
            except ValueError:
                continue
            model = task.get('model')
            if isinstance(model, dict):
                model = model.get('name') or model.get('id')
            models[task.get('id')] = str(model or 'Default')
        turns = {}
        for ts, task_id in q("SELECT ts, json_extract(doc,'$.task_id') FROM events WHERE kind='remote_sent' AND ts>?",
                             (since,)):
            key = (time.strftime('%Y-%m-%d', time.localtime(ts)), models.get(task_id, 'Default'))
            turns[key] = turns.get(key, 0) + 1
        labels = {}
        for (doc,) in q("SELECT doc FROM objects WHERE kind='workers'"):
            try:
                w = json.loads(doc)
            except ValueError:
                continue
            labels[w.get('id')] = str(w.get('label') or w.get('title') or '')
        coworkers, durations, done_durations = {}, [], []
        for worker_id, task_id, state, duration in q(
                "SELECT json_extract(doc,'$.worker_id'), json_extract(doc,'$.task_id'), json_extract(doc,'$.state'), "
                "json_extract(doc,'$.duration') FROM events WHERE kind='task_attempt_finished' AND ts>?", (since,)):
            row = coworkers.setdefault(worker_id or '', dict(tasks=set(), done=0))
            row['tasks'].add(task_id)
            if state == 'done':
                row['done'] += 1
            if isinstance(duration, (int, float)) and duration >= 0:
                durations.append(float(duration))
                if state == 'done':
                    done_durations.append(float(duration))
        counters = dict(q('SELECT name, value FROM counters').fetchall())
    finally:
        db.close()
    timed = done_durations or durations
    limit = int(budget.get('limit_per_minute') or 0)
    window = budget.get('window_count')
    return dict(
        requests=dict(last_minute=int(window if isinstance(window, int) else last_minute),
                      sent_last_minute=int(last_minute), limit_per_minute=limit,
                      text=f"{int(window if isinstance(window, int) else last_minute)} of {limit or 'no limit'} per minute"),
        turns=[dict(day=d, model=m, turns=n) for (d, m), n in sorted(turns.items(), key=lambda i: (i[0][0], i[0][1]),
                                                                   reverse=True)],
        coworkers=sorted([dict(worker_id=w, name=labels.get(w) or _short(w), tasks=len(r['tasks']), done=r['done'])
                          for w, r in coworkers.items()], key=lambda r: (-r['tasks'], r['name'])),
        average_task_seconds=round(sum(timed) / len(timed), 1) if timed else None,
        timed_tasks=len(timed), days=int(days), counters=counters)


def desk_handlers(server):
    """Overlay actions for desktop_shell.py: backup_status, backup_now, usage_report."""
    def data_dir():
        return getattr(server, 'data_dir', None) or server.store.directory
    return {'backup_status': lambda args: status(data_dir()),
            'backup_now': lambda args: backup_now(data_dir(), server.store, reason='manual'),
            'usage_report': lambda args: usage_report(server.store)}
