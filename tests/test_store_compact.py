import json
import shutil
import tempfile
import unittest

from notiontool.store import Store


class StoreCompactTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_new_store_uses_incremental_auto_vacuum(self):
        store = Store(self.dir)
        self.assertEqual(store.db.execute('PRAGMA auto_vacuum').fetchone()[0], 2)
        store.db.close()

    def test_free_pages_are_returned_on_next_start(self):
        store = Store(self.dir)
        with store.db:
            store.db.executemany('INSERT INTO objects VALUES(?,?,?)',
                                 [('x', str(i), json.dumps({'pad': 'y' * 2000})) for i in range(2000)])
        with store.db:
            store.db.execute("DELETE FROM objects WHERE kind='x'")
        store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        free_before = store.db.execute('PRAGMA freelist_count').fetchone()[0]
        self.assertGreater(free_before, 0)
        store.db.close()
        again = Store(self.dir)
        again.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.assertLess(again.db.execute('PRAGMA freelist_count').fetchone()[0], free_before)
        self.assertEqual(again.compact(), '')  # nothing left to do
        again.db.close()


if __name__ == '__main__':
    unittest.main()
