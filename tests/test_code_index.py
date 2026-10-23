"""Rank 8: per-worktree code index, nt_find_symbol and the ranked repo map in context_pack."""
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from notiontool import code_index as ci
from notiontool import context_pack as cp
from notiontool import devtools

REPO = Path(__file__).resolve().parents[1]


def write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def small_repo(root):
    write(root, 'pkg/__init__.py', '')
    write(root, 'pkg/core.py', 'def helper(x):\n    return x + 1\n\n\nclass Thing:\n    def run(self):\n        return helper(2)\n')
    write(root, 'pkg/a.py', 'from .core import helper\n\n\ndef use():\n    return helper(1)\n')
    write(root, 'pkg/b.py', 'from pkg import core\n\n\ndef other():\n    return core.helper(3)\n')
    write(root, 'main.py', 'import pkg.core\nfrom pkg.core import Thing\n\nThing().run()\n')
    write(root, 'data/ignored.py', 'def helper():\n    pass\n')  # data folder is never indexed


class TempRepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.db = os.path.join(self.root, '.index', 'idx.sqlite')
        small_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def check_helper(self, index):
        found = index.find_symbol('helper')
        self.assertEqual([h.split(': ')[0] for h in found['definitions']], ['pkg/core.py:1'])
        callers = '\n'.join(found['callers'])
        for expected in ('pkg/a.py:5: in use', 'pkg/b.py:5: in other', 'pkg/core.py:7: in Thing.run'):
            self.assertIn(expected, callers)
        self.assertIn('pkg/a.py:1: from pkg.core import helper', found['importers'])
        self.assertNotIn('data/', repr(found))
        return found

    def test_definitions_callers_importers_in_one_call(self):
        index = ci.CodeIndex(self.root, db=self.db)
        self.check_helper(index)
        method = index.find_symbol('Thing.run')
        self.assertEqual(len(method['definitions']), 1)
        self.assertIn('def Thing.run', method['definitions'][0])
        self.assertTrue(any(h.startswith('main.py:4:') for h in method['callers']))
        thing = index.find_symbol('Thing')
        self.assertIn('main.py:2: from pkg.core import Thing', thing['importers'])
        if ci.fts5_available(sqlite3.connect(':memory:')):
            self.assertEqual(index.mode, 'fts5')

    def test_like_fallback_gives_same_results(self):
        fts = ci.CodeIndex(self.root, db=self.db).find_symbol('helper')
        like_index = ci.CodeIndex(self.root, db=os.path.join(self.root, '.index', 'like.sqlite'), fts=False)
        like = like_index.find_symbol('helper')
        self.assertEqual(like_index.mode, 'like')
        for key in ('definitions', 'callers', 'importers'):
            self.assertEqual(fts[key], like[key])
        self.assertTrue(like_index.search('elp')['results'])

    def test_refresh_only_changed_files(self):
        index = ci.CodeIndex(self.root, db=self.db)
        first = index.refresh()
        self.assertEqual(first['changed'], first['files'])
        self.assertEqual(first['files'], 5)
        self.assertEqual(index.refresh()['changed'], 0)
        path = write(self.root, 'pkg/a.py', 'from .core import helper\n\n\ndef use():\n    return helper(1)\n\n\n'
                                        'def fresh():\n    return use()\n')
        later = time.time() + 5
        os.utime(path, (later, later))
        found = index.find_symbol('fresh')  # the refresh runs before every query
        self.assertEqual(index.last['changed'], 1)
        self.assertEqual(found['definitions'], ['pkg/a.py:8: def fresh (lines 8-9)'])
        self.assertEqual(index.find_symbol('use')['callers'], ['pkg/a.py:9: in fresh'])
        os.remove(os.path.join(self.root, 'pkg', 'b.py'))
        gone = index.find_symbol('other')
        self.assertEqual(index.last['removed'], 1)
        self.assertEqual(gone['definitions'], [])

    def test_repo_map_ranks_heavily_imported_module_first(self):
        write(self.root, 'hub.py', 'def shared():\n    return 1\n\n\ndef rare():\n    return 2\n')
        for number in range(6):
            write(self.root, 'user%d.py' % number, 'import hub\nfrom hub import shared\n\n\ndef f%d():\n'
                                                  '    return shared()\n' % number)
        ranked = ci.CodeIndex(self.root, db=self.db).repo_map()
        self.assertEqual(ranked['files'][0][0], 'hub.py')
        hub_symbols = [s['name'] for s in ranked['symbols'] if s['path'] == 'hub.py']
        self.assertEqual(hub_symbols[0], 'shared')
        focused = ci.CodeIndex(self.root, db=self.db).repo_map(focus=['user1.py'])
        self.assertEqual({s['path'] for s in focused['symbols']}, {'user1.py'})

    def test_pagerank_sums_to_one(self):
        rank = ci.pagerank(['a', 'b', 'c'], {('a', 'c'): 1.0, ('b', 'c'): 1.0})
        self.assertAlmostEqual(sum(rank.values()), 1.0, places=6)
        self.assertEqual(max(rank, key=rank.get), 'c')

    def test_syntax_error_file_is_skipped(self):
        write(self.root, 'broken.py', 'def (:\n')
        found = ci.CodeIndex(self.root, db=self.db).find_symbol('helper')
        self.assertEqual(len(found['definitions']), 1)


class ContextPackMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        parts = ['import os\n']
        for number in range(40):
            parts.append('\n\ndef func%d(value):\n    """Helper %d."""\n    total = value + %d\n    return total\n'
                         % (number, number, number))
        write(self.root, 'big.py', ''.join(parts))
        for number in range(5):
            write(self.root, 'caller%d.py' % number, 'from big import func7\n\n\ndef go():\n    return func7(%d)\n' % number)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ranked_symbols_fill_budget(self):
        prompt, info = cp.expand('Task', ['big.py'], self.root, budget=3000)
        self.assertTrue(info['added'])
        self.assertGreater(info['symbols'], 0)
        self.assertLessEqual(info['chars'], 3000)
        self.assertIn(cp.MAP_HEADER, prompt)
        first = prompt.index('File big.py lines')
        self.assertIn('def func7(value):', prompt[first:first + 200])  # most connected symbol first
        self.assertNotIn('map_error', info)

    def test_small_budget_is_respected(self):
        prompt, info = cp.expand('Task', ['big.py'], self.root, budget=150)
        self.assertEqual(info['symbols'], 0)
        self.assertLessEqual(info['chars'], 150)
        self.assertEqual(prompt, 'Task')

    def test_full_budget_shows_all_symbols_once(self):
        prompt, info = cp.expand('Task', ['big.py'], self.root)
        self.assertEqual(info['symbols'], 40)
        self.assertEqual(prompt.count('def func7(value):'), 1)
        self.assertLessEqual(info['chars'], cp.BUDGET_CHARS)


class ThisRepoTest(unittest.TestCase):
    def test_nt_find_symbol_estimate_tokens(self):
        found = devtools.call('nt_find_symbol', {'name': 'estimate_tokens', 'path': str(REPO)})
        definitions = '\n'.join(found['definitions'])
        callers = '\n'.join(found['callers'])
        importers = '\n'.join(found['importers'])
        self.assertIn('notiontool/context_archive.py:', definitions)
        self.assertTrue(all('def estimate_tokens' in hit for hit in found['definitions']))
        self.assertIn('notiontool/run_context.py:', callers)
        self.assertIn('notiontool/context_archive.py:', callers)
        self.assertIn('notiontool/chat_context.py:', importers)
        self.assertNotIn('work/', definitions + callers + importers)

    def test_index_file_is_per_worktree_and_never_the_main_store(self):
        path = Path(ci.db_path(REPO)).resolve()
        self.assertNotEqual(path.name.lower(), 'notiontool.db')
        self.assertNotIn(REPO / 'data', path.parents)
        self.assertNotEqual(ci.db_path(REPO), ci.db_path(REPO / 'tests'))
        with self.assertRaises(ValueError):
            ci.CodeIndex(REPO, db=str(REPO / 'data' / 'notiontool.db'))

    def test_tool_is_read_only_and_operator_visible(self):
        specs = {tool['name']: tool for tool in devtools.TOOLS}
        self.assertEqual(specs['nt_find_symbol']['annotations'], specs['nt_search_code']['annotations'])
        self.assertTrue(specs['nt_find_symbol']['annotations']['readOnlyHint'])
        from notiontool import bridge
        self.assertIn('nt_find_symbol', [tool['name'] for tool in bridge.OPERATOR_TOOLS])
        self.assertIn('nt_find_symbol', bridge.LIMIT_ALLOWED_TOOLS)
        self.assertIn('nt_find_symbol', bridge.DEV_GUARDED)


if __name__ == '__main__':
    unittest.main()
