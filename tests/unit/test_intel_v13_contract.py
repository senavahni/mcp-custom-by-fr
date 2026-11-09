"""Kontrak 1.3 (MCP-V1-00): fixture bersama `contracts/1.3/` vs model Pydantic.

Fixture yang sama divalidasi Zod di repo Dashboard (DSH-V1-00). Kontrak 1.2 tetap diuji oleh
`test_intel_contract.py` dan tidak boleh berubah.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from typing import Any, get_args

import orjson
import pytest

from mcp_marketing.core.errors import CaptchaRequired, ErrorCode, LocalModelUnavailable
from mcp_marketing.schemas import intel as v12
from mcp_marketing.schemas import intel_v13 as v13

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "1.3"


def _exporter() -> Any:
    spec = importlib.util.spec_from_file_location(
        "export_intel_v13_fixtures", ROOT / "scripts" / "export_intel_v13_fixtures.py"
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
    assert on_disk == generated, "jalankan: python scripts/export_intel_v13_fixtures.py"


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


def test_every_kind_and_new_event_has_valid_fixture() -> None:
    fixtures = [_load(f) for f in FIXTURE_FILES]
    kinds = {
        fx["payload"]["params"]["kind"]
        for fx in fixtures
        if fx["valid"] and fx["schema"] == "job_create_input"
    }
    events = {
        fx["payload"]["event_type"] for fx in fixtures if fx["valid"] and fx["schema"] == "event"
    }
    assert kinds == set(v13.JOB_KINDS)
    assert set(v13.EVENT_TYPES_V13) <= events


def test_kind_stages_cover_every_kind_with_known_stages() -> None:
    assert set(v13.KIND_STAGES) == set(v13.JOB_KINDS)
    known = set(get_args(v13.Stage13))
    for stages in v13.KIND_STAGES.values():
        assert set(stages) <= known


def test_new_error_codes_registered() -> None:
    assert set(v13.ERROR_CODES_V13) <= set(get_args(ErrorCode))
    assert CaptchaRequired("x").to_structured()["retryable"] is True
    assert LocalModelUnavailable("x").code == "LOCAL_MODEL_UNAVAILABLE"


def test_v12_contract_unchanged() -> None:
    """1.2 tetap menolak `kind`/`params` dan event v1 (perilaku lama tidak berubah)."""
    assert v12.CONTRACT_VERSION == "1.2"
    assert "intel.job.paused" not in v12.INTEL_EVENT_TYPES
    with pytest.raises(ValueError):
        v12.validate_intel_event_data("intel.job.paused", {})
    assert "params" not in v12.JobCreateIn.model_fields


def test_v13_event_validator_accepts_v12_events() -> None:
    out = v13.validate_event_data_v13("intel.job.started", {})
    assert out == {}
    with pytest.raises(ValueError):
        v13.validate_event_data_v13("intel.job.exploded", {})
