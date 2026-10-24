import re
import unicodedata

_UMLAUTS = {
    "ä": "ae", "ö": "oe", "ü": "ue",
    "Ä": "ae", "Ö": "oe", "Ü": "ue",
    "ß": "ss", "ẞ": "ss",
}

MAX_LENGTH = 60


def slugify(text):
    for char, repl in _UMLAUTS.items():
        text = text.replace(char, repl)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return text[:MAX_LENGTH].strip("-")
