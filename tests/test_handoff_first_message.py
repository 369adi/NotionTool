"""Tests for handoff_files.first_message: resume sentence, length limit, full token and path."""
import unittest

from notiontool import handoff_files as hf

RESUME = ("After the token, wait for the user; at the first user message "
          "follow resume_instructions from the file.")
TOKEN = "a1b2c3d4"


def long_path(length=140):
    head = "C:/Users/liket/Downloads/NotionTool-harness/NotionTool/data/handoffs/"
    tail = "main-971f.json"
    path = head + "x" * (length - len(head) - len(tail)) + tail
    assert len(path) == length
    return path


class FirstMessageTest(unittest.TestCase):
    def setUp(self):
        self.path = long_path()
        self.message = hf.first_message(dict(path=self.path, token=TOKEN))
        self.lines = self.message.split("\n")

    def test_path_is_140_chars(self):
        self.assertEqual(len(self.path), 140)

    def test_sentence_comes_right_after_token_line(self):
        index = self.lines.index(TOKEN)
        self.assertEqual(self.lines[index + 1], RESUME)

    def test_sentence_appears_once_and_after_token(self):
        self.assertEqual(self.message.count(RESUME), 1)
        self.assertGreater(self.message.index(RESUME), self.message.index("\n" + TOKEN + "\n"))

    def test_length_under_limit_for_140_char_path(self):
        self.assertLess(len(self.message), hf.MAX_FIRST_MESSAGE)

    def test_token_and_path_appear_in_full(self):
        self.assertIn(self.path, self.message)
        self.assertIn(TOKEN, self.lines)
        self.assertTrue(any(line.endswith(self.path) for line in self.lines))

    def test_token_stays_alone_on_its_own_line(self):
        self.assertEqual(self.lines.count(TOKEN), 1)

    def test_ack_check_unchanged(self):
        self.assertTrue(hf.verify_ack("Done.\n" + TOKEN, TOKEN))
        self.assertFalse(hf.verify_ack("Done.\n" + TOKEN + "ff", TOKEN))

    def test_missing_path_or_token_still_raises(self):
        with self.assertRaises(ValueError):
            hf.first_message(dict(path=self.path, token=""))
        with self.assertRaises(ValueError):
            hf.first_message(dict(path="", token=TOKEN))


if __name__ == "__main__":
    unittest.main()
