"""Normalisasi teks sosial Indonesia/Inggris sebelum klasifikasi sentimen (TASK-20).

lowercase, URL -> `<url>`, mention -> `@user`, hashtag dipisah (`#KopiSusu` -> `kopi susu`),
huruf berulang dipendekkan (`bagusss` -> `bagus`), slang -> baku (`data/slang_id.tsv`), emoji ->
token sentimen (`emoji_pos` / `emoji_neg`). Hasil dipakai juga sebagai kunci cache (sha256).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

SLANG_FILE = Path(__file__).resolve().parent / "data" / "slang_id.tsv"
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_MENTION = re.compile(r"@\w{1,30}")
_HASHTAG = re.compile(r"#(\w+)")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
_REPEAT = re.compile(r"([a-z])\1{2,}")
_SPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"<url>|@user|emoji_\w+|[\w']+|[^\w\s]", re.UNICODE)

EMOJI_POS = "😀😃😄😁😆😊🙂😉😍🥰😘😋😎🤩🥳👍👌🙌👏💯❤🧡💛💚💙💜🤍💖💕✨🔥⭐🌟🎉😻🤤😇🤗💪🙏"
EMOJI_NEG = "😞😔😟😕🙁☹😣😖😫😩😢😭😤😠😡🤬🤮🤢👎💔😒🙄😑😐😓😰😱🤦😵🥲😪"


@lru_cache(maxsize=1)
def slang_table() -> tuple[dict[str, str], list[tuple[re.Pattern[str], str]]]:
    """`(kata_tunggal, frasa)`; frasa (beberapa kata) diganti lebih dulu."""
    single: dict[str, str] = {}
    phrases: list[tuple[re.Pattern[str], str]] = []
    for line in SLANG_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "\t" not in line:
            continue
        key, value = (part.strip().lower() for part in line.split("\t", 1))
        if " " in key:
            phrases.append((re.compile(rf"\b{re.escape(key)}\b"), value))
        else:
            single.setdefault(key, value)
    phrases.sort(key=lambda p: -len(p[0].pattern))
    return single, phrases


def split_hashtag(tag: str) -> str:
    return _CAMEL.sub(" ", tag).lower().replace("_", " ")


def _emoji(text: str) -> str:
    out = []
    for ch in text:
        if ch in EMOJI_POS:
            out.append(" emoji_pos ")
        elif ch in EMOJI_NEG:
            out.append(" emoji_neg ")
        elif ord(ch) >= 0x1F000 or 0x2600 <= ord(ch) <= 0x27BF:
            out.append(" ")  # emoji lain / variasi: dibuang
        else:
            out.append(ch)
    return "".join(out)


def normalize(text: str) -> str:
    single, phrases = slang_table()
    value = _URL.sub(" <url> ", text)
    value = _MENTION.sub(" @user ", value)
    value = _HASHTAG.sub(lambda m: f" {split_hashtag(m.group(1))} ", value)
    value = _emoji(value).lower()
    value = _REPEAT.sub(r"\1", value)
    for pattern, replacement in phrases:
        value = pattern.sub(replacement, value)
    tokens = [single.get(tok, tok) for tok in _TOKEN.findall(value)]
    return _SPACE.sub(" ", " ".join(tokens)).strip()
