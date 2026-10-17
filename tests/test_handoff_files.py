import json
import os
import tempfile
import unittest
from unittest import mock

from notiontool import handoff_files as hf


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self._tmp.name, "handoff")

    def tearDown(self):
        self._tmp.cleanup()

    def path(self, worker_id):
        return os.path.join(self.base, worker_id + ".json")

    def load(self, worker_id):
        with open(self.path(worker_id), "r", encoding="utf-8") as fh:
            return json.load(fh)


class WriteTests(Base):
    def test_atomic_write_complete_and_no_temp_left(self):
        result = hf.write_handoff(self.base, "w1", {"summary": "x" * 5000}, 100)
        self.assertEqual(os.listdir(self.base), ["w1.json"])
        self.assertEqual(os.path.basename(result["path"]), "w1.json")
        with open(self.path("w1"), "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"\r", raw)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        record = json.loads(raw.decode("utf-8"))
        self.assertEqual(record["summary"], "x" * 5000)
        self.assertEqual(record["token"], result["token"])
        self.assertEqual(result["created"], 100)

    def test_replace_failure_keeps_old_file_and_removes_temp(self):
        hf.write_handoff(self.base, "w1", {"summary": "old"}, 100)
        hf.mark_acked(self.base, "w1", 110)
        with open(self.path("w1"), "rb") as fh:
            before = fh.read()
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                hf.write_handoff(self.base, "w1", {"summary": "new"}, 200)
        with open(self.path("w1"), "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(os.listdir(self.base), ["w1.json"])

    def test_record_fields(self):
        result = hf.write_handoff(self.base, "w1", {"old_thread": "t-old", "summary": "s"}, 1234)
        record = self.load("w1")
        self.assertEqual(record["schema_version"], hf.SCHEMA_VERSION)
        self.assertEqual(hf.SCHEMA_VERSION, 1)
        self.assertEqual(record["created"], 1234)
        self.assertEqual(record["old_thread"], "t-old")
        self.assertIsNone(record["new_thread"])
        self.assertIsNone(record["acked_at"])
        self.assertIsNone(record["first_task_done_at"])
        self.assertRegex(result["token"], r"^[0-9a-f]{8}$")

    def test_redaction_bearer_token_v2_api_key_nested(self):
        payload = {
            "note": "Authorization: Bearer abcDEF123456.xyz-789",
            "cfg": {
                "cookie": "token_v2=v02%3Auser_token_abc123; theme=dark",
                "list": ["api_key=SECRETKEY123", {"deep": "x-api-key: QWERTY987"}],
            },
        }
        hf.write_handoff(self.base, "w1", payload, 1)
        with open(self.path("w1"), "r", encoding="utf-8") as fh:
            text = fh.read()
        for secret in ("abcDEF123456", "user_token_abc123", "SECRETKEY123", "QWERTY987"):
            self.assertNotIn(secret, text)
        record = json.loads(text)
        self.assertEqual(record["note"], "Authorization: Bearer [REDACTED]")
        self.assertEqual(record["cfg"]["cookie"], "token_v2=[REDACTED]; theme=dark")
        self.assertEqual(record["cfg"]["list"][0], "api_key=[REDACTED]")
        self.assertEqual(record["cfg"]["list"][1]["deep"], "x-api-key: [REDACTED]")

    def test_redaction_secret_keys_and_sk_keys(self):
        payload = {"api_key": "plainvalue1", "token_v2": "plainvalue2",
                   "text": "use sk-abcdefghijklmnop1234 here", "plain": "keep me"}
        hf.write_handoff(self.base, "w1", payload, 1)
        record = self.load("w1")
        self.assertEqual(record["api_key"], "[REDACTED]")
        self.assertEqual(record["token_v2"], "[REDACTED]")
        self.assertEqual(record["text"], "use [REDACTED] here")
        self.assertEqual(record["plain"], "keep me")

    def test_recall_returns_same_record_without_rewrite(self):
        first = hf.write_handoff(self.base, "w1", {"summary": "a"}, 100)
        with open(self.path("w1"), "rb") as fh:
            before = fh.read()
        second = hf.write_handoff(self.base, "w1", {"summary": "b"}, 200)
        self.assertEqual(second, first)
        with open(self.path("w1"), "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_new_token_after_ack(self):
        first = hf.write_handoff(self.base, "w1", {"summary": "a"}, 100)
        hf.mark_acked(self.base, "w1", 110)
        second = hf.write_handoff(self.base, "w1", {"summary": "b"}, 200)
        self.assertNotEqual(second["token"], first["token"])
        record = self.load("w1")
        self.assertEqual(record["summary"], "b")
        self.assertIsNone(record["acked_at"])
        self.assertEqual(record["created"], 200)

    def test_traversal_ids_rejected_before_file_access(self):
        for bad in ("..x", "../x", "C:x", "/x", "", "\\x", ".hidden", "a..b", "a" * 65, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    hf.write_handoff(self.base, bad, {}, 1)
                with self.assertRaises(ValueError):
                    hf.mark_acked(self.base, bad, 1)
        self.assertFalse(os.path.exists(self.base))
        self.assertEqual(os.path.basename(hf.write_handoff(self.base, "a" * 64, {}, 1)["path"]), "a" * 64 + ".json")

    def test_first_message(self):
        result = hf.write_handoff(self.base, "w1", {}, 1)
        message = hf.first_message(result)
        self.assertLess(len(message), 600)
        self.assertIn(result["path"], message)
        self.assertIn("\n" + result["token"] + "\n", message)
        self.assertIn("read_file", message)
        self.assertTrue(hf.verify_ack("Done.\n" + result["token"] + "\n", result["token"]))


class AckTests(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(hf.verify_ack("read it\n1a2b3c4d\n", "1a2b3c4d"))
        self.assertTrue(hf.verify_ack("token: 1a2b3c4d.", "1a2b3c4d"))
        self.assertTrue(hf.verify_ack("1a2b3c4d", "1a2b3c4d"))

    def test_partial_and_longer_hex_rejected(self):
        for text in ("1a2b3c4", "x1a2b3c4d", "1a2b3c4d9", "ff1a2b3c4dff", "a2b3c4d", ""):
            with self.subTest(text=text):
                self.assertFalse(hf.verify_ack(text, "1a2b3c4d"))

    def test_empty_token_false(self):
        self.assertFalse(hf.verify_ack("anything", ""))
        self.assertFalse(hf.verify_ack("anything", None))


class LifecycleTests(Base):
    def test_no_delete_after_ack_only(self):
        hf.write_handoff(self.base, "w1", {}, 1)
        record = hf.mark_acked(self.base, "w1", 5)
        self.assertEqual(record["acked_at"], 5)
        self.assertTrue(os.path.exists(self.path("w1")))
        self.assertEqual(self.load("w1")["acked_at"], 5)

    def test_no_delete_after_done_only(self):
        hf.write_handoff(self.base, "w1", {}, 1)
        record = hf.mark_first_task_done(self.base, "w1", 6)
        self.assertEqual(record["first_task_done_at"], 6)
        self.assertTrue(os.path.exists(self.path("w1")))

    def test_delete_after_ack_then_done(self):
        hf.write_handoff(self.base, "w1", {}, 1)
        hf.mark_acked(self.base, "w1", 5)
        self.assertIsNone(hf.mark_first_task_done(self.base, "w1", 6))
        self.assertFalse(os.path.exists(self.path("w1")))

    def test_delete_after_done_then_ack(self):
        hf.write_handoff(self.base, "w1", {}, 1)
        hf.mark_first_task_done(self.base, "w1", 6)
        self.assertIsNone(hf.mark_acked(self.base, "w1", 7))
        self.assertFalse(os.path.exists(self.path("w1")))

    def test_unknown_worker_raises(self):
        with self.assertRaises(hf.UnknownWorkerError):
            hf.mark_acked(self.base, "nobody", 1)
        with self.assertRaises(LookupError):
            hf.mark_first_task_done(self.base, "nobody", 1)


class GcTests(Base):
    def test_24h_boundary(self):
        hf.write_handoff(self.base, "w1", {}, 1000)
        self.assertEqual(hf.gc(self.base, 1000 + 86400 - 1), [])
        self.assertTrue(os.path.exists(self.path("w1")))
        deleted = hf.gc(self.base, 1000 + 86400 + 1)
        self.assertEqual([os.path.basename(p) for p in deleted], ["w1.json"])
        self.assertFalse(os.path.exists(self.path("w1")))

    def test_finished_temp_and_broken_files(self):
        os.makedirs(self.base)
        now = 1000 + 86401
        with open(self.path("done"), "w", encoding="utf-8") as fh:
            json.dump({"created": now, "acked_at": now, "first_task_done_at": now}, fh)
        hf.write_handoff(self.base, "young", {}, now)
        hf.write_handoff(self.base, "acked", {}, now)
        hf.mark_acked(self.base, "acked", now)
        with open(self.path("bad"), "w", encoding="utf-8") as fh:
            fh.write("{broken")
        old_tmp = os.path.join(self.base, ".w.json.old.tmp")
        new_tmp = os.path.join(self.base, ".w.json.new.tmp")
        for path, mtime in ((old_tmp, 1000), (new_tmp, now - 10)):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x")
            os.utime(path, (mtime, mtime))
        deleted = sorted(os.path.basename(p) for p in hf.gc(self.base, now))
        self.assertEqual(deleted, [".w.json.old.tmp", "done.json"])
        self.assertEqual(sorted(os.listdir(self.base)),
                         [".w.json.new.tmp", "acked.json", "bad.json", "young.json"])

    def test_missing_base_dir(self):
        self.assertEqual(hf.gc(self.base, 1), [])


if __name__ == "__main__":
    unittest.main()
