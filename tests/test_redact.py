import unittest

from notiontool.store import redact


class RedactTests(unittest.TestCase):
    def test_token_counts_stay_readable(self):
        fields = dict(estimated_tokens=48211, token_limit=60000, context_token_limit=60000.0,
                      usage=dict(max_tokens=4000))
        self.assertEqual(redact(fields), fields)

    def test_credentials_and_codes_stay_hidden(self):
        hidden = redact(dict(token='abc123', token_v2='v2-value', assignment_token='lease', code=123456,
                             tokens=['abc'], estimated_tokens='48211', password='pw', flag_tokens=True))
        self.assertEqual(set(hidden.values()), {'[redacted]'})
        self.assertEqual(redact(dict(status_code=200, error_code=7)), dict(status_code=200, error_code=7))


if __name__ == '__main__':
    unittest.main()
