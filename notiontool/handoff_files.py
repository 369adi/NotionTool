"""File-based context handoff between an old and a new chat.

The handoff is stored as one JSON file per worker on the local PC instead of
inside a chat. The new chat reads the file with the NotionTool read_file tool
(bridge.py TOOLS) and confirms with a short random ack token. The file is
deleted only after the ack arrived and the first task finished.

Standard library only; no network access.
"""

import hashlib
import json
import os
import re
import secrets
import tempfile

SCHEMA_VERSION = 1
READ_TOOL = "read_file"
CONTEXT_TOOL = "context"
MAX_FIRST_MESSAGE = 600
REDACTED = "[REDACTED]"

_SAFE_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}")
_SECRET_KEYS = frozenset({"token_v2", "api_key", "apikey", "x-api-key", "x_api_key", "authorization"})
_PATTERNS = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"), r"\1 " + REDACTED),
    (re.compile(r"(?i)(token_v2[\"']?\s*[=:]\s*[\"']?)[^\s\"';,&]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)((?:x-api-key|api[_-]?key)[\"']?\s*[=:]\s*[\"']?)[^\s\"';,&]+"), r"\1" + REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
)


class UnknownWorkerError(LookupError):
    """Raised when no handoff file exists for a worker."""


def safe_worker_id(worker_id):
    """Return worker_id when it is safe as a file name, otherwise raise ValueError."""
    if not isinstance(worker_id, str) or not worker_id:
        raise ValueError("Worker id must be a non-empty string.")
    if not _SAFE_ID.fullmatch(worker_id) or ".." in worker_id:
        raise ValueError("Unsafe worker id %r: use letters, digits, dot, dash and underscore, "
                         "no leading dot, at most 64 characters." % worker_id)
    return worker_id


def file_key(worker_id):
    """A safe file name for any harness worker id (real ids look like '<account>:<thread>').

    Safe ids are kept as they are; others become their safe characters plus a short hash,
    so two different ids never share a file and the name stays within 64 characters."""
    if not isinstance(worker_id, str) or not worker_id:
        raise ValueError("Worker id must be a non-empty string.")
    if _SAFE_ID.fullmatch(worker_id) and ".." not in worker_id:
        return worker_id
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", worker_id) or "w"
    digest = hashlib.sha256(worker_id.encode("utf-8")).hexdigest()[:12]
    return safe_worker_id(clean[-51:] + "_" + digest)


def handoff_path(base_dir, worker_id):
    """Path of the handoff file; it must stay inside base_dir."""
    name = safe_worker_id(worker_id) + ".json"
    base = os.path.realpath(base_dir)
    path = os.path.realpath(os.path.join(base, name))
    if os.path.dirname(path) != base:
        raise ValueError("Handoff path for worker %r leaves the base folder." % worker_id)
    return path


def redact_text(text):
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value, key=None):
    """Copy of value with secrets in every string replaced; keys stay unchanged."""
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        if isinstance(key, str) and key.lower() in _SECRET_KEYS and value:
            return REDACTED
        return redact_text(value)
    return value


def _read(path):
    """Record at path, None when missing; raises ValueError on broken JSON."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Broken handoff file %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ValueError("Broken handoff file %s: not a JSON object" % path)
    return data


def _atomic_write(path, record):
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _summary(path, record):
    return {"path": path, "token": record.get("token"), "created": record.get("created")}


def write_handoff(base_dir, worker_id, payload, now):
    """Write the handoff file atomically and return dict(path, token, created).

    While an un-acked file exists for the worker, it is returned unchanged:
    same token, no rewrite, so a crash and re-call never sends a second token.
    """
    path = handoff_path(base_dir, worker_id)
    try:
        existing = _read(path)
    except ValueError:
        existing = None  # broken file: replace it
    if existing is not None and existing.get("acked_at") is None:
        return _summary(path, existing)
    payload = dict(payload or {})
    record = redact(payload)
    record.update(
        schema_version=SCHEMA_VERSION,
        token=secrets.token_hex(4),
        created=now,
        old_thread=record.get("old_thread"),
        new_thread=record.get("new_thread"),
        acked_at=None,
        first_task_done_at=None,
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write(path, record)
    return _summary(path, record)


def first_message(record):
    """Short English prompt (under 600 characters) for the new chat."""
    path, token = record.get("path"), record.get("token")
    if not path or not token:
        raise ValueError("The record needs a path and a token.")
    text = ("Context handoff: the context of your previous chat is stored in a local file. "
            "Read it now with the NotionTool %s tool (the %s tool does not read files): %s\n"
            "Then reply with this ack token alone on its own line:\n%s\n"
            "After the token, wait for the user; at the first user message follow resume_instructions from the file.\n"
            "Do not start other work before you send the token." % (READ_TOOL, CONTEXT_TOOL, path, token))
    if len(text) >= MAX_FIRST_MESSAGE:
        raise ValueError("Handoff path is too long for the first message.")
    return text


def verify_ack(answer_text, token):
    """True only when token appears as a whole word bounded by non-alphanumerics."""
    if not token or not isinstance(token, str) or not isinstance(answer_text, str):
        return False
    pattern = r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])"
    return re.search(pattern, answer_text) is not None


def _update(base_dir, worker_id, field, now):
    path = handoff_path(base_dir, worker_id)
    record = _read(path)
    if record is None:
        raise UnknownWorkerError("No handoff file for worker %r." % worker_id)
    if record.get(field) is None:
        record[field] = now
    if record.get("acked_at") is not None and record.get("first_task_done_at") is not None:
        os.remove(path)
        return None
    _atomic_write(path, record)
    return record


def mark_acked(base_dir, worker_id, now):
    """Set acked_at; deletes the file (returns None) when the first task is already done."""
    return _update(base_dir, worker_id, "acked_at", now)


def mark_first_task_done(base_dir, worker_id, now):
    """Set first_task_done_at; deletes the file (returns None) only when acked_at is set."""
    return _update(base_dir, worker_id, "first_task_done_at", now)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def gc(base_dir, now, max_age_s=86400):
    """Delete finished or expired handoff files and old temp files; return deleted paths."""
    deleted = []
    try:
        names = sorted(os.listdir(base_dir))
    except FileNotFoundError:
        return deleted
    for name in names:
        full = os.path.join(base_dir, name)
        if not os.path.isfile(full):
            continue
        if name.startswith(".") and name.endswith(".tmp"):
            try:
                if now - os.path.getmtime(full) > max_age_s:
                    os.remove(full)
                    deleted.append(full)
            except OSError:
                pass
            continue
        if not name.endswith(".json"):
            continue
        try:
            record = _read(full)
        except ValueError:
            continue
        if record is None:
            continue
        finished = record.get("acked_at") is not None and record.get("first_task_done_at") is not None
        created = record.get("created")
        expired = _number(created) and now - created > max_age_s
        if finished or expired:
            try:
                os.remove(full)
                deleted.append(full)
            except FileNotFoundError:
                pass
    return deleted
