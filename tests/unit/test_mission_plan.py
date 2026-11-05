"""Unit test validasi rencana misi (M3_TASK_013): murni Pydantic, tanpa DB/LLM."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from mcp_marketing.services.intel.mission_plan import (
    MAX_PLAN_ITEMS,
    finalize_plan,
    plan_model,
)

START, END = date(2026, 11, 10), date(2026, 11, 30)


def item(ref: str, start: str = "2026-11-10", end: str = "2026-11-12", **extra: Any) -> dict:
    return {"ref": ref, "title": f"Task {ref}", "start_date": start, "end_date": end, **extra}


def parse(tasks: list[dict], **extra: Any):
    return plan_model(START, END).model_validate({"summary": "Rencana", "tasks": tasks, **extra})


def test_rencana_valid_dengan_subtask():
    plan = parse([item("t1", subtasks=[item("s1", "2026-11-10", "2026-11-11")]), item("t2")])
    assert [t.ref for t in plan.tasks] == ["t1", "t2"]
    assert plan.tasks[0].subtasks[0].ref == "s1"


@pytest.mark.parametrize(
    ("tasks", "fragment"),
    [
        ([item("t1", "2026-11-09", "2026-11-12")], "di luar timeline"),
        ([item("t1", "2026-11-20", "2026-11-12")], "end_date"),
        ([item("t1"), item("t1")], "ref ganda"),
        ([item("t1", depends_on=["zz"])], "tidak dikenal"),
        ([item("t1", depends_on=["t2"]), item("t2", depends_on=["t1"])], "siklik"),
        ([item("t1", depends_on=["t1"])], "siklik"),
    ],
)
def test_rencana_tidak_valid_ditolak(tasks: list[dict], fragment: str):
    with pytest.raises(ValidationError, match=fragment):
        parse(tasks)


def test_rentang_task_induk_diperlebar_agar_mencakup_subtask():
    plan = parse(
        [
            item(
                "t1",
                "2026-11-12",
                "2026-11-13",
                subtasks=[
                    item("s1", "2026-11-11", "2026-11-11"),
                    item("s2", "2026-11-13", "2026-11-15"),
                ],
            )
        ]
    )
    assert (plan.tasks[0].start_date, plan.tasks[0].end_date) == (
        date(2026, 11, 11),
        END.replace(day=15),
    )


def test_jumlah_item_dibatasi():
    tasks = [
        item(f"t{i}", subtasks=[item(f"s{i}-{j}", "2026-11-10", "2026-11-10") for j in range(8)])
        for i in range(7)
    ]
    assert sum(1 + len(t["subtasks"]) for t in tasks) > MAX_PLAN_ITEMS
    with pytest.raises(ValidationError, match="maksimal"):
        parse(tasks)


def test_finalize_menambah_risiko_dependensi_dan_agent_default():
    plan = parse(
        [
            item("t1", "2026-11-10", "2026-11-15"),
            item("t2", "2026-11-12", "2026-11-14", depends_on=["t1"], executor="agent"),
        ]
    )
    final = finalize_plan(plan, default_agent_key="strategist")
    assert final.tasks[1].agent_key == "strategist"
    assert final.tasks[0].agent_key is None  # executor human
    assert [r.code for r in final.risks] == ["TIMELINE_TIGHT"]
    assert "t2" in final.risks[0].message
    # rencana asli tidak berubah (immutable)
    assert plan.tasks[1].agent_key is None and plan.risks == []


def test_finalize_mengganti_agent_yang_tidak_diizinkan_dengan_planner():
    plan = parse(
        [
            item("t1", executor="agent", agent_key="assistant"),
            item("t2", executor="agent", agent_key="researcher"),
        ]
    )
    final = finalize_plan(
        plan, default_agent_key="strategist", allowed_agents={"strategist", "researcher"}
    )
    assert [t.agent_key for t in final.tasks] == ["strategist", "researcher"]


def test_finalize_tanpa_konflik_tidak_menambah_risiko():
    plan = parse(
        [
            item("t1", "2026-11-10", "2026-11-12"),
            item("t2", "2026-11-13", "2026-11-14", depends_on=["t1"]),
            item("t3", "2026-11-14", "2026-11-15", depends_on=["t2"]),  # hari yang sama: wajar
        ]
    )
    assert finalize_plan(plan, default_agent_key="strategist").risks == []
