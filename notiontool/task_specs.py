"""Standard rules and task spec files for short coworker prompts (pure module, NOT wired yet).

- load_rules(version) reads notiontool/standard_rules_<version>.md and returns
  (text, short_hash) where short_hash is the first 12 hex chars of sha256.
- compose_prompt keeps short tasks inline (header, task, rules once). Long tasks
  are saved to a spec file (task, then the rules text once) and the prompt only
  carries a one-sentence summary, the path line and the header with the hash.
- Spec files live in <root>/data/task_specs/<run_id>/<task_id>.md. Reading never
  deletes them: retries, re-queues to another coworker and reviewers need them.
- cleanup_specs archives finished runs to data/task_specs/_archive and deletes
  archives older than keep_s. Nothing outside data/task_specs is touched.

Stdlib only, no network.
"""
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path, PureWindowsPath

RULES_DIR = Path(__file__).resolve().parent
RULES_FILES = {'v1': 'standard_rules_v1.md'}
SPEC_DIR_PARTS = ('data', 'task_specs')
ARCHIVE_DIR = '_archive'
SUMMARY_MAX_CHARS = 300
HASH_CHARS = 12

_SENTENCE_RE = re.compile(r'^(.+?[.!?])(?=\s|$)', re.S)

__all__ = [
    'ARCHIVE_DIR', 'SUMMARY_MAX_CHARS', 'check_spec_path', 'cleanup_specs', 'compose_prompt', 'first_sentence',
    'load_rules', 'read_spec', 'rules_header', 'save_spec', 'spec_file_path', 'spec_text', 'specs_dir',
]


def load_rules(version='v1'):
    """Return (rules_text, short_hash). Unknown versions raise ValueError."""
    name = RULES_FILES.get(version)
    if name is None:
        raise ValueError('Unknown standard rules version: %r.' % (version,))
    data = (RULES_DIR / name).read_bytes()
    return data.decode('utf-8'), hashlib.sha256(data).hexdigest()[:HASH_CHARS]


def rules_header(version='v1'):
    """The header line, for example 'Standard rules v1 apply (hash 0123456789ab).'."""
    return 'Standard rules %s apply (hash %s).' % (version, load_rules(version)[1])


def specs_dir(root):
    """<root>/data/task_specs as an absolute path."""
    return Path(root).resolve().joinpath(*SPEC_DIR_PARTS)


def _check_id(value, label):
    """Reject ids that could leave data/task_specs or that Windows would rewrite."""
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError('%s must be a non-empty string.' % label)
    if value != value.strip() or value.endswith('.') or '..' in value:
        raise ValueError('%s %r is not allowed (dots or spaces at the edges).' % (label, value))
    if '/' in value or '\\' in value or '\x00' in value:
        raise ValueError('%s %r must not contain a path separator.' % (label, value))
    if ':' in value or PureWindowsPath(value).drive or os.path.isabs(value):
        raise ValueError('%s %r must not be an absolute path or drive.' % (label, value))
    return value


def _inside(path, base):
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return path.resolve() != base.resolve()


def spec_file_path(root, run_id, task_id):
    """Return <root>/data/task_specs/<run_id>/<task_id>.md; invalid ids raise ValueError."""
    _check_id(run_id, 'run_id')
    _check_id(task_id, 'task_id')
    if run_id == ARCHIVE_DIR:
        raise ValueError('run_id %r is reserved.' % (run_id,))
    base = specs_dir(root)
    path = base / run_id / (task_id + '.md')
    if not _inside(path, base):
        raise ValueError('Spec path %s is outside %s.' % (path, base))
    return path


def _nearest_specs_base(resolved):
    """The closest folder data/task_specs above resolved, or None."""
    for folder in resolved.parents:
        if folder.name.lower() == SPEC_DIR_PARTS[1] and folder.parent.name.lower() == SPEC_DIR_PARTS[0]:
            return folder
    return None


def check_spec_path(spec_path, root=None):
    """Validate a spec file path before anything is written; return it resolved.

    The path must not contain '..', must end with .md and must resolve to
    data/task_specs/<run_id>/... outside _archive. With root set it must lie
    under specs_dir(root); without root a data/task_specs folder above the file
    is enough. Anything else raises ValueError.
    """
    if spec_path is None or not os.fspath(spec_path):
        raise ValueError('spec_path must be a non-empty path.')
    raw = Path(spec_path)
    if '..' in raw.parts or '..' in PureWindowsPath(os.fspath(spec_path)).parts:
        raise ValueError('spec_path %s must not contain "..".' % (spec_path,))
    resolved = raw.resolve()
    if resolved.suffix.lower() != '.md':
        raise ValueError('spec_path %s must end with .md.' % (spec_path,))
    if root is not None:
        base = specs_dir(root)
        if not _inside(resolved, base):
            raise ValueError('spec_path %s is outside %s.' % (resolved, base))
    else:
        base = _nearest_specs_base(resolved)
        if base is None:
            raise ValueError('spec_path %s is not inside a data/task_specs folder.' % (resolved,))
    relative = resolved.relative_to(base)
    if len(relative.parts) < 2:
        raise ValueError('spec_path %s must be inside a run folder of %s.' % (resolved, base))
    if any(part.lower() == ARCHIVE_DIR for part in relative.parts[:-1]):
        raise ValueError('spec_path %s must not be inside %s.' % (resolved, ARCHIVE_DIR))
    return resolved


