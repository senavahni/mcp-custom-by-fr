"""Kontrak 1.2 (toolset `intel`): fixture bersama `contracts/intel-1.2/` vs model Pydantic.

Fixture yang sama divalidasi Zod di repo Dashboard (M3_TASK_000). Test ini juga memastikan fixture
di disk = hasil generator saat ini (mencegah schema berubah tanpa memperbarui fixture).
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from typing import Any

import orjson
import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "intel-1.2"


def _exporter() -> Any:
    spec = importlib.util.spec_from_file_location(
        "export_intel_fixtures", ROOT / "scripts" / "export_intel_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPORTER = _exporter()
FIXTURE_FILES = sorted((CONTRACT / "fixtures").glob("*.json"))


def _load(path: Path) -> dict[str, Any]:
    return orjson.loads(path.read_bytes())  # type: ignore[no-any-return]


def test_fixtures_up_to_date_with_generator() -> None:
    on_disk = {f.stem: _load(f) for f in FIXTURE_FILES}
    generated = {fx["name"]: orjson.loads(orjson.dumps(fx)) for fx in EXPORTER.build()}
    assert on_disk == generated, "jalankan: python scripts/export_intel_fixtures.py"


def test_checksums_match() -> None:
    expected = {}
    for line in (CONTRACT / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        expected[name] = digest
    actual = {
        f"fixtures/{f.name}": hashlib.sha256(f.read_bytes()).hexdigest() for f in FIXTURE_FILES
    }
    assert actual == expected


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_fixture_against_pydantic(path: Path) -> None:
    fx = _load(path)
    errors = EXPORTER.check(fx)
    if fx["valid"]:
        assert errors == []
    else:
        assert errors, "fixture invalid harus ditolak"
        assert any(e.startswith(fx["expect_path"]) for e in errors), errors


def test_both_kinds_present() -> None:
    kinds = {(_load(f)["schema"], _load(f)["valid"]) for f in FIXTURE_FILES}
    assert ("job_spec", True) in kinds and ("job_spec", False) in kinds
    assert ("event", True) in kinds and ("event", False) in kinds
