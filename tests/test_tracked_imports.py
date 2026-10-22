"""Every module the app imports must be in git (live bug: the released EXE failed with
'No module named notiontool.token_budget' because token_budget.py was never committed).

The release workflow builds from a clean checkout, so an untracked module only breaks new users."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GENERATED = {'notiontool/_build.py'}  # written by the release workflow
FROM_REL = re.compile(r'^\s*from\s+(\.+)([\w.]*)\s+import\s+(.+)$')
FROM_ABS = re.compile(r'^\s*from\s+notiontool((?:\.\w+)*)\s+import\s+(.+)$')
IMPORT_ABS = re.compile(r'^\s*import\s+notiontool\.([\w.]+)')


def tracked():
    out = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return set(out.split())


def names(text):
    text = text.split('#')[0].strip().strip('()').strip('\\').strip()
    return [n.strip().split(' as ')[0].strip() for n in text.split(',') if n.strip()]


def module_file(parts):
    """Repo-relative file for a module path, or None when it is no module on disk (e.g. a function)."""
    base = '/'.join(parts)
    for candidate in (base + '.py', base + '/__init__.py'):
        if (ROOT / candidate).exists():
            return candidate
    return None


def imported_modules(path, rel):
    package = rel.split('/')[:-1]
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        m = FROM_REL.match(line)
        if m:
            dots, mod, what = m.groups()
            base = package[:len(package) - (len(dots) - 1)] if len(dots) > 1 else package
            parts = base + [p for p in mod.split('.') if p]
            yield parts
            for name in names(what):
                yield parts + [name]
            continue
        m = FROM_ABS.match(line)
        if m:
            parts = ['notiontool'] + [p for p in m.group(1).split('.') if p]
            yield parts
            for name in names(m.group(2)):
                yield parts + [name]
            continue
        m = IMPORT_ABS.match(line)
        if m:
            yield ['notiontool'] + m.group(1).split('.')


class TrackedImportsTest(unittest.TestCase):
    def test_every_imported_app_module_is_committed(self):
        files = tracked()
        sources = sorted(f for f in files if f.endswith('.py') and (f.startswith('notiontool/') or f == 'main.py'))
        self.assertIn('notiontool/server.py', sources)
        missing = set()
        for rel in sources:
            for parts in imported_modules(ROOT / rel, rel):
                found = module_file(parts)
                if found and found not in files and found not in GENERATED:
                    missing.add('%s (imported by %s)' % (found, rel))
        self.assertEqual(sorted(missing), [], 'Commit these modules, or the released EXE fails for new users')


if __name__ == '__main__':
    unittest.main()
