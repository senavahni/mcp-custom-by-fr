from __future__ import annotations

from datetime import date

import pytest

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.services.templates.renderer import render


def _template(md: str = "## Tujuan\n{{ brand|upper }}") -> dict:
    return {
        "key": "test_template",
        "name": "Test",
        "category": "campaign",
        "variables": [{"name": "brand", "type": "string", "required": True}],
        "blocks": {"md": md},
        "subtasks": [
            {
                "client_ref": "phase-1",
                "title": "Fase {{ brand|title }}",
                "offset_days": 0,
                "duration_days": 2,
                "md": "## Checklist\n- [ ] Siapkan brief",
                "children": [],
            }
        ],
    }


def test_render_expands_markdown_and_schedules_subtask() -> None:
    result = render(_template(), {"brand": "kopi nusantara"}, date(2026, 11, 6))

    assert result.blocks[0]["block_type"] == "heading"
    assert result.subtasks[0]["title"] == "Fase Kopi Nusantara"
    assert result.subtasks[0]["start_date"] == "2026-11-06"
    assert result.subtasks[0]["end_date"] == "2026-11-09"
    assert result.outline["subtasks"][0]["client_ref"] == "phase-1"


def test_render_rejects_missing_variables_and_sandbox_attribute_access() -> None:
    with pytest.raises(ValidationFailed, match="brand"):
        render(_template(), {}, date(2026, 11, 6))

    with pytest.raises(ValidationFailed):
        render(_template("{{ ''.__class__ }}"), {"brand": "safe"}, date(2026, 11, 6))


def test_render_rejects_tree_that_exceeds_dashboard_depth() -> None:
    template = _template()
    template["subtasks"][0]["children"] = [
        {
            "client_ref": "nested",
            "title": "Nested",
            "offset_days": 0,
            "duration_days": 1,
            "children": [],
        }
    ]

    with pytest.raises(ValidationFailed, match="kedalaman"):
        render(template, {"brand": "safe"}, date(2026, 11, 6), target_depth=2)
