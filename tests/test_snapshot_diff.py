import hashlib
import unittest

from notiontool import snapshot_diff as sd


def sha12(data):
    return hashlib.sha256(data).hexdigest()[:12]


class LineEndingStyleTests(unittest.TestCase):
    def test_styles(self):
        self.assertEqual(sd.line_ending_style(b"a\nb\n"), "LF")
        self.assertEqual(sd.line_ending_style(b"a\r\nb\r\n"), "CRLF")
        self.assertEqual(sd.line_ending_style(b"a\rb\r"), "CR")
        self.assertEqual(sd.line_ending_style(b"a\r\nb\n"), "mixed")
        self.assertEqual(sd.line_ending_style(b"abc"), "none")


class UnifiedDiffTests(unittest.TestCase):
    def test_identical_bytes(self):
        self.assertEqual(sd.unified_diff(b"a\nb\n", b"a\nb\n", "x/a.py"), "x/a.py: no changes")

    def test_path_is_normalized(self):
        self.assertEqual(sd.unified_diff(b"a", b"a", ".\\pkg\\\\m.py"), "pkg/m.py: no changes")

    def test_crlf_to_lf_only(self):
        out = sd.unified_diff(b"a\r\nb\r\n", b"a\nb\n", "a.py")
        self.assertEqual(out, "a.py: line endings changed CRLF -> LF")

    def test_lf_to_crlf_only(self):
        out = sd.unified_diff(b"a\nb\n", b"a\r\nb\r\n", "a.py")
        self.assertEqual(out, "a.py: line endings changed LF -> CRLF")

    def test_mixed_line_endings(self):
        out = sd.unified_diff(b"a\r\nb\nc\r", b"a\nb\nc\n", "m.txt")
        self.assertEqual(out, "m.txt: line endings changed mixed -> LF")

    def test_content_diff(self):
        out = sd.unified_diff(b"one\ntwo\nthree\n", b"one\n2\nthree\n", "n.py")
        lines = out.split("\n")
        self.assertEqual(lines[0], "--- a/n.py")
        self.assertEqual(lines[1], "+++ b/n.py")
        self.assertIn("-two", lines)
        self.assertIn("+2", lines)
        self.assertNotIn("line endings", out)

    def test_line_endings_and_content(self):
        out = sd.unified_diff(b"one\r\ntwo\r\n", b"one\nTWO\n", "c.py")
        lines = out.split("\n")
        self.assertEqual(lines[0], "c.py: line endings changed CRLF -> LF")
        self.assertEqual(lines[1], "--- a/c.py")
        self.assertIn("-two", lines)
        self.assertIn("+TWO", lines)
        self.assertNotIn("\r", out)
        self.assertEqual(out.count("line endings changed"), 1)

    def test_final_newline_only(self):
        self.assertEqual(sd.unified_diff(b"a", b"a\n", "f.txt"), "f.txt: final newline added")
        self.assertEqual(sd.unified_diff(b"a\n", b"a", "f.txt"), "f.txt: final newline removed")

    def test_truncation_at_max_lines(self):
        before = b"".join(b"line %d\n" % i for i in range(100))
        after = b"".join(b"LINE %d\n" % i for i in range(100))
        full = sd.unified_diff(before, after, "t.txt", max_lines=100000).split("\n")
        out = sd.unified_diff(before, after, "t.txt", max_lines=10).split("\n")
        self.assertGreater(len(full), 10)
        self.assertEqual(len(out), 11)
        self.assertEqual(out[:10], full[:10])
        self.assertEqual(out[10], "... %d more lines not shown (max_lines=10)" % (len(full) - 10))

    def test_default_max_lines_is_400(self):
        before = b"".join(b"a%d\n" % i for i in range(300))
        after = b"".join(b"b%d\n" % i for i in range(300))
        out = sd.unified_diff(before, after, "big.txt").split("\n")
        self.assertEqual(len(out), 401)
        self.assertTrue(out[-1].endswith("more lines not shown (max_lines=400)"))

    def test_binary_data(self):
        before = b"\x00\x01\x02"
        after = b"\x00\x01\x03\x04"
        out = sd.unified_diff(before, after, "b.bin")
        expected = "b.bin: binary file differs (before 3 bytes sha256 %s, after 4 bytes sha256 %s)" % (
            sha12(before), sha12(after))
        self.assertEqual(out, expected)

    def test_invalid_utf8_is_binary(self):
        out = sd.unified_diff(b"\xff\xfeabc", b"abc", "x.txt")
        self.assertTrue(out.startswith("x.txt: binary file differs (before 5 bytes"))
        self.assertEqual(len(out.split("\n")), 1)

    def test_added_file(self):
        lines = sd.unified_diff(None, b"new\n", "src\\n.py").split("\n")
        self.assertEqual(lines[0], "src/n.py: file added (4 bytes)")
        self.assertEqual(lines[1], "--- /dev/null")
        self.assertEqual(lines[2], "+++ b/src/n.py")
        self.assertIn("+new", lines)

    def test_removed_file(self):
        lines = sd.unified_diff(b"old\n", None, "old.py").split("\n")
        self.assertEqual(lines[0], "old.py: file removed (4 bytes)")
        self.assertEqual(lines[1], "--- a/old.py")
        self.assertEqual(lines[2], "+++ /dev/null")
        self.assertIn("-old", lines)

    def test_added_binary_and_missing_both(self):
        out = sd.unified_diff(None, b"\x00\x01", "img.bin").split("\n")
        self.assertEqual(out[0], "img.bin: file added (2 bytes)")
        self.assertEqual(out[1], "img.bin: binary file differs (before missing, after 2 bytes sha256 %s)" % sha12(b"\x00\x01"))
        self.assertEqual(sd.unified_diff(None, None, "x"), "x: no changes (missing on both sides)")


