import unittest

from notiontool import protocol_forward as pf

ORIG = '"C:\\Program Files\\WindowsApps\\Notion\\Notion.exe" "%1"'
OURS = '"C:\\NT\\NotionTool.exe" --protocol-forward "%1"'
HOST = 'https://www.' + 'notion' + '.so'
URL = 'notion://www.notion.so/loginwithtoken?code=abc'


class FakeReg:
    def __init__(self, command=ORIG):
        self.data = {(pf.COMMAND_KEY, ''): command} if command else {}

    def get(self, path, name=''):
        return self.data.get((path, name))

    def set(self, path, value, name=''):
        self.data[(path, name)] = value


def targets(*urls):
    return lambda url: [dict(type='page', url=u) for u in urls]


class ProtocolForwardTest(unittest.TestCase):
    def setUp(self):
        # Never write the real %LOCALAPPDATA% forward log from tests.
        self._log, pf._log = pf._log, (lambda data: None)

    def tearDown(self):
        pf._log = self._log

    def test_install_saves_original_and_is_idempotent(self):
        reg = FakeReg()
        self.assertTrue(pf.install(OURS, reg)['changed'])
        self.assertEqual(reg.get(pf.COMMAND_KEY), OURS)
        self.assertEqual(reg.get(pf.SAVE_KEY, 'OriginalCommand'), ORIG)
        self.assertFalse(pf.install(OURS, reg)['changed'])
        self.assertEqual(reg.get(pf.SAVE_KEY, 'OriginalCommand'), ORIG)

    def test_reinstall_after_notion_overwrote_handler(self):
        reg = FakeReg()
        pf.install(OURS, reg)
        reg.set(pf.COMMAND_KEY, ORIG)  # normal Notion re-registered itself
        self.assertTrue(pf.install(OURS, reg)['changed'])
        self.assertEqual(reg.get(pf.SAVE_KEY, 'OriginalCommand'), ORIG)

    def test_install_refuses_without_original(self):
        with self.assertRaises(RuntimeError):
            pf.install(OURS, FakeReg(None))

    def test_restore(self):
        reg = FakeReg()
        pf.install(OURS, reg)
        self.assertTrue(pf.restore(reg)['restored'])
        self.assertEqual(reg.get(pf.COMMAND_KEY), ORIG)

    def test_login_window_gets_the_link(self):
        reg = FakeReg(); pf.install(OURS, reg)
        target, argv = pf.decide(URL, reg, targets(HOST + '/login'), profile_dir='P')
        self.assertEqual(target, 'notiontool')
        self.assertEqual(argv, ['C:\\Program Files\\WindowsApps\\Notion\\Notion.exe', '--user-data-dir=P',
                                '--remote-debugging-port=9333', URL])

    def test_signed_in_window_leaves_link_to_normal_notion(self):
        reg = FakeReg(); pf.install(OURS, reg)
        target, argv = pf.decide(URL, reg, targets(HOST + '/Budget-0123456789abcdef'), profile_dir='P')
        self.assertEqual(target, 'normal')
        self.assertEqual(argv, ['C:\\Program Files\\WindowsApps\\Notion\\Notion.exe', URL])

    def test_no_window_goes_to_normal_notion(self):
        reg = FakeReg(); pf.install(OURS, reg)
        self.assertEqual(pf.decide(URL, reg, lambda u: None)[0], 'normal')

    def test_main_spawns_and_ignores_non_notion(self):
        reg = FakeReg(); pf.install(OURS, reg)
        spawned = []
        self.assertEqual(pf.main(['x', pf.FLAG, URL], spawned.append, reg, targets(HOST + '/login')), 0)
        self.assertEqual(spawned[0][-1], URL)
        self.assertEqual(pf.main(['x', pf.FLAG, 'https://evil'], spawned.append, reg), 2)
        self.assertEqual(len(spawned), 1)


    def test_guard_retakes_handler_after_notion_overwrites_it(self):
        import threading
        reg = FakeReg()
        stop = threading.Event()
        changes = []
        calls = []

        def installer():
            calls.append(1)
            if len(calls) == 2:
                reg.set(pf.COMMAND_KEY, ORIG)  # Notion.exe re-registered between two checks
            if len(calls) >= 3:
                stop.set()
            return pf.install(OURS, reg)

        pf.guard(stop, interval=0, installer=installer, on_change=changes.append)
        self.assertEqual(reg.get(pf.COMMAND_KEY), OURS)
        self.assertEqual(len(changes), 2)
        self.assertEqual(reg.get(pf.SAVE_KEY, 'OriginalCommand'), ORIG)

if __name__ == '__main__':
    unittest.main()
