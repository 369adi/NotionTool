"""File snapshots for one task: hashes plus content-addressed, deduplicated copies.

Standard library only. Layout under <root>/<store_dir> (default work/snapshots):
  objects/<sha256>/content            one stored copy per distinct content
  snapshots/<task_id>.json            the snapshot dict of one task (a new take replaces it)
  backups/<task_id>/<time_ns>/<path>  the current file, saved by restore() before it overwrites
Paths are relative to root and use task_cache.normalize_path, the same as
snapshot_diff.py, so compare() results feed snapshot_diff.summary() and diff()
uses snapshot_diff.unified_diff(). Not wired into engine.py yet (NOT WIRED).
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import time

from notiontool import snapshot_diff
from notiontool.task_cache import normalize_path

DEFAULT_STORE_DIR = 'work/snapshots'
OBJECTS = 'objects'
SNAPSHOTS = 'snapshots'
BACKUPS = 'backups'
CONTENT_NAME = 'content'
CHUNK = 1024 * 1024
FORMAT_VERSION = 1
MAX_TASK_ID = 120
_UNSAFE_ID = re.compile(r'[^A-Za-z0-9._-]')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_clock = time.time_ns  # replaced in tests


def _root(root):
    return os.path.abspath(os.fspath(root if root is not None else os.getcwd()))


def _store_path(root, store_dir):
    store = os.fspath(store_dir if store_dir is not None else DEFAULT_STORE_DIR)
    if os.path.isabs(store):
        return os.path.abspath(store)
    return os.path.abspath(os.path.join(root, store))


def safe_task_id(task_id):
    """File-name-safe task id; ValueError when nothing usable is left."""
    text = _UNSAFE_ID.sub('_', str(task_id if task_id is not None else '').strip()).strip('.')
    if not text:
        raise ValueError('task_id must not be empty')
    return text[:MAX_TASK_ID]


def relative_path(path, root):
    """Normalized path relative to root; ValueError when it points outside root."""
    root = _root(root)
    raw = os.fspath(path)
    full = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(root, raw))
    try:
        rel = os.path.relpath(full, root)
    except ValueError:
        raise ValueError('path is outside the root: %s' % raw) from None
    rel = normalize_path(rel)
    if rel in ('.', '..') or rel.startswith('../'):
        raise ValueError('path is outside the root: %s' % raw)
    return rel


def _full(root, rel):
    return os.path.join(root, *rel.split('/'))


def _snapshot_file(store, task_id):
    return os.path.join(store, SNAPSHOTS, safe_task_id(task_id) + '.json')


def object_path(store, sha256):
    """Path of the stored copy for one sha256."""
    if not _SHA.match(str(sha256)):
        raise ValueError('invalid sha256: %r' % (sha256,))
    return os.path.join(store, OBJECTS, sha256, CONTENT_NAME)


def _unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _hash_file(full):
    digest = hashlib.sha256()
    size = 0
    with open(full, 'rb') as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _write_json(path, data):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write('\n')
        os.replace(tmp, path)
    except BaseException:
        _unlink(tmp)
        raise


def _store_object(store, full):
    """Copy one file into the object store while hashing it; return (sha256, size).

    The copy is hashed from the same bytes that are stored, and a content that
    is already stored is not written again.
    """
    folder = os.path.join(store, OBJECTS)
    os.makedirs(folder, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'wb') as out, open(full, 'rb') as source:
            for chunk in iter(lambda: source.read(CHUNK), b''):
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
        sha = digest.hexdigest()
        target = object_path(store, sha)
        if os.path.isfile(target):
            _unlink(tmp)
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(tmp, target)
        return sha, size
    except BaseException:
        _unlink(tmp)
        raise


def take(task_id, paths, root, store_dir=DEFAULT_STORE_DIR):
    """Record sha256, size and mtime of each file and store a deduplicated copy.

    Returns the snapshot dict {version, task_id, root, store_dir, created_ns,
    files: {path: {sha256, size, mtime, mtime_ns}}, missing: [path]} and saves it
    as snapshots/<task_id>.json. Paths that are not files go to missing.
    A path outside root raises ValueError. store_dir is relative to root unless absolute.
    """
    safe_task_id(task_id)
    root = _root(root)
    store = _store_path(root, store_dir)
    files, missing = {}, []
    for path in paths or []:
        rel = relative_path(path, root)
        if rel in files or rel in missing:
            continue
        full = _full(root, rel)
        if not os.path.isfile(full):
            missing.append(rel)
            continue
        stat = os.stat(full)
        sha, size = _store_object(store, full)
        files[rel] = dict(sha256=sha, size=size, mtime=stat.st_mtime, mtime_ns=stat.st_mtime_ns)
    snapshot = dict(
        version=FORMAT_VERSION,
        task_id=str(task_id),
        root=root,
        store_dir=store,
        created_ns=int(_clock()),
        files=dict(sorted(files.items())),
        missing=sorted(missing),
    )
    _write_json(_snapshot_file(store, task_id), snapshot)
    return snapshot


def load(task_id, root=None, store_dir=DEFAULT_STORE_DIR):
    """Saved snapshot dict of a task; KeyError when there is none."""
    path = _snapshot_file(_store_path(_root(root), store_dir), task_id)
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise KeyError('no snapshot for task %s' % task_id) from None


def _current_sha(root, rel):
    full = _full(root, rel)
    if not os.path.isfile(full):
        return None
    return _hash_file(full)[0]


def compare(before, after_paths):
    """Compare a snapshot with the files on disk now.

    Checks every file of the snapshot plus after_paths and returns
    {changed, added, removed, unchanged}, each a sorted list of normalized paths.
    """
    root = before['root']
    files = before.get('files') or {}
    paths = set(files)
    for path in after_paths or []:
        paths.add(relative_path(path, root))
    result = {key: [] for key in snapshot_diff.CATEGORIES}
    for rel in sorted(paths):
        entry = files.get(rel)
        old_sha = entry.get('sha256') if entry else None
        new_sha = _current_sha(root, rel)
        if old_sha is None and new_sha is None:
            continue
        if old_sha is None:
            result['added'].append(rel)
        elif new_sha is None:
            result['removed'].append(rel)
        elif old_sha == new_sha:
            result['unchanged'].append(rel)
        else:
            result['changed'].append(rel)
    return result


def stored_bytes(snapshot, path):
    """Bytes saved for path in the snapshot, or None when the path is not in it."""
    rel = relative_path(path, snapshot['root'])
    entry = (snapshot.get('files') or {}).get(rel)
    if entry is None:
        return None
    with open(object_path(snapshot['store_dir'], entry['sha256']), 'rb') as handle:
        return handle.read()


def diff(snapshot, path, max_lines=snapshot_diff.DEFAULT_MAX_LINES):
    """snapshot_diff.unified_diff of the saved copy against the file on disk now."""
    rel = relative_path(path, snapshot['root'])
    full = _full(snapshot['root'], rel)
    current = None
    if os.path.isfile(full):
        with open(full, 'rb') as handle:
            current = handle.read()
    return snapshot_diff.unified_diff(stored_bytes(snapshot, rel), current, rel, max_lines=max_lines)


def _claim_key(path):
    text = normalize_path(str(path).strip()).rstrip('/')
    return text.casefold() if os.name == 'nt' else text


def outside_claim(changed, claimed_files):
    """Changed files that are not covered by the claim, as sorted normalized paths.

    changed is a list of paths or a compare() result (changed, added and removed
    count). A claimed entry covers the same path or, as a folder, every path below it.
    """
    if isinstance(changed, dict):
        items = [path for key in ('changed', 'added', 'removed') for path in (changed.get(key) or [])]
    else:
        items = list(changed or [])
    claims = {_claim_key(item) for item in (claimed_files or []) if str(item).strip()}
    claims.discard('')
    outside = set()
    for path in items:
        rel = normalize_path(path)
        key = _claim_key(rel)
        if key in claims or any(key.startswith(claim + '/') for claim in claims):
            continue
        outside.add(rel)
    return sorted(outside)


def restore(task_id, path, root=None, store_dir=DEFAULT_STORE_DIR):
    """Back up the current file, then put the saved copy from the task's snapshot back.

    Returns {path, sha256, backup}; backup is None when the file did not exist.
    KeyError when the task or path is not in a snapshot, ValueError when the saved
    copy is missing or damaged (the current file is left as it is then).
    """
    snapshot = load(task_id, root, store_dir)
    rel = relative_path(path, snapshot['root'])
    entry = (snapshot.get('files') or {}).get(rel)
    if entry is None:
        raise KeyError('%s is not in the snapshot of task %s' % (rel, task_id))
    store = snapshot['store_dir']
    source = object_path(store, entry['sha256'])
    if not os.path.isfile(source) or _hash_file(source)[0] != entry['sha256']:
        raise ValueError('the saved copy of %s is missing or damaged' % rel)
    target = _full(snapshot['root'], rel)
    backup = None
    if os.path.isfile(target):
        backup = os.path.join(store, BACKUPS, safe_task_id(task_id), str(int(_clock())), *rel.split('/'))
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        shutil.copy2(target, backup)
    folder = os.path.dirname(target)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.nt-restore-')
    os.close(fd)
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)
    except BaseException:
        _unlink(tmp)
        raise
    mtime_ns = entry.get('mtime_ns')
    if isinstance(mtime_ns, int):
        try:
            os.utime(target, ns=(mtime_ns, mtime_ns))
        except OSError:
            pass
    return dict(path=rel, sha256=entry['sha256'], backup=backup)


def _saved_snapshots(store):
    """[(created_ns, file_name, path, data)] sorted oldest first; unreadable files count as oldest."""
    folder = os.path.join(store, SNAPSHOTS)
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    items = []
    for name in names:
        if name.startswith('.') or not name.endswith('.json'):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            data = {}
        created = data.get('created_ns')
        if isinstance(created, bool) or not isinstance(created, int):
            created = 0
        items.append((created, name, path, data))
    items.sort(key=lambda item: (item[0], item[1]))
    return items


def _shas(data):
    shas = set()
    for entry in (data.get('files') or {}).values():
        if isinstance(entry, dict) and _SHA.match(str(entry.get('sha256'))):
            shas.add(entry['sha256'])
    return shas


def _object_sizes(store):
    folder = os.path.join(store, OBJECTS)
    sizes = {}
    if not os.path.isdir(folder):
        return sizes
    for name in os.listdir(folder):
        content = os.path.join(folder, name, CONTENT_NAME)
        if _SHA.match(name) and os.path.isfile(content):
            sizes[name] = os.path.getsize(content)
    return sizes


def prune(keep_last=200, max_mb=500, root=None, store_dir=DEFAULT_STORE_DIR):
    """Delete the oldest snapshots, then every stored content no snapshot uses anymore.

    Keeps at most keep_last snapshots, then drops further oldest snapshots while
    the stored contents they use are larger than max_mb. Backups are not touched.
    Returns {removed_snapshots, kept_snapshots, removed_objects, bytes_kept}.
    """
    store = _store_path(_root(root), store_dir)
    items = _saved_snapshots(store)
    keep_last = max(0, int(keep_last))
    limit = max(0.0, float(max_mb)) * 1024 * 1024
    sizes = _object_sizes(store)

    def used(rest):
        return sum(sizes.get(sha, 0) for sha in set().union(*(_shas(item[3]) for item in rest)))

    removed = []
    while len(items) > keep_last:
        removed.append(items.pop(0))
    while items and used(items) > limit:
        removed.append(items.pop(0))
    for item in removed:
        _unlink(item[2])
    referenced = set().union(*(_shas(item[3]) for item in items))
    removed_objects = 0
    folder = os.path.join(store, OBJECTS)
    for name in (os.listdir(folder) if os.path.isdir(folder) else []):
        if _SHA.match(name) and name not in referenced:
            shutil.rmtree(os.path.join(folder, name), ignore_errors=True)
            removed_objects += 1
    return dict(
        removed_snapshots=[item[1][:-len('.json')] for item in removed],
        kept_snapshots=len(items),
        removed_objects=removed_objects,
        bytes_kept=used(items),
    )