class SummaryTests(unittest.TestCase):
    def test_counts_and_order(self):
        result = {"changed": ["b.py", "a.py"], "added": ["new.py"], "removed": ["gone.py"],
                  "unchanged": ["u1.py", "u2.py"]}
        expected = "\n".join([
            "Snapshot comparison: 2 changed, 1 added, 1 removed, 2 unchanged.",
            "Changed (2):", "- a.py", "- b.py",
            "Added (1):", "- new.py",
            "Removed (1):", "- gone.py",
        ])
        self.assertEqual(sd.summary(result), expected)
        self.assertNotIn("u1.py", sd.summary(result))

    def test_truncation(self):
        result = {"changed": ["f%02d.py" % i for i in range(25)], "added": [], "removed": [], "unchanged": []}
        lines = sd.summary(result, max_paths=5).split("\n")
        self.assertEqual(lines[0], "Snapshot comparison: 25 changed, 0 added, 0 removed, 0 unchanged.")
        self.assertEqual(lines[1], "Changed (25):")
        self.assertEqual(lines[2:7], ["- f%02d.py" % i for i in range(5)])
        self.assertEqual(lines[7], "- ... and 20 more")
        self.assertEqual(len(lines), 8)

    def test_default_cap_is_20(self):
        result = {"removed": ["r%02d" % i for i in range(25)]}
        lines = sd.summary(result).split("\n")
        self.assertEqual(lines[-1], "- ... and 5 more")
        self.assertEqual(len(lines), 2 + 20 + 1)

    def test_empty_result(self):
        expected = "Snapshot comparison: 0 changed, 0 added, 0 removed, 0 unchanged.\nNo files changed."
        empty = {"changed": [], "added": [], "removed": [], "unchanged": []}
        self.assertEqual(sd.summary(empty), expected)
        self.assertEqual(sd.summary({}), expected)
        self.assertEqual(sd.summary(None), expected)

    def test_only_unchanged(self):
        out = sd.summary({"unchanged": ["a", "b", "c"]})
        self.assertEqual(out, "Snapshot comparison: 0 changed, 0 added, 0 removed, 3 unchanged.\nNo files changed.")

    def test_paths_normalized_and_deduplicated(self):
        out = sd.summary({"changed": ["src\\a.py", "src/a.py"]})
        self.assertEqual(out, "Snapshot comparison: 1 changed, 0 added, 0 removed, 0 unchanged.\nChanged (1):\n- src/a.py")


if __name__ == "__main__":
    unittest.main()
