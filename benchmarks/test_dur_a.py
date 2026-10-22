import unittest

from benchmarks.dur_a import parse_duration


class ValidDurationTests(unittest.TestCase):
    def check(self, cases):
        for text, seconds in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text), seconds)
                self.assertIsInstance(parse_duration(text), int)

    def test_examples(self):
        self.check([('1h30m', 5400), ('2d4h', 187200), ('45s', 45), ('1h 30m 10s', 5410)])

    def test_every_unit(self):
        self.check([('1d', 86400), ('1h', 3600), ('1m', 60), ('1s', 1), ('1d2h3m4s', 93784),
                    ('1d 1s', 86401), ('2h5s', 7205)])

    def test_case_insensitive(self):
        self.check([('1H30M', 5400), ('2D4h', 187200), ('45S', 45), ('1h 30M 10s', 5410)])

    def test_plain_integer_is_seconds(self):
        self.check([('90', 90), ('0', 0), ('007', 7), (' 3600 ', 3600)])

    def test_decimals_round_to_nearest(self):
        self.check([('1.5h', 5400), ('1.25m', 75), ('0.5s', 1), ('0.4s', 0), ('2.5s', 3),
                    ('0.1d', 8640), ('1h 0.5m', 3630), ('0.0001s', 0)])

    def test_whitespace(self):
        self.check([(' 1h ', 3600), ('\t2m\n', 120), ('1h  30m', 5400), ('1h\t30m', 5400)])

    def test_zero_parts(self):
        self.check([('0s', 0), ('0h0m', 0)])


class InvalidDurationTests(unittest.TestCase):
    def refuse(self, cases):
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_duration(text)

    def test_empty_and_blank(self):
        self.refuse(['', ' ', '\t\n'])

    def test_unknown_units(self):
        self.refuse(['5ms', '1x', '2w', '3y', '1hm', '10sec', '1min'])

    def test_repeated_units(self):
        self.refuse(['1h2h', '1h 2h', '5s5s', '1d2D'])

    def test_wrong_order(self):
        self.refuse(['30m1h', '10s 1m', '1h1d', '1s1d'])

    def test_negative_values(self):
        self.refuse(['-5', '-1h', '1h-30m', '1h -30m'])

    def test_number_without_unit_after_parts(self):
        self.refuse(['1h30', '1h 30', '1d2h3'])

    def test_space_between_number_and_unit(self):
        self.refuse(['1 h', '1h 30 m'])

    def test_garbage(self):
        self.refuse(['abc', 'h', 'h1', '1h,30m', '1..5h', '.5h', '1.h', '1.5', '+5', '1h!', '1_000s'])


class TypeTests(unittest.TestCase):
    def test_non_str_input(self):
        for value in (None, 5, 1.5, b'1h', True, ['1h']):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    parse_duration(value)


if __name__ == '__main__':
    unittest.main()
