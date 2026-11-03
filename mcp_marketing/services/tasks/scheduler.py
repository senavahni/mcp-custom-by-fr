"""Deterministic weekday scheduling for LLM-proposed task breakdowns."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.utils.time import add_workdays, next_workday


def schedule_subtasks(
    subtasks: Sequence[Mapping[str, Any]],
    *,
    start_date: str,
    parent_end_date: str | None = None,
) -> list[dict[str, Any]]:
    try:
        start = next_workday(date.fromisoformat(start_date))
        parent_end = date.fromisoformat(parent_end_date) if parent_end_date else None
    except ValueError:
        raise ValidationFailed("Tanggal breakdown harus berformat YYYY-MM-DD") from None

    by_ref: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(subtasks):
        task = dict(raw)
        ref = task.get("client_ref")
        duration = task.get("duration_days")
        if not isinstance(ref, str) or not ref or ref in by_ref:
            raise ValidationFailed(f"client_ref tidak valid atau duplikat pada subtask {index}")
        if not isinstance(duration, int) or isinstance(duration, bool) or duration < 1:
            raise ValidationFailed(f"duration_days harus bilangan positif pada subtask {index}")
        dependencies = task.get("depends_on") or []
        if not isinstance(dependencies, list) or any(not isinstance(d, str) for d in dependencies):
            raise ValidationFailed(f"depends_on tidak valid pada subtask {index}")
        task["depends_on"] = dependencies
        by_ref[ref] = task

    for task in by_ref.values():
        missing = [ref for ref in task["depends_on"] if ref not in by_ref]
        if missing:
            raise ValidationFailed(f"depends_on merujuk client_ref yang tidak ada: {missing[0]}")

    ordered: list[dict[str, Any]] = []
    completed: dict[str, tuple[date, date]] = {}
    remaining = set(by_ref)
    while remaining:
        ready = [
            ref
            for ref in by_ref
            if ref in remaining and all(dep in completed for dep in by_ref[ref]["depends_on"])
        ]
        if not ready:
            raise ValidationFailed("Dependensi subtask siklik")
        for ref in ready:
            task = by_ref[ref]
            dependency_end = max((completed[dep][1] for dep in task["depends_on"]), default=start)
            task_start = next_workday(dependency_end if task["depends_on"] else start)
            if task["depends_on"]:
                task_start = next_workday(task_start.fromordinal(task_start.toordinal() + 1))
            task_end = add_workdays(task_start, task["duration_days"])
            completed[ref] = (task_start, task_end)
            task["start_date"] = task_start.isoformat()
            task["end_date"] = task_end.isoformat()
            task["outside_parent_range"] = parent_end is not None and task_end > parent_end
            ordered.append(task)
            remaining.remove(ref)
    return ordered
