"""Repository line-ending guard.

Every text source file under notiontool/, tests/, docs/ and bench/ must be
either pure CRLF or pure LF. A mixed file is always the result of a bad edit.
A fixed list of files that are pure CRLF today must stay pure CRLF.
Uses notiontool.eol_guard.detect for the classification. Reads bytes only.
"""

import tempfile
import time
import unittest
from pathlib import Path

from notiontool.eol_guard import _counts, detect

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("notiontool", "tests", "docs", "bench")
SUFFIXES = {".py", ".md", ".js", ".css", ".ps1"}
SKIP_PARTS = {"__pycache__", ".test-data"}

# Measured on 2026-10-18: these files are pure CRLF and must stay CRLF.
KNOWN_CRLF = (
    "notiontool/chat_context.py",
    "notiontool/devtools.py",
    "notiontool/mains.py",
    "notiontool/mcp_servers.py",
    "notiontool/notion.py",
    "notiontool/perfection.py",
    "notiontool/server.py",
    "notiontool/vendor/sessions.py",
    "notiontool/vendor/transport.py",
    "notiontool/verifier.py",
    "tests/test_devtools.py",
    "tests/test_harness_context.py",
    "tests/test_harness_multi_main.py",
    "tests/test_verifier.py",
    "docs/HANDOFF.md",
)


def iter_text_files(root, dirs=SCAN_DIRS):
    """Yield (relative posix path, bytes) for scanned text files."""
    root = Path(root)
    for top in dirs:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix.lower() not in SUFFIXES or not path.is_file():
                continue
            if SKIP_PARTS.intersection(path.relative_to(root).parts):
                continue
            data = path.read_bytes()
            if b"\x00" in data:  # binary file
                continue
            yield path.relative_to(root).as_posix(), data


def mixed_files(root, dirs=SCAN_DIRS):
    """Return 'path (lf=.., crlf=.., cr=..)' for every mixed file."""
    found = []
    for rel, data in iter_text_files(root, dirs):
        if detect(data) == "mixed":
            lf, crlf, cr = _counts(data)
            found.append("%s (lf=%d, crlf=%d, cr=%d)" % (rel, lf, crlf, cr))
    return found


def not_crlf(root, names=KNOWN_CRLF):
    """Return 'path: style' for every listed file that is not pure CRLF."""
    problems = []
    for rel in names:
        path = Path(root) / rel
        if not path.is_file():
            problems.append("%s: missing" % rel)
            continue
        style = detect(path.read_bytes())
        if style != "crlf":
            problems.append("%s: %s" % (rel, style))
    return problems


class RepoEolTest(unittest.TestCase):
    def test_no_mixed_line_endings(self):
        started = time.monotonic()
        found = mixed_files(ROOT)
        self.assertEqual(found, [], "mixed CRLF/LF files: " + "; ".join(found))
        self.assertLess(time.monotonic() - started, 5.0)

    def test_known_crlf_files_stay_crlf(self):
        problems = not_crlf(ROOT)
        self.assertEqual(problems, [], "expected pure CRLF: " + "; ".join(problems))

    def test_scan_sees_files(self):
        names = {rel for rel, _ in iter_text_files(ROOT)}
        self.assertIn("tests/test_repo_eol.py", names)
        self.assertIn("notiontool/eol_guard.py", names)


class GuardSelfTest(unittest.TestCase):
    """Prove the guard fails on bad trees, using a temp folder only."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "notiontool" / "__pycache__").mkdir(parents=True)
        (self.root / "tests").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rel, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_mixed_file_is_reported_with_counts(self):
        self.write("notiontool/a.py", b"x = 1\r\ny = 2\nz = 3\r\n")
        self.write("tests/ok_lf.py", b"a\nb\n")
        self.write("tests/ok_crlf.md", b"a\r\nb\r\n")
        self.assertEqual(mixed_files(self.root), ["notiontool/a.py (lf=1, crlf=2, cr=0)"])

    def test_lone_cr_counts_as_mixed(self):
        self.write("tests/b.js", b"a\rb\n")
        self.assertEqual(len(mixed_files(self.root)), 1)

    def test_skipped_and_binary_and_other_suffixes(self):
        self.write("notiontool/__pycache__/c.py", b"a\r\nb\n")
        self.write("tests/.test-data/d.md", b"a\r\nb\n")
        self.write("tests/e.py", b"\x00a\r\nb\n")
        self.write("tests/f.txt", b"a\r\nb\n")
        self.assertEqual(mixed_files(self.root), [])

    def test_known_crlf_change_is_reported(self):
        self.write("notiontool/k1.py", b"a\r\nb\r\n")
        self.write("notiontool/k2.py", b"a\nb\n")
        self.write("notiontool/k3.py", b"a\r\nb\n")
        names = ("notiontool/k1.py", "notiontool/k2.py", "notiontool/k3.py", "notiontool/k4.py")
        self.assertEqual(not_crlf(self.root, names), [
            "notiontool/k2.py: lf",
            "notiontool/k3.py: mixed",
            "notiontool/k4.py: missing",
        ])


if __name__ == "__main__":
    unittest.main()
