import threading
import unittest

from notiontool.edge_transport import EdgeTransport

BASE = 'X-BASE'


class FakeBrowser:
    process = None  # reused Edge: not started by this NotionTool
    port = 1

    def ensure(self):
        return True

    def tabs(self):
        return [{'id': 't1', 'url': BASE + '/robots.txt', 'webSocketDebuggerUrl': 'ws://x'}]

    def send(self, tab, method, params=None, timeout=30):
        assert tab is not None
        return {}

    def evaluate(self, tab, expr):
        assert tab is not None
        return BASE if 'origin' in expr else 'complete'

    def hidden(self):
        return False


class ReuseTarget(unittest.TestCase):
    def test_reused_edge_gets_a_target(self):
        transport = EdgeTransport.__new__(EdgeTransport)
        transport.lock = threading.RLock()
        transport.browser = FakeBrowser()
        transport.ready_process = None
        transport.target = None
        transport._base = BASE
        transport.wake = lambda tab: None
        try:
            transport.prepare()
        except Exception:
            pass  # cookie/helper steps of the real class may need more; the target must be set first
        self.assertIsNotNone(transport.target)
        self.assertEqual(transport.target['id'], 't1')


if __name__ == '__main__':
    unittest.main()
