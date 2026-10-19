import unittest

from benchmarks.dur_b import parse_duration


class ValidTests(unittest.TestCase):
    def check(self, cases):
        for text, want in cases:
            with self.subTest(text=text):
                got = parse_duration(text)
                self.assertEqual(got, want)
                self.assertIs(type(got), int)

    def test_examples(self):
        self.check([('1h30m', 5400), ('2d4h', 187200), ('45s', 45), ('1h 30m 10s', 5410)])

    def test_single_units(self):
        self.check([('1d', 86400), ('1h', 3600), ('1m', 60), ('1s', 1), ('0s', 0), ('0m', 0)])

    def test_all_units(self):
        self.check([('1d2h3m4s', 93784), ('1d 2h 3m 4s', 93784), ('1d4s', 86404), ('2h5s', 7205)])

    def test_spaces_between_parts(self):
        self.check([('1h  30m', 5400), ('1h\t30m', 5400), ('1d   1s', 86401)])

    def test_leading_trailing_whitespace(self):
        self.check([('  1h30m  ', 5400), ('\t45s\n', 45), (' 90 ', 90)])

    def test_case_insensitive(self):
        self.check([('1H30M', 5400), ('2D4h', 187200), ('45S', 45), ('1h 30M 10S', 5410)])

    def test_plain_integer_is_seconds(self):
        self.check([('90', 90), ('0', 0), ('007', 7), ('3600', 3600)])

    def test_large_values(self):
        self.check([('100000d', 8640000000), ('99999999999s', 99999999999)])

    def test_decimals(self):
        self.check([('1.5h', 5400), ('0.5m', 30), ('1.5d', 129600), ('2.25s', 2), ('1.5h 30m', 7200),
                    ('0.1s', 0), ('0.75s', 1)])

    def test_rounding_to_nearest(self):
        self.check([('1.4s', 1), ('1.6s', 2), ('0.5s', 1), ('1.5s', 2), ('2.5s', 3), ('0.01m', 1),
                    ('0.001h', 4), ('1.0000001s', 1)])


class ValueErrorTests(unittest.TestCase):
    def bad(self, *texts):
        for text in texts:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_duration(text)

    def test_empty_or_blank(self):
        self.bad('', ' ', '   ', '\t\n')

    def test_unknown_units(self):
        self.bad('5ms', '1h30ms', '3w', '2y', '10x', '1hr', '5sec', '2min', '1hm', '1µs')

    def test_repeated_units(self):
        self.bad('1h2h', '1h 2h', '1m1m', '1s1s', '1d1h1d', '1H2h')

    def test_wrong_order(self):
        self.bad('30m1h', '1s1m', '1h1d', '1m 1h', '10s5m', '1d1s1h')

    def test_negative_values(self):
        self.bad('-1h', '-5', '-30s', '1h-30m', '1h -30m', '- 1h')

    def test_number_without_unit_after_parts(self):
        self.bad('1h30', '1h 30', '2d4', '1.5h3', '1h30m10')

    def test_space_between_number_and_unit(self):
        self.bad('1 h', '1h 30 m', '45 s')

    def test_garbage(self):
        self.bad('abc', 'h', '1h abc', 'one hour', '1h,30m', '1h+30m', '+5s', '1..5h', '.5h', '1.h',
                 '1.5', '1e3s', 'h1', '1h_30m', '1h30m!', '٣s', '1 1')


class TypeErrorTests(unittest.TestCase):
    def test_non_str(self):
        for value in (None, 90, 1.5, b'1h', bytearray(b'1h'), True, ['1h'], object()):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    parse_duration(value)


if __name__ == '__main__':
    unittest.main()
