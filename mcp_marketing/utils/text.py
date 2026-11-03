"""Helper teks aman-unicode."""

import re
import unicodedata

ELLIPSIS = "…"
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def truncate(text: str, max_chars: int) -> str:
    """Potong ke `max_chars` (termasuk elipsis) tanpa memecah grapheme gabungan."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    cut = max_chars - 1
    # mundur selama karakter berikutnya adalah combining mark / surrogate
    while cut > 0 and unicodedata.combining(text[cut]):
        cut -= 1
    return text[:cut].rstrip() + ELLIPSIS


def strip_control_chars(text: str) -> str:
    return _CONTROL_RE.sub("", text)


def normalize_ws(text: str) -> str:
    lines = [_WS_RE.sub(" ", line).strip() for line in text.replace("\r\n", "\n").split("\n")]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()
