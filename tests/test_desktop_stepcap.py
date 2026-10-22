"""Backtest of the desktop 'stepcap' part with a fake Notion ThreadStore (runs the real JS in node)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from notiontool import desktop_shell

JS = Path(desktop_shell.__file__).resolve().parent / 'web' / 'desktop' / 'stepcap.js'

HARNESS = r'''
var window = globalThis; globalThis.window = window;
function Store(i, type) { this.i = i; this.type = type; }
Store.prototype.getStep = function () { return { type: this.type }; };
function ThreadStore(id, n, cfgAt) { this.id = id; this.n = n; this.cfgAt = cfgAt; }
ThreadStore.keyName = 'ThreadStore';
ThreadStore.prototype.getMessageStores = function () {
  var out = [];
  for (var i = 0; i < this.n; i++) out.push(new Store(i, i === this.cfgAt ? 'config' : 'user'));
  return out;
};
var MODULES = { 174148: { E: ThreadStore } };
if (process.argv[3] === 'wrongmodule') MODULES = { 174148: { E: function () {} } };
window.rspackChunknotion_next = { push: function (c) { c[2](function (id) { if (!(id in MODULES)) throw new Error('no'); return MODULES[id]; }); } };
window.setTimeout = function () {};
require(process.argv[2]);
var r = {};
r.state = window.__ntStepCapState;
function ids(t) { return t.getMessageStores().map(function (s) { return s.i; }); }
var long = new ThreadStore('a', 100, 0);
r.long = ids(long);
r.short = ids(new ThreadStore('b', 12, 0));
r.noCfg = ids(new ThreadStore('c', 50, -1)).length;
r.cfgInTail = ids(new ThreadStore('d', 50, 45)).length;
window.__ntStepCap = { a: 0 };
r.override0 = ids(long).length;
window.__ntStepCap = { a: 10 };
r.override10 = ids(long);
window.__ntStepCap = {};
window.__ntStepCapOff = true;
r.off = ids(long).length;
window.__ntStepCapOff = false;
var first = ThreadStore.prototype.getMessageStores;
require('module')._cache = {}; delete require.cache[require.resolve(process.argv[2])];
require(process.argv[2]);
r.idempotent = ThreadStore.prototype.getMessageStores === first;
var cur = ThreadStore.prototype.getMessageStores;
r.unwrapped = !!cur.__orig && cur.__orig.__nt === undefined;
console.log(JSON.stringify(r));
'''


@unittest.skipUnless(shutil.which('node'), 'node not available')
class StepCapTest(unittest.TestCase):
    def run_js(self, mode='ok'):
        harness = Path(__file__).resolve().parent / '_stepcap_harness.cjs'
        harness.write_text(HARNESS, encoding='utf-8')
        try:
            out = subprocess.run(['node', str(harness), str(JS), mode], capture_output=True, text=True, timeout=30)
        finally:
            harness.unlink()
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_cap_keeps_config_and_last_30(self):
        r = self.run_js()
        self.assertEqual(r['state'], 'active')
        self.assertEqual(r['long'], [0] + list(range(70, 100)))
        self.assertEqual(r['short'], list(range(12)))
        self.assertEqual(r['noCfg'], 30)
        self.assertEqual(r['cfgInTail'], 30)

    def test_overrides_and_off(self):
        r = self.run_js()
        self.assertEqual(r['override0'], 100)
        self.assertEqual(r['override10'], [0] + list(range(90, 100)))
        self.assertEqual(r['off'], 100)

    def test_reinstall_is_idempotent(self):
        r = self.run_js()
        self.assertTrue(r['idempotent'])
        self.assertTrue(r['unwrapped'])

    def test_unknown_module_patches_nothing(self):
        r = self.run_js('wrongmodule')
        self.assertEqual(r['state'], 'module-missing')
        self.assertEqual(r['long'], list(range(100)))

    def test_registered_as_part(self):
        self.assertIn('stepcap', desktop_shell.PART_FILES)
        self.assertIn('stepcap', [stem for stem, _js, _css in desktop_shell.part_files()])


if __name__ == '__main__':
    unittest.main()
