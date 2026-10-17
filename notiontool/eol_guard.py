"""Line-ending and BOM guard for coworker file writes.

Pure helpers, standard library only. Nothing here is wired yet.

Wiring note (NOT WIRED): bridge.py write_file (:341-359) currently ends with
path.write_text(text, encoding='utf-8', newline='') at :358. The Main can
replace that call with:

    existing = path.read_bytes() if path.exists() else None
    data = eol_guard.normalize(text, existing)
    path.write_bytes(data)

and return len(data) as 'bytes' at :359. An edited file then keeps its
dominant line ending and BOM state, and a new file is written as LF
without a BOM.
"""

from __future__ import annotations

from pathlib import Path

BOM = b"\xef\xbb\xbf"
STYLES = ("lf", "crlf")


def _as_bytes(data) -> bytes:
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    raise TypeError("expected bytes, got " + type(data).__name__)


def _counts(data: bytes) -> tuple[int, int, int]:
    """Return (bare_lf, crlf, lone_cr) counts for raw bytes."""
    crlf = data.count(b"\r\n")
    bare_lf = data.count(b"\n") - crlf
    lone_cr = data.count(b"\r") - crlf
    return bare_lf, crlf, lone_cr


def _has_newline(data: bytes) -> bool:
    return b"\n" in data or b"\r" in data


def detect(data) -> str:
    """Return 'crlf', 'lf', 'mixed' or 'none' for raw bytes.

    A lone CR always counts toward 'mixed'. Empty input or input without
    any newline gives 'none'.
    """
    bare_lf, crlf, lone_cr = _counts(_as_bytes(data))
    if lone_cr:
        return "mixed"
    if crlf and bare_lf:
        return "mixed"
    if crlf:
        return "crlf"
    if bare_lf:
        return "lf"
    return "none"


def dominant(data) -> str:
    """Return the majority line ending, 'crlf' or 'lf'. Ties go to 'lf'."""
    bare_lf, crlf, _ = _counts(_as_bytes(data))
    return "crlf" if crlf > bare_lf else "lf"


def normalize(new_text, existing_bytes_or_None=None, default="lf") -> bytes:
    """Return UTF-8 bytes for new_text in the target line-ending style.

    Every CRLF and lone CR in new_text is first collapsed to LF, then LF is
    turned into the target style, so the result never contains a doubled
    CRLF or CR CR LF. The target style is the dominant ending of the
    existing bytes, or default when there is no existing file or it has no
    newline. A UTF-8 BOM is written only when the existing bytes start with
    one; a BOM at the start of new_text is always dropped first.
    """
    if not isinstance(new_text, str):
        raise TypeError("new_text must be str, got " + type(new_text).__name__)
    if default not in STYLES:
        raise ValueError("default must be 'lf' or 'crlf'")
    existing = None if existing_bytes_or_None is None else _as_bytes(existing_bytes_or_None)
    text = new_text[1:] if new_text.startswith("\ufeff") else new_text
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if existing is None or not _has_newline(existing):
        style = default
    else:
        style = dominant(existing)
    if style == "crlf":
        text = text.replace("\n", "\r\n")
    prefix = BOM if existing is not None and existing.startswith(BOM) else b""
    return prefix + text.encode("utf-8")


def report(path) -> dict:
    """Return line-ending diagnostics for a file.

    lf counts bare LF only, crlf counts CRLF pairs and cr counts lone CR.
    """
    p = Path(path)
    data = p.read_bytes()
    bare_lf, crlf, lone_cr = _counts(data)
    return {
        "path": str(p),
        "bytes": len(data),
        "lf": bare_lf,
        "crlf": crlf,
        "cr": lone_cr,
        "bom": data.startswith(BOM),
        "style": detect(data),
        "dominant": dominant(data),
    }
