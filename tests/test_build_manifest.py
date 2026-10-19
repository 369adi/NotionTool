"""Static release-build manifest check (plan item P10).

work/restart-harness.ps1 -Rebuild runs PyInstaller with NotionTool.spec, which
analyses main.py. PyInstaller follows import statements, also those inside
functions, but it cannot see a module loaded through importlib.import_module
with a computed name, and it bundles only the non-Python files listed in
``datas``. This test repeats that analysis with ast (no build, the app is not
imported) and names every notiontool module or package data file the exe
would miss at runtime.
"""

import ast
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = 'NotionTool.spec'
PACKAGE = 'notiontool'
SKIP_PARTS = {'__pycache__'}
CODE_SUFFIXES = {'.py', '.pyc', '.pyo'}
IMPORT_FUNCS = {'import_module', '__import__'}

# Gaps measured on 2026-10-19 were fixed in NotionTool.spec the same night.
# A new gap makes the two full checks below fail and names it.
# Every known gap is imported only by modules that main.py does not reach yet
# (main_wake, run_context, delegation, role_split, work_queue, task_specs), so
# the exe works today but breaks as soon as one of them is wired in lazily.
KNOWN_MISSING_MODULES = set()
KNOWN_MISSING_DATA = set()


def _strings(node):
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def read_spec(root):
    """(entry scripts, hiddenimports, datas as (source, dest)) from the Analysis() call."""
    tree = ast.parse((root / SPEC).read_text(encoding='utf-8'))
    entries, hidden, datas = [], [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, 'id', getattr(node.func, 'attr', '')) != 'Analysis':
            continue
        if node.args:
            entries += [s.replace('\\', '/').strip('/') for s in _strings(node.args[0])]
        for keyword in node.keywords:
            if keyword.arg == 'hiddenimports':
                hidden += _strings(keyword.value)
            elif keyword.arg == 'datas':
                for item in getattr(keyword.value, 'elts', []):
                    if isinstance(item, ast.Tuple) and len(item.elts) == 2:
                        source, dest = _strings(item.elts[0]), _strings(item.elts[1])
                        if source and dest:
                            datas.append((source[-1].replace('\\', '/').strip('/'),
                                          dest[-1].replace('\\', '/').strip('/')))
    return entries, hidden, datas


def package_modules(root):
    """{dotted name: (path, is_package)} for every notiontool .py file."""
    modules = {}
    for path in sorted((root / PACKAGE).rglob('*.py')):
        rel = path.relative_to(root)
        if SKIP_PARTS & set(rel.parts):
            continue
        parts = list(rel.with_suffix('').parts)
        is_package = parts[-1] == '__init__'
        if is_package:
            parts = parts[:-1]
        modules['.'.join(parts)] = (path, is_package)
    return modules


def _expand(name, modules):
    parts = name.split('.')
    return {'.'.join(parts[:i]) for i in range(1, len(parts) + 1) if '.'.join(parts[:i]) in modules}


def _is_import_call(node):
    if not isinstance(node, ast.Call):
        return False
    return getattr(node.func, 'id', getattr(node.func, 'attr', '')) in IMPORT_FUNCS


def imports_of(path, module_name, is_package, modules):
    """(static, dynamic) notiontool modules imported anywhere in path, function bodies included."""
    tree = ast.parse(path.read_bytes(), filename=str(path))
    package = module_name.split('.') if is_package else module_name.split('.')[:-1]
    static, dynamic = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                static |= _expand(alias.name, modules)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - (node.level - 1)
                if keep < 1:
                    continue
                prefix = '.'.join(package[:keep] + (node.module.split('.') if node.module else []))
            else:
                prefix = node.module or ''
            static |= _expand(prefix, modules)
            for alias in node.names:
                static |= _expand(prefix + '.' + alias.name, modules)
    scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    texts = []
    for scope in scopes:
        if any(_is_import_call(n) for n in ast.walk(scope)):
            texts += _strings(scope)
    texts += [s for n in ast.walk(tree) if _is_import_call(n) for s in _strings(n)]
    for text in texts:
        short = text.lstrip('.')
        if short.startswith(PACKAGE + '.'):
            short = short[len(PACKAGE) + 1:]
        candidate = PACKAGE + '.' + short
        if short and candidate in modules:
            dynamic.add(candidate)
    return static, dynamic


def bundled_as(rel, datas):
    """Path the file gets inside the exe, or None when no datas entry covers it."""
    for source, dest in datas:
        if rel == source:
            return (dest + '/' if dest else '') + rel.rsplit('/', 1)[-1]
        if rel.startswith(source + '/'):
            return dest + rel[len(source):]
    return None


