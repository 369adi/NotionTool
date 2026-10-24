import pytest

from slugify import slugify


def test_example():
    assert slugify("Ärger & Co. 2026!") == "aerger-co-2026"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Äpfel", "aepfel"),
        ("Öl", "oel"),
        ("Übung", "uebung"),
        ("Straße", "strasse"),
        ("müßig öde", "muessig-oede"),
    ],
)
def test_umlauts(text, expected):
    assert slugify(text) == expected


def test_lowercase():
    assert slugify("Hello World") == "hello-world"


def test_collapses_separators():
    assert slugify("a   --  b!!!c") == "a-b-c"


def test_no_hyphen_at_start_or_end():
    assert slugify("  --Hallo--  ") == "hallo"
    assert slugify("!!!x???") == "x"


def test_max_length_60():
    assert len(slugify("a" * 200)) == 60


def test_no_trailing_hyphen_after_truncation():
    result = slugify("a" * 59 + " bbb")
    assert result == "a" * 59
    assert not result.endswith("-")


def test_empty_and_symbols_only():
    assert slugify("") == ""
    assert slugify("&!?") == ""


def test_digits_kept():
    assert slugify("Version 2.0") == "version-2-0"
