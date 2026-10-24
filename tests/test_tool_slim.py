import unittest

from notiontool.chat_context import TOOL_FIELD_CAP, estimate_tokens, omitted_tokens, slim_entity


class ToolSlimTests(unittest.TestCase):
    def test_big_tool_output_is_shortened_but_still_counted(self):
        big = 'x' * 300000
        entity = dict(id='e1', kind='tool', sequence=3, text=big, result={'out': big})
        slim = slim_entity(entity)
        self.assertLess(len(slim['text']), TOOL_FIELD_CAP + 64)
        self.assertIsInstance(slim['result'], str)
        self.assertGreater(slim['omitted_tokens'], estimate_tokens(big))
        self.assertEqual(entity['text'], big)  # the input is never changed
        self.assertEqual(omitted_tokens([slim]), slim['omitted_tokens'])

    def test_shortening_is_stable_and_other_kinds_stay(self):
        slim = slim_entity(dict(id='e1', kind='tool', text='y' * 100000))
        self.assertEqual(slim_entity(slim), slim)
        message = dict(id='m', kind='user_message', text='z' * 100000)
        self.assertIs(slim_entity(message), message)


if __name__ == '__main__':
    unittest.main()
