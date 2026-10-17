import os
import tempfile
import unittest

from notiontool import eol_guard
from notiontool.eol_guard import BOM, detect, dominant, normalize, report


class DetectTests(unittest.TestCase):
    def test_lf(self):
        self.assertEqual(detect(b"a\nb\n"), "lf")

    def test_crlf(self):
        self.assertEqual(detect(b"a\r\nb\r\n"), "crlf")

    def test_mixed_crlf_and_lf(self):
        self.assertEqual(detect(b"a\r\nb\n"), "mixed")

    def test_lone_cr_is_mixed(self):
        self.assertEqual(detect(b"a\rb\n"), "mixed")
        self.assertEqual(detect(b"a\rb"), "mixed")

    def test_no_newline_is_none(self):
        self.assertEqual(detect(b"abc"), "none")

    def test_empty_is_none(self):
        self.assertEqual(detect(b""), "none")

    def test_rejects_text(self):
        with self.assertRaises(TypeError):
            detect("a\n")


class DominantTests(unittest.TestCase):
    def test_tie_goes_to_lf(self):
        self.assertEqual(dominant(b"1\r\n2\n"), "lf")
        self.assertEqual(dominant(b""), "lf")

    def test_crlf_majority(self):
        self.assertEqual(dominant(b"1\r\n2\r\n3\n"), "crlf")


class NormalizeTests(unittest.TestCase):
    def test_new_file_gets_lf_without_bom(self):
        self.assertEqual(normalize("a\r\nb\rc\n", None), b"a\nb\nc\n")

    def test_new_file_drops_bom_from_text(self):
        self.assertEqual(normalize("\ufeffa\n", None), b"a\n")

    def test_existing_crlf_kept(self):
        self.assertEqual(normalize("x\ny\n", b"a\r\nb\r\n"), b"x\r\ny\r\n")

    def test_existing_lf_kept_for_crlf_input(self):
        self.assertEqual(normalize("x\r\ny\r\n", b"a\nb\n"), b"x\ny\n")

    def test_no_doubled_crlf(self):
        out = normalize("a\r\nb\r\n\r", b"q\r\n")
        self.assertEqual(out, b"a\r\nb\r\n\r\n")
        self.assertNotIn(b"\r\r", out)

    def test_mixed_existing_uses_dominant(self):
        self.assertEqual(normalize("x\ny", b"1\r\n2\r\n3\n"), b"x\r\ny")
        self.assertEqual(normalize("x\r\ny", b"1\n2\n3\r\n"), b"x\ny")

    def test_mixed_tie_goes_to_lf(self):
        self.assertEqual(normalize("x\r\ny", b"1\r\n2\n"), b"x\ny")

    def test_bom_kept(self):
        existing = BOM + b"a\r\n"
        self.assertEqual(normalize("z\n", existing), BOM + b"z\r\n")
        self.assertEqual(normalize("\ufeffz\n", existing), BOM + b"z\r\n")

    def test_bom_not_added(self):
        self.assertEqual(normalize("\ufeffz", b"a\n"), b"z")
        self.assertFalse(normalize("z", b"a\n").startswith(BOM))

    def test_existing_empty_file_uses_default(self):
        self.assertEqual(normalize("a\r\nb", b""), b"a\nb")
        self.assertEqual(normalize("a\nb", b"", default="crlf"), b"a\r\nb")

    def test_existing_without_newline_uses_default(self):
        self.assertEqual(normalize("a\nb", b"one line", default="crlf"), b"a\r\nb")

    def test_default_crlf_for_new_file(self):
        self.assertEqual(normalize("a\nb\r\n", None, default="crlf"), b"a\r\nb\r\n")

    def test_utf8_byte_exact(self):
        text = "\u00e4\u00f6\u00fc \u20ac\n"
        self.assertEqual(normalize(text, None), text.encode("utf-8"))

    def test_invalid_default_rejected(self):
        with self.assertRaises(ValueError):
            normalize("a", None, default="cr")

    def test_idempotent_on_own_output(self):
        first = normalize("a\nb\n", b"x\r\n")
        second = normalize(first.decode("utf-8"), first)
        self.assertEqual(first, second)


class ReportTests(unittest.TestCase):
    def test_report_on_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.txt")
            with open(path, "wb") as handle:
                handle.write(BOM + b"a\r\nb\nc\rd\r\n")
            info = report(path)
        self.assertEqual(info["path"], path)
        self.assertEqual(info["bytes"], 13)
        self.assertEqual(info["lf"], 1)
        self.assertEqual(info["crlf"], 2)
        self.assertEqual(info["cr"], 1)
        self.assertTrue(info["bom"])
        self.assertEqual(info["style"], "mixed")
        self.assertEqual(info["dominant"], "crlf")

    def test_report_on_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "empty.txt")
            open(path, "wb").close()
            info = eol_guard.report(path)
        self.assertEqual(info["bytes"], 0)
        self.assertFalse(info["bom"])
        self.assertEqual(info["style"], "none")
        self.assertEqual(info["dominant"], "lf")


if __name__ == "__main__":
    unittest.main()
