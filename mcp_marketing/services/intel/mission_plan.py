"""Rencana misi agent (M3_TASK_013): skema rencana, validasi deterministik, dan panggilan LLM.

Rencana = task (depth 2) + subtask (depth 3) di bawah task induk misi milik Dashboard.
Validasi tanggal/ref/siklus berada di model Pydantic agar ikut loop perbaikan `chat_json`.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

import orjson
from pydantic import BaseModel, Field, field_validator, model_validator

from mcp_marketing.core.errors import UpstreamError
from mcp_marketing.services.agent.budget import BudgetScope
from mcp_marketing.services.llm import prompts

PROMPT = "mission_plan"
PROMPT_VERSION = "v1"
MAX_TASKS = 15
MAX_SUBTASKS = 8
MAX_PLAN_ITEMS = 60
RETRY_NOTE = (
    "PERHATIAN: jawaban sebelumnya ditolak validasi: {reason}. "
    "Susun ulang rencana LENGKAP yang memenuhi semua ketentuan, hanya JSON."
)

Priority = Literal["none", "low", "medium", "high", "urgent"]
RiskCode = Literal["TIMELINE_TIGHT", "LOW_DATA", "OUT_OF_SCOPE", "OTHER"]


class PlanSubtask(BaseModel):
    ref: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2_000)
    start_date: date
    end_date: date
    priority: Priority = "medium"
    tags: list[str] = Field(default_factory=list, max_length=5)
    executor: Literal["agent", "human"] = "human"
    agent_key: str | None = Field(default=None, max_length=100)
    depends_on: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("ref", "title")
    @classmethod
    def _strip(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("tidak boleh kosong")
        return cleaned

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return [t.strip()[:50] for t in v if t.strip()]

    @model_validator(mode="after")
    def _dates(self) -> PlanSubtask:
        if self.end_date < self.start_date:
            raise ValueError(f"{self.ref}: end_date harus >= start_date")
        return self


class PlanTask(PlanSubtask):
    subtasks: list[PlanSubtask] = Field(default_factory=list, max_length=MAX_SUBTASKS)


class PlanRisk(BaseModel):
    code: RiskCode = "OTHER"
    message: str = Field(min_length=1, max_length=300)


class MissionPlan(BaseModel):
    summary: str = Field(default="", max_length=600)
    assumptions: list[str] = Field(default_factory=list, max_length=10)
    risks: list[PlanRisk] = Field(default_factory=list, max_length=10)
    tasks: list[PlanTask] = Field(min_length=1, max_length=MAX_TASKS)

    @field_validator("assumptions")
    @classmethod
    def _assumptions(cls, v: list[str]) -> list[str]:
        return [a.strip()[:300] for a in v if a.strip()]

    def items(self) -> list[PlanSubtask]:
        return [x for t in self.tasks for x in (t, *t.subtasks)]


def _check_structure(plan: MissionPlan, start: date, end: date) -> None:
    items = plan.items()
    if len(items) > MAX_PLAN_ITEMS:
        raise ValueError(f"Rencana maksimal {MAX_PLAN_ITEMS} task + subtask")
    refs = [x.ref for x in items]
    duplicates = sorted({r for r in refs if refs.count(r) > 1})
    if duplicates:
        raise ValueError(f"ref ganda: {', '.join(duplicates)}")
    for task in plan.tasks:
        for x in (task, *task.subtasks):
            if x.start_date < start or x.end_date > end:
                raise ValueError(f"{x.ref}: tanggal di luar timeline {start}..{end}")
        for sub in task.subtasks:
            if sub.start_date < task.start_date or sub.end_date > task.end_date:
                raise ValueError(f"{sub.ref}: tanggal di luar rentang task {task.ref}")
    known = set(refs)
    for x in items:
        unknown = [d for d in x.depends_on if d not in known]
        if unknown:
            raise ValueError(f"{x.ref}: depends_on tidak dikenal: {', '.join(unknown)}")
    _check_acyclic({x.ref: x.depends_on for x in items})


def _check_acyclic(graph: dict[str, list[str]]) -> None:
    state: dict[str, int] = {}  # 1 = sedang dikunjungi, 2 = selesai

    def visit(node: str) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            raise ValueError(f"dependensi siklik pada {node}")
        state[node] = 1
        for dep in graph.get(node, []):
            visit(dep)
        state[node] = 2

    for node in graph:
        visit(node)


def _covering(task: Any) -> Any:
    subs = task.get("subtasks") if isinstance(task, dict) else None
    if not isinstance(subs, list) or not subs:
        return task
    starts = [
        s.get("start_date")
        for s in subs
        if isinstance(s, dict) and isinstance(s.get("start_date"), str)
    ]
    ends = [
        s.get("end_date")
        for s in subs
        if isinstance(s, dict) and isinstance(s.get("end_date"), str)
    ]
    out = dict(task)
    # tanggal ISO (YYYY-MM-DD) dapat dibandingkan sebagai string
    if starts and isinstance(out.get("start_date"), str):
        out["start_date"] = min([out["start_date"], *starts])
    if ends and isinstance(out.get("end_date"), str):
        out["end_date"] = max([out["end_date"], *ends])
    return out


def plan_model(start: date, end: date) -> type[MissionPlan]:
    """Model rencana yang terikat timeline misi (dipakai juga untuk loop perbaikan LLM)."""

    class BoundedMissionPlan(MissionPlan):
        @model_validator(mode="before")
        @classmethod
        def _cover_subtasks(cls, data: Any) -> Any:
            """Perlebar rentang task induk agar mencakup subtask-nya (kesalahan LLM yang umum)."""
            if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
                return data
            return {**data, "tasks": [_covering(t) for t in data["tasks"]]}

        @model_validator(mode="after")
        def _bounds(self) -> BoundedMissionPlan:
            _check_structure(self, start, end)
            return self

    return BoundedMissionPlan


def finalize_plan(
    plan: MissionPlan,
    *,
    default_agent_key: str,
    allowed_agents: set[str] | None = None,
) -> MissionPlan:
    """Salinan rencana: agent executor=agent dinormalkan + risiko dependensi yang bertabrakan.

    `agent_key` di luar `allowed_agents` (profil MCP aktif) diganti agent planner.
    """
    allowed = allowed_agents or {default_agent_key}
    by_ref = {x.ref: x for x in plan.items()}
    tight = [
        f"{x.ref} mulai {x.start_date} sebelum {d} selesai {by_ref[d].end_date}"
        for x in plan.items()
        for d in x.depends_on
        if x.start_date < by_ref[d].end_date  # serah terima di hari yang sama = wajar
    ]

    def with_agent(x: PlanSubtask) -> dict[str, Any]:
        if x.executor != "agent":
            return {"agent_key": None}
        key = x.agent_key if x.agent_key in allowed else default_agent_key
        return {"agent_key": key}

    tasks = [
        t.model_copy(
            update={
                **with_agent(t),
                "subtasks": [s.model_copy(update=with_agent(s)) for s in t.subtasks],
            }
        )
        for t in plan.tasks
    ]
    risks = list(plan.risks)
    if tight:
        message = ("Dependensi bertabrakan: " + "; ".join(tight))[:300]
        risks = [*risks, PlanRisk(code="TIMELINE_TIGHT", message=message)][-10:]
    return MissionPlan(summary=plan.summary, assumptions=plan.assumptions, risks=risks, tasks=tasks)


def build_prompt(
    *,
    title: str,
    instruction: str,
    start: date,
    end: date,
    today: date,
    context: dict[str, Any],
    available_agents: list[str],
    feedback: str | None,
    previous_plan: dict[str, Any] | None,
) -> str:
    def dumps(value: Any) -> str:
        return orjson.dumps(value, option=orjson.OPT_INDENT_2).decode()

    existing = context.get("existing_tasks") or []
    return prompts.render(
        PROMPT,
        PROMPT_VERSION,
        title=title,
        instruction=instruction,
        start=start.isoformat(),
        end=end.isoformat(),
        today=today.isoformat(),
        days=(end - start).days + 1,
        insight=context.get("insight_md") or "(tidak ada)",
        recommendations="\n".join(f"- {r}" for r in context.get("recommendations") or [])
        or "(tidak ada)",
        existing="\n".join(
            f"- {t['title']} ({t.get('start_date') or '?'} s/d {t.get('end_date') or '?'})"
            for t in existing
        )
        or "(belum ada)",
        agents=", ".join(available_agents) or "(tidak ada)",
        feedback=feedback or "(tidak ada)",
        previous=dumps(previous_plan) if previous_plan else "(tidak ada)",
        max_tasks=MAX_TASKS,
        max_subtasks=MAX_SUBTASKS,
        max_items=MAX_PLAN_ITEMS,
    )


async def generate_plan(
    services: Any,
    *,
    prompt: str,
    start: date,
    end: date,
    route: str,
    budget: BudgetScope | None,
) -> tuple[MissionPlan, int]:
    """(rencana tervalidasi, token percobaan yang berhasil). Error LLM diteruskan ke pemanggil.

    Rencana bisa ribuan token: loop `repair` generik (teks dipotong 2.000 karakter, 800 token)
    tidak mampu memperbaikinya, jadi planner diminta ulang sekali dengan alasan penolakan.
    """
    assert services.llm is not None
    model = plan_model(start, end)

    async def ask(content: str) -> tuple[MissionPlan, int]:
        plan, result = await services.llm.chat_json(
            "mission_plan",
            [{"role": "user", "content": content}],
            model,
            repair_attempts=0,
            budget=budget,
            prompt_version=PROMPT_VERSION,
            route_override=route,
        )
        return plan, result.prompt_tokens + result.completion_tokens

    try:
        return await ask(prompt)
    except UpstreamError as exc:
        reason = str((exc.details or {}).get("error") or "")
        if (exc.details or {}).get("reason") != "invalid_json":
            raise
    return await ask(f"{prompt}\n\n{RETRY_NOTE.format(reason=reason[:500])}")
