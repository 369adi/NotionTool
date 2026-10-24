import pytest

from durations import parse


def test_hours_and_minutes():
    assert parse("1h30m") == 5400


def test_days():
    assert parse("2d") == 172800


def test_seconds():
    assert parse("45s") == 45


def test_minutes():
    assert parse("10m") == 600


def test_all_units():
    assert parse("1d2h3m4s") == 86400 + 7200 + 180 + 4


def test_uppercase_and_whitespace():
    assert parse(" 1H30M ") == 5400


def test_zero():
    assert parse("0s") == 0


def test_repeated_unit_adds_up():
    assert parse("30m30m") == 3600


@pytest.mark.parametrize("bad", ["", "   ", "abc", "10", "h", "1x", "1h 30m", "1h-5m", "1.5h", "1hfoo"])
def test_invalid_raises(bad):
    with pytest.raises(ValueError):
        parse(bad)


def test_non_string_raises():
    with pytest.raises(ValueError):
        parse(None)
