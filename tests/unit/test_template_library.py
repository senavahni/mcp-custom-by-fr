from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mcp_marketing.core.errors import NotFound, ValidationFailed
from mcp_marketing.services.templates import library


def _template(key: str = "sample") -> dict[str, object]:
    return {
        "key": key,
        "name": "Sample",
        "category": "custom",
        "variables": [],
        "blocks": {"md": "## Goal\nWrite the brief"},
        "subtasks": [],
    }


def _session(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "get": AsyncMock(return_value=None),
        "scalar": AsyncMock(return_value=None),
        "scalars": AsyncMock(),
        "add": Mock(),
        "flush": AsyncMock(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_seed_templates_creates_missing_seed_and_skips_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_path = tmp_path / "sample.yaml"
    seed_path.write_text(
        "key: sample\nname: Sample\ncategory: custom\n"
        "blocks:\n  md: |\n    ## Goal\n    Write the brief\nsubtasks: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(library, "SEED_DIR", tmp_path)
    session = _session()
    library._list_cache.clear()

    seeded = await library.seed_templates(session)

    assert seeded == 1
    assert session.add.call_count == 2
    session.flush.assert_awaited_once()

    existing_session = _session(get=AsyncMock(return_value=object()))
    assert await library.seed_templates(existing_session) == 0
    assert existing_session.add.call_count == 0


@pytest.mark.asyncio
async def test_list_templates_returns_metadata_and_uses_copy_safe_cache() -> None:
    library._list_cache.clear()
    row = SimpleNamespace(
        key="sample",
        name="Sample",
        category="custom",
        description="brief",
        origin="seed",
        current_version=1,
        workspace_id=None,
        is_active=True,
        created_by_client=None,
    )
    result = SimpleNamespace(all=lambda: [row])
    session = _session(scalars=AsyncMock(return_value=result))

    first = await library.list_templates(session, category="custom")
    first[0]["name"] = "mutated"
    second = await library.list_templates(session, category="custom")

    assert second[0]["name"] == "Sample"
    assert second[0]["key"] == "sample"
    session.scalars.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_template_requires_active_template_and_current_version() -> None:
    row = SimpleNamespace(
        key="sample",
        name="Sample",
        category="custom",
        description=None,
        origin="seed",
        current_version=2,
    )
    version = SimpleNamespace(
        variables=[],
        blocks={"md": "## Goal"},
        subtasks=[],
        outline={"sections": []},
        version=2,
    )
    session = _session(
        scalar=AsyncMock(return_value=row),
        get=AsyncMock(return_value=version),
    )

    template = await library.get_template(session, "sample")

    assert template["version"] == 2
    assert template["blocks"] == {"md": "## Goal"}

    missing_version = _session(
        scalar=AsyncMock(return_value=row),
        get=AsyncMock(return_value=None),
    )
    with pytest.raises(NotFound, match="Versi template"):
        await library.get_template(missing_version, "sample")

    missing_template = _session(scalar=AsyncMock(return_value=None))
    with pytest.raises(NotFound, match="Template tidak ditemukan"):
        await library.get_template(missing_template, "missing")


@pytest.mark.asyncio
async def test_save_template_creates_versions_and_rejects_unsafe_overwrites() -> None:
    library._list_cache.clear()
    session = _session()
    assert (
        await library.save_template(
            session,
            _template(),
            client_id="client-1",
            workspace_id=None,
        )
        == 1
    )
    assert session.add.call_count == 2
    session.flush.assert_awaited_once()

    existing = SimpleNamespace(
        workspace_id="workspace-1",
        created_by_client="client-1",
        origin="client",
        current_version=1,
        category="custom",
    )
    update_session = _session(get=AsyncMock(return_value=existing))
    assert (
        await library.save_template(
            update_session,
            _template(),
            client_id="client-1",
            workspace_id="workspace-1",
        )
        == 2
    )
    assert existing.current_version == 2

    invalid_cases = [
        (
            _template("seed"),
            "client-1",
            None,
            SimpleNamespace(
                workspace_id=None,
                created_by_client="client-1",
                origin="seed",
                current_version=1,
                category="custom",
            ),
        ),
        (_template("private"), None, None, None),
    ]
    for template, client_id, workspace_id, existing_row in invalid_cases:
        rejected = _session(get=AsyncMock(return_value=existing_row))
        with pytest.raises(ValidationFailed):
            await library.save_template(
                rejected,
                template,
                client_id=client_id,
                workspace_id=workspace_id,
            )

    with pytest.raises(ValidationFailed, match="key wajib"):
        await library.save_template(
            session,
            {"key": ""},
            client_id="client-1",
            workspace_id=None,
        )


def test_seed_outline_counts_section_content_and_subtask_metadata() -> None:
    outline = library._seed_outline(
        {
            "key": "sample",
            "name": "Sample",
            "blocks": {"md": "## Goal\nOne\n\n## Plan\nTwo"},
            "subtasks": [{"client_ref": "step-1", "title": "Step 1"}],
        }
    )

    assert outline["sections"] == [
        {"heading": "Goal", "block_count": 1},
        {"heading": "Plan", "block_count": 1},
    ]
    assert outline["subtasks"][0]["duration_days"] == 1