def analyse(root):
    entries, hidden, datas = read_spec(root)
    modules = package_modules(root)
    graph, importers = {}, {}
    for name, (path, is_package) in modules.items():
        static, dynamic = imports_of(path, name, is_package, modules)
        graph[name] = static
        for target in static | dynamic:
            importers.setdefault(target, set()).add(name)
    roots = set()
    for entry in entries:
        static, dynamic = imports_of(root / entry, '__main__', False, modules)
        roots |= static
        for target in static | dynamic:
            importers.setdefault(target, set()).add(entry)
    for name in hidden:
        roots |= _expand(name, modules)
    covered, stack = set(), list(roots)
    while stack:
        name = stack.pop()
        if name not in covered:
            covered.add(name)
            stack += graph.get(name, ())
    missing_modules = {name: sorted(importers[name]) for name in sorted(set(importers) - covered)}
    data = []
    for path in sorted((root / PACKAGE).rglob('*')):
        rel = path.relative_to(root)
        if path.is_file() and not SKIP_PARTS & set(rel.parts) and path.suffix not in CODE_SUFFIXES:
            data.append(rel.as_posix())
    missing_data = [rel for rel in data if bundled_as(rel, datas) != rel]
    return dict(entries=entries, hidden=hidden, datas=datas, covered=covered,
                missing_modules=missing_modules, missing_data=missing_data)


def served_web_files(root):
    """index.html plus its local script/style references and the notion_shell overlay assets."""
    web = PACKAGE + '/web/'
    html = (root / web / 'index.html').read_text(encoding='utf-8')
    files = {web + 'index.html'}
    files |= {web + ref.lstrip('./') for ref in re.findall(r'(?:src|href)="([^":?#]+)"', html)}
    shell = (root / PACKAGE / 'notion_shell.py').read_text(encoding='utf-8')
    files |= {web + 'notion_shell/' + name for name in re.findall(r"asset_dir\s*/\s*'([^']+)'", shell)}
    return sorted(files)


class BuildManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = analyse(ROOT)

    def test_spec_builds_main_with_datas(self):
        self.assertEqual(self.result['entries'], ['main.py'])
        self.assertTrue(self.result['datas'])
        self.assertIn('notiontool.server', self.result['covered'])

    def test_no_new_lazily_or_dynamically_imported_module_is_missing(self):
        new = {k: v for k, v in self.result['missing_modules'].items() if k not in KNOWN_MISSING_MODULES}
        self.assertEqual(new, {}, 'Not bundled by NotionTool.spec (module: importers); add them to hiddenimports')

    def test_no_new_package_data_file_is_missing(self):
        new = [rel for rel in self.result['missing_data'] if rel not in KNOWN_MISSING_DATA]
        self.assertEqual(new, [], 'Not bundled (or at the wrong path) by NotionTool.spec datas')

    def test_served_web_files_are_bundled(self):
        files = served_web_files(ROOT)
        self.assertIn('notiontool/web/app.js', files)
        self.assertIn('notiontool/web/notion_shell/overlay.js', files)
        for rel in files:
            self.assertTrue((ROOT / rel).is_file(), rel)
            self.assertEqual(bundled_as(rel, self.result['datas']), rel, rel)

    def test_known_gaps_are_still_listed(self):
        stale = (KNOWN_MISSING_MODULES - set(self.result['missing_modules'])) | (
            KNOWN_MISSING_DATA - set(self.result['missing_data']))
        self.assertEqual(stale, set(), 'Fixed gaps: remove them from the KNOWN_MISSING sets')


    def test_every_imported_module_is_bundled(self):
        self.assertEqual(self.result['missing_modules'], {})

    def test_every_package_data_file_is_bundled(self):
        self.assertEqual(self.result['missing_data'], [])

class DetectorTests(unittest.TestCase):
    def test_names_lazy_dynamic_and_data_gaps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pkg = root / PACKAGE
            (pkg / 'web').mkdir(parents=True)
            (root / SPEC).write_text(
                "a = Analysis([str(root / 'main.py')], datas=[(str(root / 'notiontool/web'), 'notiontool/web')],"
                " hiddenimports=['notiontool.extra'])\n", encoding='utf-8')
            (root / 'main.py').write_text('from notiontool.server import run\n', encoding='utf-8')
            (pkg / '__init__.py').write_text('', encoding='utf-8')
            (pkg / 'server.py').write_text(
                'import importlib\n\ndef run():\n    from . import lazy\n\n'
                'def plugin():\n    name = __package__ + ".dyn"\n    return importlib.import_module(name)\n',
                encoding='utf-8')
            for name in ('lazy', 'dyn', 'extra', 'orphan'):
                (pkg / (name + '.py')).write_text('', encoding='utf-8')
            (pkg / 'web' / 'app.js').write_text('', encoding='utf-8')
            (pkg / 'rules.md').write_text('', encoding='utf-8')
            result = analyse(root)
        self.assertIn('notiontool.lazy', result['covered'])
        self.assertIn('notiontool.extra', result['covered'])
        self.assertEqual(result['missing_modules'], {'notiontool.dyn': ['notiontool.server']})
        self.assertEqual(result['missing_data'], ['notiontool/rules.md'])


if __name__ == '__main__':
    unittest.main()