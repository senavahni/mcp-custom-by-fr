from __future__ import annotations

import pytest

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.services.tasks.scheduler import schedule_subtasks


def test_schedules_dependent_tasks_on_workdays() -> None:
    scheduled = schedule_subtasks(
        [
            {"client_ref": "research", "title": "Riset", "duration_days": 2},
            {
                "client_ref": "draft",
                "title": "Draft",
                "duration_days": 1,
                "depends_on": ["research"],
            },
        ],
        start_date="2026-11-05",
        parent_end_date="2026-11-11",
    )

    assert [(task["start_date"], task["end_date"]) for task in scheduled] == [
        ("2026-11-05", "2026-11-06"),
        ("2026-11-09", "2026-11-09"),
    ]
    assert all(not task["outside_parent_range"] for task in scheduled)


def test_marks_schedule_outside_parent_range() -> None:
    scheduled = schedule_subtasks(
        [{"client_ref": "late", "title": "Task", "duration_days": 3}],
        start_date="2026-11-05",
        parent_end_date="2026-11-06",
    )

    assert scheduled[0]["end_date"] == "2026-11-09"
    assert scheduled[0]["outside_parent_range"] is True


def test_rejects_cyclic_or_unknown_dependencies() -> None:
    with pytest.raises(ValidationFailed, match="siklik"):
        schedule_subtasks(
            [
                {"client_ref": "a", "title": "A", "duration_days": 1, "depends_on": ["b"]},
                {"client_ref": "b", "title": "B", "duration_days": 1, "depends_on": ["a"]},
            ],
            start_date="2026-11-05",
        )

    with pytest.raises(ValidationFailed, match="depends_on"):
        schedule_subtasks(
            [{"client_ref": "a", "title": "A", "duration_days": 1, "depends_on": ["missing"]}],
            start_date="2026-11-05",
        )
