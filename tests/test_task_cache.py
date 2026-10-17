import io
import tempfile
import unittest

from notiontool import task_cache as tc
from notiontool.store import Store

MANIFEST = {"sources": [{"kind": "artifact", "id": "a1", "sha256": "x", "tokens": 3}], "total_tokens": 3, "budget": 100}
FILES = {"notiontool/a.py": tc.hash_file(b"alpha"), "tests/b.py": tc.hash_file(b"beta")}


class KeyTests(unittest.TestCase):
    def test_same_inputs_same_key(self):
        self.assertEqual(tc.cache_key("do x", MANIFEST, FILES), tc.cache_key("do x", dict(MANIFEST), dict(FILES)))

    def test_manifest_key_order_does_not_matter(self):
        reordered = {"budget": 100, "total_tokens": 3, "sources": MANIFEST["sources"]}
        self.assertEqual(tc.cache_key("p", MANIFEST, FILES), tc.cache_key("p", reordered, FILES))

    def test_changed_prompt_manifest_or_file_gives_new_key(self):
        base = tc.cache_key("do x", MANIFEST, FILES)
        self.assertNotEqual(base, tc.cache_key("do y", MANIFEST, FILES))
        self.assertNotEqual(base, tc.cache_key("do x", dict(MANIFEST, budget=200), FILES))
        changed = dict(FILES, **{"tests/b.py": tc.hash_file(b"beta2")})
        self.assertNotEqual(base, tc.cache_key("do x", MANIFEST, changed))
        self.assertNotEqual(base, tc.cache_key("do x", MANIFEST, {"notiontool/a.py": FILES["notiontool/a.py"]}))

    def test_path_normalization(self):
        back = {"notiontool\\a.py": FILES["notiontool/a.py"], "tests\\b.py": FILES["tests/b.py"]}
        reversed_pairs = list(reversed(list(back.items())))
        key = tc.cache_key("p", MANIFEST, FILES)
        self.assertEqual(key, tc.cache_key("p", MANIFEST, back))
        self.assertEqual(key, tc.cache_key("p", MANIFEST, reversed_pairs))
        self.assertEqual(tc.normalize_path(".\\notiontool\\\\a.py"), "notiontool/a.py")

    def test_conflicting_spellings_raise(self):
        with self.assertRaises(ValueError):
            tc.normalize_file_hashes([("a/b.py", "1"), ("a\\b.py", "2")])

    def test_hash_file_bytes_and_reader_match(self):
        data = b"x" * 200000
        self.assertEqual(tc.hash_file(data), tc.hash_file(io.BytesIO(data)))
        self.assertEqual(tc.hash_file(b""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        with self.assertRaises(TypeError):
            tc.hash_file("text")
        with self.assertRaises(TypeError):
            tc.hash_file(io.StringIO("text"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(self.tmp.name)
        self.key = tc.cache_key("do x", MANIFEST, FILES)
        tc.store_result(self.store, self.key, {"text": "done", "ok": True}, FILES)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_hit_when_everything_matches(self):
        extra = dict(FILES, **{"other.py": "zzz"})
        self.assertEqual(tc.lookup(self.store, self.key, FILES), {"text": "done", "ok": True})
        self.assertEqual(tc.lookup(self.store, self.key, extra), {"text": "done", "ok": True})
        back = {"notiontool\\a.py": FILES["notiontool/a.py"], "tests\\b.py": FILES["tests/b.py"]}
        self.assertIsNotNone(tc.lookup(self.store, self.key, back))

    def test_changed_hash_misses_and_invalidates(self):
        changed = dict(FILES, **{"tests/b.py": tc.hash_file(b"beta2")})
        self.assertIsNone(tc.lookup(self.store, self.key, changed))
        self.assertIsNone(self.store.get(tc.KIND, self.key))
        self.assertIsNone(tc.lookup(self.store, self.key, FILES))

    def test_missing_file_misses_and_invalidates(self):
        self.assertIsNone(tc.lookup(self.store, self.key, {"notiontool/a.py": FILES["notiontool/a.py"]}))
        self.assertIsNone(self.store.get(tc.KIND, self.key))

    def test_explicit_invalidate(self):
        self.assertTrue(tc.invalidate(self.store, self.key))
        self.assertIsNone(tc.lookup(self.store, self.key, FILES))
        self.assertFalse(tc.invalidate(self.store, self.key))

    def test_unknown_key_misses(self):
        self.assertIsNone(tc.lookup(self.store, "nope", FILES))

    def test_store_normalizes_paths(self):
        key = tc.cache_key("p", {}, {"a\\x.py": "h1"})
        doc = tc.store_result(self.store, key, "r", {"a\\x.py": "h1"})
        self.assertEqual(doc["file_hashes"], {"a/x.py": "h1"})
        self.assertEqual(self.store.get(tc.KIND, key)["file_hashes"], {"a/x.py": "h1"})
        self.assertEqual(tc.lookup(self.store, key, {"a/x.py": "h1"}), "r")


if __name__ == "__main__":
    unittest.main()
