import unittest

from notiontool.desktop_app import is_notion_page


def page(url):
    return {'type': 'page', 'url': url}


class NotionPageHostTests(unittest.TestCase):
    def test_app_notion_com_workspace_page(self):
        self.assertTrue(is_notion_page(page('https://app.notion.com/p/3fda1b13e54d806fb1a4e93152c3c04b')))

    def test_classic_hosts_still_work(self):
        self.assertTrue(is_notion_page(page('https://www.notion.so/Notes-abc123')))

    def test_blank_login_and_handoff_pages_rejected(self):
        for path in ('/blank', '/login', '/login?redirectURL=/x', '/desktopwithbrowserlogin?x=1', '/signup'):
            self.assertFalse(is_notion_page(page('https://app.notion.com' + path)), path)

    def test_root_still_allowed(self):
        self.assertTrue(is_notion_page(page('https://notion.com/')))

    def test_foreign_host_and_file_rejected(self):
        self.assertFalse(is_notion_page(page('https://evil.example/p/abc')))
        self.assertFalse(is_notion_page(page('file:///C:/tabbar.html')))
        self.assertFalse(is_notion_page({'type': 'worker', 'url': 'https://app.notion.com/p/abc'}))


if __name__ == '__main__':
    unittest.main()
