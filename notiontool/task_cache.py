"""Task result cache keyed by prompt, context manifest and file hashes.

Uses only the public Store API (get, put, delete). No network, no file I/O
of its own: file hashing takes bytes or a reader. Not wired into engine.py
yet (NOT WIRED).
"""
import hashlib
import json

KIND = "task_cache"
KEY_VERSION = 1


def sha256_text(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def hash_file(source, chunk_size=65536):
    """sha256 hex digest of bytes or of a binary reader with read()."""
    digest = hashlib.sha256()
    if isinstance(source, (bytes, bytearray, memoryview)):
        digest.update(bytes(source))
        return digest.hexdigest()
    read = getattr(source, "read", None)
    if not callable(read):
        raise TypeError("hash_file expects bytes or a reader with read()")
    while True:
        chunk = read(chunk_size)
        if not chunk:
            break
        if isinstance(chunk, str):
            raise TypeError("hash_file expects a binary reader")
        digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def normalize_path(path):
    """Forward slashes only, so a/b and a\\b are the same file."""
    text = str(path).replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def normalize_file_hashes(files):
    """Return {normalized_path: hash} sorted by path.

    files is a mapping path -> hash or an iterable of (path, hash) pairs.
    Two spellings of one path with different hashes raise ValueError.
    """
    if files is None:
        return {}
    pairs = files.items() if isinstance(files, dict) else files
    result = {}
    for path, digest in pairs:
        key = normalize_path(path)
        value = None if digest is None else str(digest)
        if key in result and result[key] != value:
            raise ValueError("conflicting hashes for %s" % key)
        result[key] = value
    return dict(sorted(result.items()))


def cache_key(prompt, context_manifest, files):
    """sha256 over the prompt hash, the canonical manifest hash and the file hashes."""
    parts = {
        "v": KEY_VERSION,
        "prompt": sha256_text(prompt or ""),
        "manifest": sha256_text(canonical_json(context_manifest or {})),
        "files": sha256_text(canonical_json(sorted(normalize_file_hashes(files).items()))),
    }
    return sha256_text(canonical_json(parts))


def store_result(store, key, result, file_hashes):
    doc = {"id": str(key), "result": result, "file_hashes": normalize_file_hashes(file_hashes)}
    store.put(KIND, doc)
    return doc


def invalidate(store, key):
    """Remove the entry. Returns True when an entry existed."""
    existed = store.get(KIND, str(key)) is not None
    store.delete(KIND, str(key))
    return existed


def lookup(store, key, current_file_hashes):
    """Return the stored result only when every stored file hash still matches.

    A changed or missing file invalidates the entry and returns None.
    """
    doc = store.get(KIND, str(key))
    if doc is None:
        return None
    current = normalize_file_hashes(current_file_hashes)
    for path, digest in (doc.get("file_hashes") or {}).items():
        if path not in current or current[path] != digest:
            invalidate(store, key)
            return None
    return doc.get("result")
