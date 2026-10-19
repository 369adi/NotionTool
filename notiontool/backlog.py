"""Persistent batch backlog stored as JSON files in work/queue/.

Standard library only. Each item is one file <dir>/<id>.json. Every write goes
to a temp file in the same folder and is then moved into place with
os.replace, so a reader never sees half a file. Legacy <dir>/*.txt files are
read as queued goals with the default priority and are never changed.

Item contract (shared with notiontool/autopilot.py):
{id, goal, priority, deps, approvals, request_id, state, submitted_run_id,
 created, submitted_at}
"""
import json
import os
import re
import tempfile
import time
import uuid

DEFAULT_DIR = os.path.join("work", "queue")
STATES = ("queued", "submitted", "done", "failed")
DEFAULT_PRIORITY = 0
# engine.py:156 keeps at most 100 characters of a request_id.
MAX_REQUEST_ID = 100
_ID_RE = re.compile(r'^[^\\/:*?"<>|\x00-\x1f]+$')


class BacklogError(ValueError):
    """Raised for an invalid item, an unknown item id or an unknown state."""


def _check_id(item_id):
    item_id = str(item_id or "").strip()
    if not item_id or item_id.startswith(".") or not _ID_RE.match(item_id):
        raise BacklogError("Invalid backlog item id: %r" % item_id)
    return item_id


def _check_state(state):
    if state not in STATES:
        raise BacklogError("Unknown backlog state %r; use one of %s" % (state, ", ".join(STATES)))
    return state


def _str_list(value, field, slashes=False):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise BacklogError("%s must be a list" % field)
    out = []
    for entry in value:
        text = str(entry).strip()
        if slashes:
            text = text.replace("\\", "/")
        if text and text not in out:
            out.append(text)
    return out


def normalize_item(item, now=None):
    """Return a new dict with every contract field filled in and checked."""
    if not isinstance(item, dict):
        raise BacklogError("A backlog item must be a JSON object")
    goal = str(item.get("goal") or "").strip()
    if not goal:
        raise BacklogError("A backlog item needs a non-empty goal")
    item_id = _check_id(item.get("id") or "b" + uuid.uuid4().hex[:12])
    try:
        priority = int(item.get("priority") or DEFAULT_PRIORITY)
        created = item.get("created")
        created = float(time.time() if now is None else now) if created is None else float(created)
        submitted_at = item.get("submitted_at")
        submitted_at = None if submitted_at is None else float(submitted_at)
    except (TypeError, ValueError):
        raise BacklogError("priority, created and submitted_at must be numbers")
    run_id = item.get("submitted_run_id")
    return {
        "id": item_id,
        "goal": goal,
        "priority": priority,
        "deps": _str_list(item.get("deps"), "deps"),
        "approvals": _str_list(item.get("approvals"), "approvals", slashes=True),
        "request_id": str(item.get("request_id") or "backlog:" + item_id)[:MAX_REQUEST_ID],
        "state": _check_state(item.get("state") or "queued"),
        "submitted_run_id": str(run_id) if run_id else None,
        "created": created,
        "submitted_at": submitted_at,
    }


def _json_path(dir, item_id):
    return os.path.join(dir, item_id + ".json")


def _write_atomic(path, item):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    data = (json.dumps(item, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".backlog-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_json(path):
    with open(path, "r", encoding="utf-8-sig") as handle:
        raw = json.load(handle)
    item = normalize_item(raw, now=os.path.getmtime(path))
    stem = os.path.splitext(os.path.basename(path))[0]
    if item["id"] != stem:
        raise BacklogError("id %r does not match the file name" % item["id"])
    return item


def _read_legacy(path):
    name = os.path.basename(path)
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        goal = handle.read().strip()
    return normalize_item({
        "id": os.path.splitext(name)[0],
        "goal": goal,
        "priority": DEFAULT_PRIORITY,
        "request_id": "legacy:" + name,
        "created": os.path.getmtime(path),
    })


def load(dir=DEFAULT_DIR, errors=None):
    """Return all backlog items sorted by created, then id.

    A <id>.json file wins over a legacy <id>.txt file with the same id.
    Broken files are skipped; when errors is a list, (file name, message)
    pairs are appended to it.
    """
    if not os.path.isdir(dir):
        return []
    items = {}
    legacy = []
    for name in sorted(os.listdir(dir)):
        path = os.path.join(dir, name)
        if name.startswith(".") or not os.path.isfile(path):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext == ".txt":
            legacy.append(path)
        elif ext == ".json":
            try:
                item = _read_json(path)
                items[item["id"]] = item
            except (OSError, ValueError) as exc:
                if errors is not None:
                    errors.append((name, "skipped broken backlog file: %s" % exc))
    for path in legacy:
        name = os.path.basename(path)
        if os.path.splitext(name)[0] in items:
            continue
        try:
            item = _read_legacy(path)
            items[item["id"]] = item
        except (OSError, ValueError) as exc:
            if errors is not None:
                errors.append((name, "skipped legacy goal: %s" % exc))
    return sorted(items.values(), key=lambda i: (i["created"], i["id"]))


def add(item, dir=DEFAULT_DIR, now=None):
    """Store a new item and return it.

    When an item with the same request_id already exists, nothing is written
    and the existing item is returned.
    """
    new = normalize_item(item, now=now)
    existing = load(dir)
    for old in existing:
        if old["request_id"] == new["request_id"]:
            return old
    path = _json_path(dir, new["id"])
    if any(old["id"] == new["id"] for old in existing) or os.path.exists(path):
        raise BacklogError("Backlog item id %r already exists" % new["id"])
    _write_atomic(path, new)
    return new


def done_ids(items):
    """Ids of all items in state done."""
    return {i["id"] for i in items or () if i.get("state") == "done"}


def next_ready(items, done_ids):
    """Queued item with every dep in done_ids: highest priority, then oldest, then smallest id."""
    done = {str(d) for d in (done_ids or ())}
    ready = [i for i in items or ()
             if i.get("state", "queued") == "queued"
             and all(str(d) in done for d in i.get("deps") or ())]
    if not ready:
        return None
    return min(ready, key=lambda i: (-int(i.get("priority") or 0), float(i.get("created") or 0), str(i.get("id"))))


def _find(dir, item_id):
    path = _json_path(dir, item_id)
    if os.path.isfile(path):
        return _read_json(path)
    legacy = os.path.join(dir, item_id + ".txt")
    if os.path.isfile(legacy):
        return _read_legacy(legacy)
    raise BacklogError("Unknown backlog item %r" % item_id)


def mark(item_id, state, run_id=None, dir=DEFAULT_DIR, now=None):
    """Set the state of an item and return the updated item.

    submitted needs a run_id and sets submitted_run_id and submitted_at.
    A legacy .txt goal gets a <id>.json file; the .txt file is left as it is.
    """
    item_id = _check_id(item_id)
    _check_state(state)
    item = dict(_find(dir, item_id), state=state)
    if run_id is not None:
        item["submitted_run_id"] = str(run_id)
    if state == "submitted":
        if not item.get("submitted_run_id"):
            raise BacklogError("State submitted needs a run_id")
        item["submitted_at"] = float(time.time() if now is None else now)
    _write_atomic(_json_path(dir, item_id), item)
    return item
