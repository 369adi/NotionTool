import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from notiontool.notion import NotionAdapter


class NativeIdTests(unittest.TestCase):
    space = '440869f1-58c1-81c8-b3d1-0003ec9a0dfc'

    def at(self, date, tail='00aa'):
        stamp = datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()
        with patch('notiontool.notion.time.time', return_value=stamp):
            return uuid.UUID(NotionAdapter.new_id(self.space, tail))

    def test_date_prefix_changes_at_utc_midnight(self):
        before = self.at('2026-10-14T23:59:59')
        after = self.at('2026-10-15T00:00:00')
        self.assertEqual(int(before.hex[:3], 16)+1, int(after.hex[:3], 16))
        self.assertEqual(after.hex[:3], '3fa')

    def test_short_space_id_and_table_tag_are_preserved(self):
        message = self.at('2026-10-15T12:00:00')
        thread = self.at('2026-10-15T12:00:00', '00a9')
        for generated, table in [(message, '00aa'), (thread, '00a9')]:
            self.assertEqual(generated.version, 8)
            self.assertEqual(generated.hex[3:12], uuid.UUID(self.space).hex[3:12])
            self.assertEqual(generated.hex[20:24], table)
            self.assertEqual(generated.bytes[6], 0x80)
            self.assertEqual(generated.variant, uuid.RFC_4122)

    def test_legacy_space_uses_uuid4(self):
        generated = uuid.UUID(NotionAdapter.new_id('d602c00a-50c5-4fc2-8ad0-bf8676a8abe2'))
        self.assertEqual(generated.version, 4)

    def test_random_suffix_avoids_collisions(self):
        ids = {str(self.at('2026-10-15T12:00:00', '00a9')) for _ in range(100)}
        self.assertEqual(len(ids), 100)

    def test_out_of_range_date_fails_instead_of_reusing_stale_prefix(self):
        with self.assertRaisesRegex(ValueError, 'date range'):
            self.at('2023-12-31T12:00:00')
