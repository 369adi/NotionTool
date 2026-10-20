"""Model-free acceptance gate for one executor part (pure module, NOT WIRED).

check_part(files, test_modules, allowed_files, root, timeout_s=50, expected_eol=None)
runs only objective checks, without any model:

    scope         every file in files is inside allowed_files (normalize_path from
                  task_cache.py, compared case-insensitively)
    exists        the file is present under root (only reported when it is missing)
    bom           no UTF-8 BOM
    line_endings  the expected line ending per file, counted from the raw bytes:
                  'lf' by default, overridable per path with expected_eol
                  {path: 'lf' or 'crlf'}
    py_compile    every .py file compiles; bytecode goes to a temp folder, never into root
    unittest      the listed modules in one subprocess (sys.executable -m unittest),
                  cwd root, PYTHONPATH root and root/tests joined by os.pathsep, and a
                  hard timeout of timeout_s with a process-tree kill; skipped when no
                  modules are given

Result: {ok, checks: [{name, ok, detail, path}], failures}.
failures is a short English list with one line per failed check. Each line starts
with the path (or the module list for unittest), so the list can be used directly
as a fix prompt (see fix_prompt). ok is True only when every check passed.

The reviewer role is only needed when ok is True. When ok is False, the part goes
back to a fix round with fix_prompt(result) and no model review is spent on it.

Wiring (NOT WIRED): engine.py _complete would call check_part for executor results
with the task's claimed files as files and allowed_files and its test modules.
"""
import os
import py_compile
import re
import signal
import subprocess
import sys
import tempfile

from notiontool.task_cache import normalize_path

BOM = b"\xef\xbb\xbf"
EOL_STYLES = ("lf", "crlf")
DEFAULT_EOL = "lf"
MAX_FAILED_TESTS = 3
KILL_WAIT_S = 15

_RAN = re.compile(r"^(Ran \d+ tests?) in ", re.M)
_RESULT = re.compile(r"^(?:OK|FAILED|NO TESTS RAN)\b.*$", re.M)
_FAILED_TEST = re.compile(r"^(?:FAIL|ERROR): .*$", re.M)


def _check(checks, name, ok, detail, path):
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail), "path": path})


def _seconds(value):
    return "%g" % float(value)


def _rel(path, root):
    """Project-relative normalized path; absolute paths outside root stay absolute."""
    text = str(path)
    if os.path.isabs(text):
        try:
            relative = os.path.relpath(text, root)
        except ValueError:
            relative = text
        if not relative.startswith(".."):
            text = relative
    return normalize_path(text)


def _expected(expected_eol, rel):
    for key, value in (expected_eol or {}).items():
        if normalize_path(key).lower() == rel.lower():
            style = str(value).strip().lower()
            if style not in EOL_STYLES:
                raise ValueError("expected_eol for %s must be 'lf' or 'crlf'" % rel)
            return style
    return DEFAULT_EOL


def eol_counts(data):
    """(crlf, lone lf, lone cr) counted from raw bytes."""
    crlf = data.count(b"\r\n")
    return crlf, data.count(b"\n") - crlf, data.count(b"\r") - crlf


def _check_eol(data, style):
    crlf, lf, cr = eol_counts(data)
    ok = cr == 0 and (crlf == 0 if style == "lf" else lf == 0)
    return ok, "expected %s, found %d CRLF, %d LF, %d CR" % (style.upper(), crlf, lf, cr)


def _compile(full, workdir):
    try:
        py_compile.compile(full, cfile=os.path.join(workdir, "check.pyc"), doraise=True)
    except py_compile.PyCompileError as error:
        inner = error.exc_value
        line = getattr(inner, "lineno", None)
        message = getattr(inner, "msg", None) or str(inner)
        if line:
            return False, "line %s: %s" % (line, message)
        lines = str(message).strip().splitlines()
        return False, lines[-1] if lines else "does not compile"
    return True, "compiles"


def _kill_tree(proc):
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=KILL_WAIT_S)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_tests(test_modules, root, timeout_s=50):
    """Run unittest modules in a subprocess. Returns (ok, detail, output)."""
    modules = [str(module) for module in test_modules]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(root), os.path.join(str(root), "tests")])
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, "-m", "unittest"] + modules, cwd=str(root), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, **kwargs)
    try:
        raw, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=KILL_WAIT_S)
        except (OSError, subprocess.SubprocessError):
            pass
        return False, "timed out after %s s" % _seconds(timeout_s), ""
    output = (raw or b"").decode("utf-8", errors="replace").replace("\r\n", "\n")
    ran = _RAN.search(output)
    results = _RESULT.findall(output)
    result = results[-1].strip() if results else ""
    parts = []
    if ran:
        parts.append(ran.group(1))
    if result:
        parts.append(result)
    ok = proc.returncode == 0 and bool(ran) and result.startswith("OK")
    if not ok:
        if not ran:
            lines = [line for line in output.splitlines() if line.strip()]
            parts.append("exit code %s, no 'Ran N tests' line" % proc.returncode)
            if lines:
                parts.append(lines[-1].strip()[:200])
        parts.extend(_FAILED_TEST.findall(output)[:MAX_FAILED_TESTS])
    return ok, "; ".join(parts), output


def check_part(files, test_modules, allowed_files, root, timeout_s=50, expected_eol=None):
    """Run every objective check for one part. See the module docstring."""
    root = os.path.abspath(str(root))
    checks = []
    allowed = {normalize_path(path).lower() for path in (allowed_files or [])}
    paths, seen = [], set()
    for item in files or []:
        rel = _rel(item, root)
        if rel.lower() not in seen:
            seen.add(rel.lower())
            paths.append(rel)
    with tempfile.TemporaryDirectory(prefix="auto_accept_") as workdir:
        for rel in paths:
            inside = rel.lower() in allowed
            _check(checks, "scope", inside, "inside allowed_files" if inside else "not in allowed_files", rel)
            full = rel if os.path.isabs(rel) else os.path.join(root, rel)
            if not os.path.isfile(full):
                _check(checks, "exists", False, "file not found under root", rel)
                continue
            with open(full, "rb") as handle:
                data = handle.read()
            has_bom = data.startswith(BOM)
            _check(checks, "bom", not has_bom, "UTF-8 BOM found, remove it" if has_bom else "no BOM", rel)
            ok, detail = _check_eol(data, _expected(expected_eol, rel))
            _check(checks, "line_endings", ok, detail, rel)
            if rel.lower().endswith(".py"):
                ok, detail = _compile(full, workdir)
                _check(checks, "py_compile", ok, detail, rel)
    modules = [str(module) for module in (test_modules or []) if str(module).strip()]
    if modules:
        ok, detail, _ = run_tests(modules, root, timeout_s)
        _check(checks, "unittest", ok, detail, " ".join(modules))
    if not checks:
        _check(checks, "inputs", False, "no files and no test modules given", "(part)")
    failures = ["%s: %s: %s" % (c["path"], c["name"], c["detail"]) for c in checks if not c["ok"]]
    return {"ok": not failures, "checks": checks, "failures": failures}


def fix_prompt(result):
    """Short English text for the next step: a fix round or the reviewer."""
    failures = list((result or {}).get("failures") or [])
    if (result or {}).get("ok") and not failures:
        return "All automatic checks passed. Hand the part to the reviewer."
    if not failures:
        failures = ["(part): result: no checks were reported"]
    lines = ["The automatic acceptance gate failed. Fix these problems, then run the checks again:"]
    lines.extend("- " + item for item in failures)
    return "\n".join(lines)
