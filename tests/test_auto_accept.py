import os
import tempfile
import time
import unittest

from notiontool import auto_accept as aa

PASSING = (
    "import unittest\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_one(self):\n"
    "        self.assertTrue(True)\n\n"
    "    def test_two(self):\n"
    "        self.assertEqual(1 + 1, 2)\n"
)
FAILING = (
    "import unittest\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_bad(self):\n"
    "        self.assertEqual(1, 2)\n"
)
SLEEPING = (
    "import time\nimport unittest\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_sleep(self):\n"
    "        time.sleep(30)\n"
)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def write(self, rel, data):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if isinstance(data, str):
            data = data.encode("utf-8")
        with open(path, "wb") as handle:
            handle.write(data)
        return rel

    def find(self, result, name, path=None):
        found = [c for c in result["checks"] if c["name"] == name and (path is None or c["path"] == path)]
        self.assertEqual(len(found), 1, "expected one %s check, got %r" % (name, result["checks"]))
        return found[0]

    def names(self, result):
        return [c["name"] for c in result["checks"]]


class CheckPartTests(Base):
    def test_all_pass(self):
        files = [self.write("pkg/mod.py", "X = 1\n"), self.write("fake_ok_tests.py", PASSING)]
        result = aa.check_part(files, ["fake_ok_tests"], files, self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["failures"], [])
        self.assertEqual(set(self.names(result)), {"scope", "bom", "line_endings", "py_compile", "unittest"})
        detail = self.find(result, "unittest")["detail"]
        self.assertIn("Ran 2 tests", detail)
        self.assertIn("OK", detail)

    def test_syntax_error(self):
        rel = self.write("bad.py", "def f(:\n    pass\n")
        result = aa.check_part([rel], [], [rel], self.root)
        self.assertFalse(result["ok"])
        check = self.find(result, "py_compile")
        self.assertFalse(check["ok"])
        self.assertTrue(check["detail"].startswith("line 1"), check["detail"])
        self.assertEqual(len(result["failures"]), 1)
        self.assertTrue(result["failures"][0].startswith("bad.py: py_compile: line 1"))

    def test_failing_test_module(self):
        self.write("fake_fail_tests.py", FAILING)
        result = aa.check_part([], ["fake_fail_tests"], [], self.root)
        self.assertFalse(result["ok"])
        detail = self.find(result, "unittest")["detail"]
        self.assertIn("Ran 1 test", detail)
        self.assertIn("FAILED (failures=1)", detail)
        self.assertIn("FAIL: test_bad", detail)
        self.assertTrue(result["failures"][0].startswith("fake_fail_tests: unittest: "))

    def test_missing_test_module(self):
        result = aa.check_part([], ["does_not_exist_tests"], [], self.root)
        self.assertFalse(result["ok"])
        detail = self.find(result, "unittest")["detail"]
        self.assertIn("FAILED", detail)
        self.assertIn("ERROR:", detail)

    def test_timeout_kills_the_run(self):
        self.write("fake_sleep_tests.py", SLEEPING)
        started = time.monotonic()
        result = aa.check_part([], ["fake_sleep_tests"], [], self.root, timeout_s=2)
        elapsed = time.monotonic() - started
        self.assertFalse(result["ok"])
        self.assertEqual(self.find(result, "unittest")["detail"], "timed out after 2 s")
        self.assertLess(elapsed, 20)

    def test_bom(self):
        rel = self.write("b.py", b"\xef\xbb\xbfX = 1\n")
        result = aa.check_part([rel], [], [rel], self.root)
        self.assertFalse(result["ok"])
        self.assertFalse(self.find(result, "bom")["ok"])
        self.assertTrue(self.find(result, "py_compile")["ok"])
        self.assertEqual(result["failures"], ["b.py: bom: UTF-8 BOM found, remove it"])

    def test_crlf_in_lf_file(self):
        rel = self.write("c.py", b"X = 1\r\nY = 2\r\n")
        result = aa.check_part([rel], [], [rel], self.root)
        self.assertFalse(result["ok"])
        check = self.find(result, "line_endings")
        self.assertEqual(check["detail"], "expected LF, found 2 CRLF, 0 LF, 0 CR")

    def test_crlf_expectation_passes(self):
        rel = self.write("sub/c.py", b"X = 1\r\nY = 2\r\n")
        result = aa.check_part([rel], [], [rel], self.root, expected_eol={"sub\\c.py": "CRLF"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.find(result, "line_endings")["detail"], "expected CRLF, found 2 CRLF, 0 LF, 0 CR")

    def test_mixed_line_endings(self):
        rel = self.write("m.txt", b"a\r\nb\nc\r")
        as_lf = aa.check_part([rel], [], [rel], self.root)
        as_crlf = aa.check_part([rel], [], [rel], self.root, expected_eol={rel: "crlf"})
        self.assertEqual(self.find(as_lf, "line_endings")["detail"], "expected LF, found 1 CRLF, 1 LF, 1 CR")
        self.assertFalse(as_lf["ok"])
        self.assertFalse(as_crlf["ok"])
        self.assertNotIn("py_compile", self.names(as_lf))

    def test_file_outside_allowed_files(self):
        files = [self.write("a.py", "A = 1\n"), self.write("b.py", "B = 1\n")]
        result = aa.check_part(files, [], ["a.py"], self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(self.find(result, "scope", "a.py")["ok"])
        self.assertFalse(self.find(result, "scope", "b.py")["ok"])
        self.assertEqual(result["failures"], ["b.py: scope: not in allowed_files"])

    def test_backslash_and_case_paths(self):
        self.write("pkg/Mod.py", "X = 1\n")
        result = aa.check_part(["pkg\\Mod.py"], [], ["./PKG/mod.py"], self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.find(result, "scope")["path"], "pkg/Mod.py")

    def test_absolute_path_and_duplicates(self):
        self.write("a.py", "A = 1\n")
        files = [os.path.join(self.root, "a.py"), "a.py", ".\\a.py"]
        result = aa.check_part(files, [], ["a.py"], self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.names(result).count("scope"), 1)

    def test_missing_file(self):
        result = aa.check_part(["nope.py"], [], ["nope.py"], self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(self.find(result, "exists")["detail"], "file not found under root")
        self.assertNotIn("bom", self.names(result))
        self.assertEqual(result["failures"], ["nope.py: exists: file not found under root"])

    def test_no_test_modules(self):
        rel = self.write("a.py", "A = 1\n")
        result = aa.check_part([rel], [], [rel], self.root)
        self.assertTrue(result["ok"])
        self.assertNotIn("unittest", self.names(result))

    def test_nothing_given(self):
        result = aa.check_part([], [], [], self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"], ["(part): inputs: no files and no test modules given"])

    def test_invalid_expected_eol(self):
        rel = self.write("a.py", "A = 1\n")
        with self.assertRaises(ValueError):
            aa.check_part([rel], [], [rel], self.root, expected_eol={rel: "cr"})

    def test_no_bytecode_written_into_root(self):
        files = [self.write("a.py", "A = 1\n"), self.write("fake_ok_tests.py", PASSING)]
        aa.check_part(files, ["fake_ok_tests"], files, self.root)
        self.assertFalse(os.path.exists(os.path.join(self.root, "__pycache__")))

    def test_failure_text_and_fix_prompt(self):
        files = [self.write("x.py", b"X = 1\r\n"), self.write("y.py", "Y = 1\n")]
        result = aa.check_part(files, [], ["x.py"], self.root)
        self.assertEqual(result["failures"], [
            "x.py: line_endings: expected LF, found 1 CRLF, 0 LF, 0 CR",
            "y.py: scope: not in allowed_files",
        ])
        prompt = aa.fix_prompt(result).split("\n")
        self.assertEqual(prompt[0], "The automatic acceptance gate failed. Fix these problems, then run the checks again:")
        self.assertEqual(prompt[1:], ["- " + item for item in result["failures"]])
        passed = aa.check_part(["y.py"], [], ["y.py"], self.root)
        self.assertEqual(aa.fix_prompt(passed), "All automatic checks passed. Hand the part to the reviewer.")


if __name__ == "__main__":
    unittest.main()
