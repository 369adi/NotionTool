"""Round 19 (user 2026-10-28 05:02): Notion never shows through, effort pick from the clicked row."""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


class Round19Tests(unittest.TestCase):
    def setUp(self):
        self.js = read('notiontool', 'web', 'desktop', 'v6.js')
        self.css = read('notiontool', 'web', 'desktop', 'v6.css')
        self.ds = read('notiontool', 'desktop_shell.py')

    def pick(self):
        a = self.js.index('    function onEffortPick(e) {')
        b = self.js.index('\n    }\n', a)
        return self.js[a:b]

    def test_effort_pick_reads_the_clicked_row_at_once(self):
        body = self.pick()
        self.assertNotIn('setTimeout', body)
        self.assertNotIn('nativeEffort()', body)
        self.assertIn('effortLevel(text(it))', body)
        self.assertIn("ctx.call('chat_effort_pick', { thread_id: tid, effort: lv })", body)
        self.assertIn("closest('.notion-overlay-container')", body)
        self.assertIn('effortBackend[tid] = lv', body)
        self.assertIn("xhigh: 'xHigh'", self.js)

    def test_effort_level_maps_notion_menu_rows(self):
        node = shutil.which('node') or r'C:\Program Files\nodejs\node.exe'
        if not os.path.exists(node):
            self.skipTest('node missing')
        a = self.js.index('    function effortLevel(label) {')
        b = self.js.index('\n    }\n', a) + 6
        src = ('function str(v){return v==null?"":String(v)}\n' + self.js[a:b] +
               '\nconsole.log(JSON.stringify(["xHigh","Medium Default","High","Max","Low","Extra high"].map(effortLevel)))')
        out = subprocess.run([node, '-e', src], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), '["xhigh","medium","high","max","low","xhigh"]')

    def test_mask_hides_notion_by_default_in_a_chat(self):
        block = self.css[self.css.index('round 19 (user 2026-10-28 05:02'):]
        self.assertIn('html.nt-v6-on.nt-v6-mask #notion-app { visibility: hidden !important; }', block)
        for part in ('[data-nt-desk-own]', '.nt-desk-composer', '.notion-overlay-container', '[role="dialog"]',
                     ':not(.nt-v6-feed-on) #notion-app .layout-chat'):
            self.assertIn(part, block)
        self.assertIn('function maskSync()', self.js)
        self.assertIn('      maskSync();\n      ensureNodes();', self.js)
        self.assertIn("root.classList.remove('nt-v6-mask');", self.js)

    def test_boot_cover_from_the_first_paint_with_fail_safe(self):
        self.assertIn("cover.id = 'nt-boot-cover';", self.ds)
        self.assertIn('window.__ntBootCoverOff = function', self.ds)
        self.assertIn('}, 30000);', self.ds)
        self.assertIn("getElementById('nt-boot-cover'); if (bootCover", self.ds)
        self.assertIn('window.__ntBootCoverOff', self.js)
        # the cover sits in the document-start bootstrap, before the parts mount
        self.assertLess(self.ds.index("cover.id = 'nt-boot-cover';"), self.ds.index('_BOOT_TAIL = '))


if __name__ == '__main__':
    unittest.main()
