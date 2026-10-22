import tempfile
import unittest
import uuid
from pathlib import Path

from notiontool import notion

OWNER = next(v for v in vars(notion).values() if isinstance(v, type) and 'new_id' in vars(v))
V8_SPACE = str(uuid.UUID('06e039884abc8fae80777591123c5b6c'))
V4_SPACE = str(uuid.UUID('06e039884abc4fae80777591123c5b6c'))


class OwnIds(unittest.TestCase):
    def setUp(self):
        notion.OWN_CONFIG_IDS.clear()
        notion.OWN_ID_FILE[0] = None

    def test_signed_new_id_is_own_without_memory(self):
        ident = OWNER.new_id(V8_SPACE, '00aa')
        self.assertTrue(notion.own_config_step({'id': ident}))

    def test_foreign_00aa_id_after_cutoff_is_not_own(self):
        ident = '4010a2b3-4abc-80ab-9123-00aa12345678'
        if notion.own_id_sign(ident[:-4]) == '5678':
            ident = ident[:-1] + '9'
        self.assertFalse(notion.own_config_step({'id': ident}))

    def test_legacy_00aa_id_until_cutoff_is_own(self):
        self.assertTrue(notion.own_config_step({'id': '4000a2b3-4abc-80ab-9123-00aa12345678'}))

    def test_notion_tail_is_not_own(self):
        self.assertFalse(notion.own_config_step({'id': '4010a2b3-4abc-80ab-9123-00a912345678'}))

    def test_legacy_space_ids_persist_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'own_config_ids.txt'
            notion.load_own_config_ids(file)
            ident = OWNER.new_id(V4_SPACE, '00aa')  # legacy space: plain uuid4
            notion.remember_own_config_id(ident)
            notion.OWN_CONFIG_IDS.clear()  # simulated restart
            self.assertFalse(notion.own_config_step({'id': ident}))
            notion.load_own_config_ids(file)
            self.assertTrue(notion.own_config_step({'id': ident}))


if __name__ == '__main__':
    unittest.main()
