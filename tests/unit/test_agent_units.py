"""TASK-09/10 (unit): estimasi & kalender kerja, brief, kompaksi riwayat, rekonstruksi resume."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from mcp_marketing.models import RunStep
from mcp_marketing.schemas.contract import EstimationInput
from mcp_marketing.services.agent import brief
from mcp_marketing.services.agent.estimation import estimate, heuristic, schedule
from mcp_marketing.services.agent.memory import (
    compact_history,
    messages_from_steps,
    split_for_compaction,
)
from mcp_marketing.services.agent.profiles import DEFAULT_PROFILES
from mcp_marketing.services.agent.runtime import pinned_route
from mcp_marketing.utils.tokens import count_tokens
from tests.fakes import ScriptedLLM

TASK = {
    "id": "33333333-3333-4333-8333-333333333333",
    "project_id": "55555555-5555-4555-8555-555555555555",
    "title": "Generate Content Calendar",
    "status": "todo",
    "priority": "medium",
    "tags": ["content"],
}


def _inp(earliest: str, description: str = "", title: str = "Task") -> EstimationInput:
    return EstimationInput.model_validate(
        {
            "contract_version": "1.0",
            "run_id": "11111111-1111-4111-8111-111111111111",
            "agent_key": "strategist",
            "task": {**TASK, "title": title},
            "task_description": description,
            "earliest_start_date": earliest,
        }
    )


def test_heuristic_rules() -> None:
    assert heuristic("Posting", "").days == 1
    assert heuristic("Riset kompetitor", "").days == 2
    assert heuristic("Kalender konten 3 minggu", "").days == 4
    assert heuristic("Laporan bulanan", "x" * 3_100).days == 4
    assert heuristic("Task", "", subtask_count=3).days == 2.5


def test_schedule_friday_three_days() -> None:
    assert schedule(date(2026, 10, 30), 3) == (date(2026, 10, 30), date(2026, 11, 3))
    assert schedule(date(2026, 10, 31), 0.5) == (date(2026, 11, 2), date(2026, 11, 2))


async def test_estimate_without_llm_fallback_confidence() -> None:
    out, tokens = await estimate(_inp("2026-11-02", title="Riset pasar"), None)
    assert out.estimated_days == 2 and out.confidence == 0.4 and tokens == 0
    assert out.start_date == "2026-11-02" and out.end_date == "2026-11-03"


async def test_estimate_clamps_llm_range() -> None:
    llm = ScriptedLLM(json_reply={"estimated_days": 0.1, "confidence": 0.9, "message": "cepat"})
    out, _ = await estimate(_inp("2026-11-02", title="Kalender konten 4 minggu"), llm)
    assert out.estimated_days == 2.5  # heuristik 5 hari x 0,5


async def test_estimate_property_dates_valid_1000_cases() -> None:
    rng = random.Random(7)
    base = date(2026, 1, 1)
    for _ in range(1_000):
        earliest = base + timedelta(days=rng.randint(0, 700))
        days = rng.choice([0.5, 1, 2.5, 7, 30, 120])
        llm = ScriptedLLM(
            json_reply={"estimated_days": days, "confidence": rng.random(), "message": "ok"}
        )
        words = rng.choice(["", "riset", "kalender 6 minggu", "laporan analisis"])
        out, _ = await estimate(
            _inp(earliest.isoformat(), "x" * rng.randint(0, 6_000), f"Task {words}"), llm
        )
        start, end = date.fromisoformat(out.start_date), date.fromisoformat(out.end_date)
        assert start >= earliest and end >= start
        assert start.weekday() < 5 and end.weekday() < 5
        assert 0 <= (out.confidence or 0) <= 1


def test_brief_assignment_is_compact() -> None:
    context = [
        {"id": "h", "type": "heading", "level": 2, "text": "Brief"},
        {"id": "p1", "type": "paragraph", "text": "Kalimat pertama. Kalimat kedua panjang " * 40},
        {"id": "p2", "type": "paragraph", "text": "Paragraf lanjutan tidak masuk outline."},
    ]
    text = brief.user_brief(
        {
            "trigger": "assignment",
            "task": TASK,
            "description": "d" * 5_000,
            "subtasks": [{"id": "s1", "title": "Riset", "status": "todo"}],
            "context": context,
        }
    )
    assert "## Brief" in text and "Kalimat pertama." in text
    assert "Paragraf lanjutan" not in text
    assert "d" * 2_001 not in text
    assert "Riset [todo]" in text


def test_light_request_detection() -> None:
    assert brief.is_light_request(
        {"trigger": "prompt_block", "prompt_block": {"prompt": "Buat 3 caption"}}
    )
    assert not brief.is_light_request(
        {"trigger": "prompt_block", "prompt_block": {"prompt": "Riset tren kopi"}}
    )
    assert not brief.is_light_request(
        {"trigger": "prompt_block", "prompt_block": {"prompt": "x" * 501}}
    )
    assert not brief.is_light_request({"trigger": "assignment"})


def test_system_prompts_within_600_tokens() -> None:
    for p in DEFAULT_PROFILES:
        system = brief.system_message(p.system_prompt(), trigger="assignment")["content"]
        assert count_tokens(system) <= 600, p.agent_key


def _history(steps: int, tool_chars: int) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "brief"}]
    for i in range(steps):
        msgs.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {"name": "data_get", "arguments": '{"handle":"h_0123456789"}'},
                    }
                ],
            }
        )
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": f"c{i}",
                "content": f"hasil {i} h_abcdefghjk " + "data " * tool_chars,
            }
        )
    return msgs


async def test_compaction_30k_to_under_12k_keeps_handles() -> None:
    msgs = _history(12, 2_600)
    assert count_tokens(msgs) > 30_000
    llm = ScriptedLLM(compact_text="Ringkasan singkat langkah sebelumnya.")
    new, summary, tokens = await compact_history(llm, msgs, 2)
    assert count_tokens(new) < 12_000
    assert summary is not None and "h_0123456789" in summary and "h_abcdefghjk" in summary
    assert new[:2] == msgs[:2] and new[-4:] == msgs[-4:]  # 2 langkah terakhir utuh
    assert tokens == 240


async def test_compaction_uses_agent_route_when_given() -> None:
    llm = ScriptedLLM()
    await compact_history(llm, _history(6, 50), 2, route="openrouter/gemma-4-31b")
    await compact_history(llm, _history(6, 50), 2)
    assert [c.get("route_override") for c in llm.calls] == ["openrouter/gemma-4-31b", None]


def test_pinned_route_only_for_non_tier_routes() -> None:
    assert pinned_route("fast") is None and pinned_route("smart") is None
    assert pinned_route("openrouter/gemma-4-31b") == "openrouter/gemma-4-31b"
    assert pinned_route("smart-premium") == "smart-premium"


def test_split_requires_more_than_tail() -> None:
    middle, tail = split_for_compaction(_history(2, 1), 2)
    assert middle == [] and len(tail) == 4


def _row(step: int, role: str, content: dict) -> RunStep:
    return RunStep(run_id=None, step=step, role=role, content=content)


def test_messages_from_steps_drops_dangling_and_applies_summary() -> None:
    head = [{"role": "system", "content": "s"}, {"role": "user", "content": "b"}]
    rows = [
        _row(
            1,
            "assistant",
            {"content": None, "tool_calls": [{"id": "a", "name": "x", "arguments": {}}]},
        ),
        _row(2, "tool", {"tool_call_id": "a", "content": "r1"}),
        _row(3, "summary", {"text": "ringkas", "tail": [{"role": "user", "content": "tail"}]}),
        _row(4, "assistant", {"content": "ok", "tool_calls": []}),
        _row(
            5,
            "assistant",
            {"content": None, "tool_calls": [{"id": "z", "name": "y", "arguments": {"k": 1}}]},
        ),
    ]
    msgs = messages_from_steps(head, rows)
    assert msgs[2]["content"].startswith("[Ringkasan") and msgs[3] == {
        "role": "user",
        "content": "tail",
    }
    assert msgs[-1] == {"role": "assistant", "content": "ok"}  # assistant tanpa hasil tool dibuang


@pytest.mark.parametrize("trigger", ["assignment", "mention", "prompt_block"])
def test_system_message_variants(trigger: str) -> None:
    msg = brief.system_message("Peran", trigger=trigger)
    assert msg["role"] == "system" and "write_blocks" in msg["content"]
