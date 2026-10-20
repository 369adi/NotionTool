"""Secure per-account session store for NotionTool native API.

SessionStore: Windows DPAPI-encrypted cookie persistence, per-slot account isolation.
import_from_cdp: explicit one-time migration from a running CDP browser session.

Safety:
- Cookies never appear in stdout, logs, or the public bundle.
- All saves are atomic (write to .tmp, then os.replace).
- Non-Windows platforms use an injected protector (for testing).
"""
import json
import os
import time

__all__ = ["SessionStore"]

# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------

class _DPAPIProtector:
    """Windows DPAPI protect/unprotect using ctypes (stdlib only)."""

    def protect(self, plaintext: bytes) -> bytes:
        import ctypes
        import ctypes.wintypes
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        blob_in = DATA_BLOB(len(plaintext), ctypes.cast(ctypes.c_char_p(plaintext), ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
        if not ok:
            raise OSError("CryptProtectData failed")
        result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return result

    def unprotect(self, ciphertext: bytes) -> bytes:
        import ctypes
        import ctypes.wintypes
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        blob_in = DATA_BLOB(len(ciphertext), ctypes.cast(ctypes.c_char_p(ciphertext), ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
        if not ok:
            raise OSError("CryptUnprotectData failed")
        result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return result


def _default_protector():
    if os.name == "nt":
        return _DPAPIProtector()
    raise RuntimeError("No protector available on non-Windows; inject one for testing.")


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------

class SessionStore:
    """Encrypted per-slot cookie/session store.

    data_dir: directory for session files (default: %APPDATA%/NotionTool/sessions).
    protector: optional inject for testing (must have .protect(bytes)->bytes and .unprotect(bytes)->bytes).

    session_dict schema:
        cookies: list of {name, value, domain, path, secure, expires}
        user_agent: str (optional)
        client_version: str (optional)
        updated_at: float (unix timestamp, set automatically on save)
    """

    _MAX_AGE_S = 90 * 24 * 3600  # 90 days

    def __init__(self, data_dir=None, protector=None):
        if data_dir is None:
            data_dir = os.path.join(
                os.environ.get("APPDATA") or os.path.expanduser("~"),
                "NotionTool", "sessions"
            )
        self._dir = data_dir
        self._protector = protector  # None -> lazy-init on first use

    def _get_protector(self):
        if self._protector is None:
            self._protector = _default_protector()
        return self._protector

    def _path(self, key: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(key))
        return os.path.join(self._dir, f"session_{safe}.bin")

    def load(self, key: str) -> "dict | None":
        """Return decrypted session_dict or None (missing/corrupt/expired)."""
        path = self._path(key)
        try:
            with open(path, "rb") as fh:
                ciphertext = fh.read()
        except OSError:
            return None
        try:
            plaintext = self._get_protector().unprotect(ciphertext)
            d = json.loads(plaintext.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(d, dict):
            return None
        try:
            age = time.time() - float(d.get("updated_at") or 0)
        except (TypeError, ValueError):
            return None
        if key not in ('host_api','bridge') and (age < 0 or age > self._MAX_AGE_S):
            return None
        return d

    def load_identity(self,key):
        if key not in ('host_api','bridge'): raise ValueError('Not a persistent MCP identity slot')
        value=self.load(key)
        if value is None and os.path.exists(self._path(key)):
            raise ValueError('Existing MCP identity cannot be decrypted; refusing to silently rotate its Bearer key')
        field='key' if key=='host_api' else 'token'
        if value is not None and (not isinstance(value.get(field),str) or len(value[field])<16):
            raise ValueError('Existing MCP identity is invalid; explicit repair required, no automatic key rotation')
        return value

    def save(self, key: str, session_dict: dict) -> None:
        """Encrypt and atomically write session_dict."""
        import uuid as _uuid
        d = dict(session_dict)
        d["updated_at"] = time.time()
        plaintext = json.dumps(d, separators=(",", ":")).encode("utf-8")
        ciphertext = self._get_protector().protect(plaintext)
        os.makedirs(self._dir, exist_ok=True)
        # unique tmp name avoids concurrent-save races for the same key
        tmp = self._path(key) + ".tmp." + _uuid.uuid4().hex
        try:
            with open(tmp, "wb") as fh:
                fh.write(ciphertext)
            os.replace(tmp, self._path(key))
        except Exception:  # noqa: BLE001
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def delete(self, key: str) -> None:
        """Remove session file; silent if missing."""
        try:
            os.remove(self._path(key))
        except OSError:
            pass


# ---------------------------------------------------------------------------
