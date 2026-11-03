"""Prompt library berversi: file `<name>.<version>.md` dengan placeholder `{{var}}`.

Versi prompt dicatat di `llm_calls.purpose` sebagai `<purpose>@<version>` agar perubahan prompt
bisa dilacak terhadap pemakaian token.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

PROMPT_DIR = Path(__file__).resolve().parent
_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


@lru_cache(maxsize=64)
def load(name: str, version: str = "v1") -> str:
    path = PROMPT_DIR / f"{name}.{version}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt tidak ditemukan: {path.name}")
    return path.read_text(encoding="utf-8").strip()


def render(name: str, version: str = "v1", **variables: Any) -> str:
    """Isi `{{var}}`; variabel yang tidak diberikan -> KeyError (prompt tak boleh kosong)."""
    template = load(name, version)

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise KeyError(f"variabel prompt '{key}' tidak diisi untuk {name}.{version}")
        return str(variables[key])

    return _VAR_RE.sub(_sub, template)


def tag(purpose: str, version: str = "v1") -> str:
    return f"{purpose}@{version}"
