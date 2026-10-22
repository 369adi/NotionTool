import tempfile
import unittest
from pathlib import Path

from notiontool import notion_shell
from notiontool.store import Store


class PageDefaults(unittest.TestCase):
    def test_page_shows_the_server_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            try:
                settings = store.settings()
                for key, value in notion_shell.PAGE_DEFAULTS.items():
                    self.assertEqual(settings.get(key), value, key)
            finally:
                try:
                    store.close()
                except Exception:
                    pass

    def test_old_chats_are_deleted_by_default(self):
        self.assertIs(notion_shell.PAGE_DEFAULTS['coworker_delete_old_chat'], True)


if __name__ == '__main__':
    unittest.main()
