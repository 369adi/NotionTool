"""Backtest of the stepcap v3 scroll-up loading with a fake ThreadStore and fake scroller (runs the real JS in node)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from notiontool import desktop_shell

JS = Path(desktop_shell.__file__).resolve().parent / 'web' / 'desktop' / 'stepcap.js'

HARNESS = r'''
var window = globalThis; globalThis.window = window;
var emits = [];
function Store(i, type, tid) { this.i = i; this.type = type; this.tid = tid; }
Store.prototype.getStep = function () { return { type: this.type, id: this.tid + '-s' + this.i }; };
function ThreadStore(id, n) {
  this.id = id; this.n = n;
  var self = this;
  this.internals = { _pointer: { table: 'thread', id: id }, inMemoryRecordCache: { emitRecord: function (p, paths) { emits.push([p.id, JSON.stringify(paths)]); } } };
}
ThreadStore.keyName = 'ThreadStore';
ThreadStore.prototype.getMessageStores = function () {
  var out = [];
  for (var i = 0; i < this.n; i++) out.push(new Store(i, i === 0 ? 'config' : 'user', this.id));
  return out;
};
window.rspackChunknotion_next = { push: function (c) { c[2](function (id) { if (id !== 174148) throw new Error('no'); return { E: ThreadStore }; }); } };
window.setTimeout = function () {};
window.requestAnimationFrame = function () {};
require(process.argv[2]);
var X = window.__ntStepExp;
var r = { state: window.__ntStepCapState, version: X.version };
var a = new ThreadStore('a', 100), b = new ThreadStore('b', 50);
a.getMessageStores(); b.getMessageStores();
function El(tid, stepIdx) {
  this.scrollHeight = 2000; this.clientHeight = 600; this.scrollTop = 0; this.nodeType = 1; this.isConnected = true;
  this.querySelectorAll = function () { return stepIdx.map(function (i) { return { getAttribute: function () { return tid + '-s' + i; } }; }); };
}
r.next = [X.nextCap(30, 100), X.nextCap(60, 100), X.nextCap(90, 100), X.nextCap(0, 100), X.nextCap(30, 40)];
var elA = new El('a', [95, 99]);
r.thread = X.threadForEl(elA);
r.threadNone = X.threadForEl(new El('zz', [1]));
r.threadOld = X.threadForEl(new El('a', [5]));
r.exp1 = X.expand(elA, 1000000);
r.cap1 = window.__ntStepCap.a;
r.len1 = a.getMessageStores().length;
r.cool = X.expand(elA, 1000500);
r.exp2 = X.expand(elA, 1003000);
r.exp3 = X.expand(elA, 1006000);
r.cap3 = window.__ntStepCap.a;
r.len3 = a.getMessageStores().length;
r.exp4 = X.expand(elA, 1009000);
r.capB = Object.prototype.hasOwnProperty.call(window.__ntStepCap, 'b');
r.emitsAfterExpand = emits.slice();
r.reset = X.reset('a');
r.capReset = window.__ntStepCap.a;
r.lenReset = a.getMessageStores().length;
r.resetAgain = X.reset('a');
var elB = new El('b', [45]);
X._resetCooldown();
r.expB = X.expand(elB, 2000000);
elB.isConnected = false;
X.sweep();
r.capBSwept = window.__ntStepCap.b;
r.expandedLeft = Object.keys(X.expanded);
r.emitsTotal = emits.length;
window.__ntStepCapOff = true;
r.offLen = a.getMessageStores().length;
console.log(JSON.stringify(r));
'''


@unittest.skipUnless(shutil.which('node'), 'node not available')
class StepCapExpandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        harness = Path(__file__).resolve().parent / '_stepcap_expand_harness.cjs'
        harness.write_text(HARNESS, encoding='utf-8')
        try:
            out = subprocess.run(['node', str(harness), str(JS)], capture_output=True, text=True, timeout=30)
        finally:
            harness.unlink()
        if out.returncode != 0:
            raise AssertionError(out.stderr)
        cls.r = json.loads(out.stdout.strip().splitlines()[-1])

    def test_active_v3(self):
        self.assertEqual(self.r['state'], 'active')
        self.assertEqual(self.r['version'], 3)

    def test_next_cap(self):
        self.assertEqual(self.r['next'], [60, 90, 0, 0, 0])

    def test_finds_visible_thread(self):
        self.assertEqual(self.r['thread'], 'a')
        self.assertIsNone(self.r['threadNone'])
        self.assertIsNone(self.r['threadOld'])  # step outside the rendered tail is not matched

    def test_expand_steps_and_cooldown(self):
        r = self.r
        self.assertEqual(r['exp1'], 60)
        self.assertEqual(r['cap1'], 60)
        self.assertEqual(r['len1'], 61)
        self.assertEqual(r['cool'], 'cooldown')
        self.assertEqual(r['exp2'], 90)
        self.assertEqual(r['exp3'], 0)
        self.assertEqual(r['cap3'], 0)
        self.assertEqual(r['len3'], 100)
        self.assertEqual(r['exp4'], 'all-loaded')
        self.assertFalse(r['capB'])  # other chats untouched

    def test_emits_like_notion(self):
        self.assertEqual(self.r['emitsAfterExpand'], [['a', '[]'], ['a', '[]'], ['a', '[]']])

    def test_reset_back_to_30(self):
        r = self.r
        self.assertTrue(r['reset'])
        self.assertEqual(r['capReset'], 30)
        self.assertEqual(r['lenReset'], 31)
        self.assertFalse(r['resetAgain'])

    def test_sweep_on_closed_chat(self):
        r = self.r
        self.assertEqual(r['expB'], 0)  # 50 stores: 30 + 30 >= 50 -> uncapped
        self.assertEqual(r['capBSwept'], 30)
        self.assertEqual(r['expandedLeft'], [])
        self.assertEqual(r['emitsTotal'], 5)  # 3 expands a + reset a + expand b (sweep does not emit)

    def test_off_switch(self):
        self.assertEqual(self.r['offLen'], 100)


if __name__ == '__main__':
    unittest.main()