def _atomic_write(path, text):
    """Write text as UTF-8 via a temp file in the same folder plus os.replace.

    When the file already holds exactly these bytes nothing is written.
    """
    path = Path(path)
    data = str(text).encode('utf-8')
    if path.is_file() and path.read_bytes() == data:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return path


def save_spec(root, run_id, task_id, text):
    """Save a task spec atomically and idempotently; return its path."""
    return _atomic_write(spec_file_path(root, run_id, task_id), text)


def read_spec(root, run_id, task_id):
    """Return the saved spec text. The file is never deleted on read."""
    return spec_file_path(root, run_id, task_id).read_text(encoding='utf-8')


def spec_text(task_text, rules_version='v1'):
    """Spec file content for a long task: the task, then the rules text exactly once."""
    rules, _ = load_rules(rules_version)
    return str(task_text or '').rstrip() + '\n\n' + rules.strip() + '\n'


def first_sentence(text, limit=SUMMARY_MAX_CHARS):
    """First sentence of text with whitespace collapsed, at most limit characters."""
    flat = ' '.join(str(text or '').split())
    match = _SENTENCE_RE.match(flat)
    sentence = match.group(1) if match else flat
    if len(sentence) > limit:
        sentence = sentence[:max(0, limit - 3)].rstrip() + '...'
    return sentence[:limit]


def compose_prompt(task_text, rules_version='v1', spec_path=None, max_inline_chars=1500, root=None):
    """Build the coworker prompt for one task.

    Short task (at most max_inline_chars): header, task text, rules text once.
    Long task: spec_path is checked first (check_spec_path, with root if given),
    then the task plus the rules text once is saved atomically there, and the
    prompt is the first sentence (max 300 chars), 'Full details: read <path>'
    and the header. A long task without a valid spec_path raises ValueError and
    nothing is written.
    """
    if isinstance(max_inline_chars, bool) or not isinstance(max_inline_chars, int) or max_inline_chars < 0:
        raise ValueError('max_inline_chars must be a non-negative int.')
    rules, short_hash = load_rules(rules_version)
    header = 'Standard rules %s apply (hash %s).' % (rules_version, short_hash)
    task = str(task_text or '')
    if len(task) <= max_inline_chars:
        return header + '\n\n' + task.strip() + '\n\n' + rules.strip() + '\n'
    if spec_path is None:
        raise ValueError('A task longer than %d characters needs spec_path.' % max_inline_chars)
    path = check_spec_path(spec_path, root)
    _atomic_write(path, spec_text(task, rules_version))
    return first_sentence(task) + '\n' + 'Full details: read ' + os.fspath(path) + '\n' + header + '\n'


def _free_name(folder, name, now):
    target = folder / name
    if not target.exists():
        return target
    stamp = '%s.%d' % (name, int(now))
    target, counter = folder / stamp, 1
    while target.exists():
        target = folder / ('%s.%d' % (stamp, counter))
        counter += 1
    return target


def cleanup_specs(root, finished_run_ids, now, keep_s=86400):
    """Archive spec folders of finished runs, then delete old archives.

    Returns {'archived': [archive names], 'deleted': [archive names],
    'skipped': [{'run_id', 'reason'}]}. Invalid ids are skipped and reported;
    runs without a spec folder are ignored. An archived folder gets mtime=now,
    so its age counts from archiving.
    """
    if isinstance(keep_s, bool) or not isinstance(keep_s, (int, float)) or keep_s < 0:
        raise ValueError('keep_s must be a non-negative number.')
    base = specs_dir(root)
    archive = base / ARCHIVE_DIR
    result = dict(archived=[], deleted=[], skipped=[])
    for run_id in finished_run_ids or ():
        try:
            _check_id(run_id, 'run_id')
            if run_id == ARCHIVE_DIR:
                raise ValueError('run_id %r is reserved.' % (run_id,))
        except ValueError as error:
            result['skipped'].append(dict(run_id=run_id, reason=str(error)))
            continue
        source = base / run_id
        if source.is_symlink() or not source.is_dir() or not _inside(source, base):
            continue
        archive.mkdir(parents=True, exist_ok=True)
        target = _free_name(archive, run_id, now)
        os.replace(source, target)
        os.utime(target, (now, now))
        result['archived'].append(target.name)
    if archive.is_dir() and not archive.is_symlink():
        limit = now - keep_s
        for entry in sorted(archive.iterdir()):
            if entry.is_symlink() or not _inside(entry, archive):
                continue
            if entry.stat().st_mtime >= limit:
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            result['deleted'].append(entry.name)
    return result
