"""Deteksi bahasa id/en/other (lingua, mode low-accuracy agar cepat untuk teks pendek).

Bahasa Melayu/Jawa/Sunda dipetakan ke `id` (campur kode umum di media sosial Indonesia);
teks sangat pendek atau tidak yakin dianggap `id` (bahasa dominan data).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

Lang = Literal["id", "en", "other"]
MIN_CHARS = 12
MIN_CONFIDENCE = 0.5
INDONESIAN_SHARE = 0.25


@lru_cache(maxsize=1)
def _detector() -> Any:
    from lingua import Language, LanguageDetectorBuilder

    languages = [
        Language.INDONESIAN,
        Language.MALAY,
        Language.ENGLISH,
        Language.JAVANESE if hasattr(Language, "JAVANESE") else Language.MALAY,
        Language.TAGALOG,
        Language.SPANISH,
        Language.PORTUGUESE,
        Language.FRENCH,
        Language.GERMAN,
        Language.DUTCH,
        Language.CHINESE,
        Language.JAPANESE,
        Language.KOREAN,
        Language.ARABIC,
        Language.THAI,
        Language.VIETNAMESE,
    ]
    return (
        LanguageDetectorBuilder.from_languages(*dict.fromkeys(languages))
        .with_low_accuracy_mode()
        .build()
    )


def detect(text: str) -> Lang:
    stripped = text.strip()
    if len(stripped) < MIN_CHARS:
        return "id"
    from lingua import Language

    values = _detector().compute_language_confidence_values(stripped)
    if not values:
        return "id"
    indonesian = sum(
        v.value
        for v in values
        if v.language in (Language.INDONESIAN, Language.MALAY)
        or v.language.name in ("JAVANESE", "SUNDANESE")
    )
    # slang/campur kode menurunkan keyakinan lingua; porsi kecil Indonesia/Melayu sudah cukup
    if indonesian >= INDONESIAN_SHARE or values[0].value < MIN_CONFIDENCE:
        return "id"
    return "en" if values[0].language == Language.ENGLISH else "other"
