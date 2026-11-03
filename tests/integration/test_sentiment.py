"""TASK-20 end-to-end: sentiment_analyze (texts & handle), cache DB, konfirmasi biaya, summary."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from mcp_marketing.models.social import SocialPost
from mcp_marketing.server.app import build_asgi_app
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.server.pipeline import ToolPipeline
from mcp_marketing.server.services import Services
from mcp_marketing.services.nlp import sentiment as sentiment_mod
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.utils.time import now
from mcp_marketing.worker.handlers import default_registry
from mcp_marketing.worker.queue import JobQueue
from mcp_marketing.worker.runner import Worker
from tests.fakes import SentimentLLM
from tests.integration.conftest import make_client


def fake_predict(model_dir: str, texts: list[str]) -> list[Any]:
    out = []
    for t in texts:
        if "ragu" in t:
            out.append(("neutral", {"positive": 0.35, "neutral": 0.4, "negative": 0.25}))
        elif "kecewa" in t or "mahal" in t:
            out.append(("negative", {"positive": 0.05, "neutral": 0.1, "negative": 0.85}))
        else:
            out.append(("positive", {"positive": 0.92, "neutral": 0.05, "negative": 0.03}))
    return out


@dataclass
class Env:
    services: Services
    pipeline: ToolPipeline
    client: ClientIdentity
    llm: SentimentLLM

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        return await self.pipeline.call(tool, args, client=self.client)


@pytest_asyncio.fixture
async def env(
    services: Services, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Env]:
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.onnx").write_bytes(b"")
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "_name_or_path": "test/model",
                "id2label": {"0": "positive", "1": "neutral", "2": "negative"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sentiment_mod, "predict_batch", fake_predict)
    settings = services.settings
    services.settings = settings.model_copy(
        update={"nlp": settings.nlp.model_copy(update={"sentiment_model_dir": tmp_path})}
    )
    llm = SentimentLLM()
    original_llm, services.llm = services.llm, llm
    reg = default_registry()
    services.jobs = JobQueue(services.db, estimator=reg)
    worker = Worker(
        services,
        services.jobs,
        reg,
        worker_id="w-sent",
        use_process_pool=False,
        poll_interval_s=0.05,
    )
    task = asyncio.create_task(worker.run())
    _, pipeline, _ = build_asgi_app(services)
    _, client = await make_client(services, name="sent", toolsets=["analysis", "job", "data"])
    try:
        yield Env(services, pipeline, client, llm)
    finally:
        worker.stop()
        await task
        await pipeline.drain()
        services.settings, services.llm = settings, original_llm


TEXTS = [
    "Kopinya enak bgt 😍",
    "Kopinya enak bgt 😍",
    "agak ragu sama rasanya",
    "kecewa, harganya mahal",
    "katanya enak, wkwk",
]


async def test_texts_inline_then_full_cache_hit(env: Env) -> None:
    first = (
        await env.call("sentiment_analyze", {"source": {"texts": TEXTS}, "aspects": ["harga"]})
    ).structured
    # "ragu" & "katanya enak" dieskalasi; LLM palsu melabeli keduanya positif
    assert first["counts"] == {"positive": 4, "neutral": 0, "negative": 1}
    assert first["cost"]["items_model"] == 4 and first["cost"]["items_llm"] == 2
    assert first["aspects"]["rows"][0][:2] == ["harga", 1]
    assert first["handle"].startswith("h_")
    tokens_before = len(env.llm.calls)

    again = (await env.call("sentiment_analyze", {"source": {"texts": TEXTS}})).structured
    assert again["cost"]["cache_hits"] == len(TEXTS) and again["cost"]["llm_tokens"] == 0
    assert len(env.llm.calls) == tokens_before


async def test_llm_method_requires_cost_confirmation(env: Env) -> None:
    preview = (
        await env.call("sentiment_analyze", {"source": {"texts": TEXTS}, "method": "llm"})
    ).structured
    assert preview["estimate"]["items"] == 5 and "handle" not in preview or not preview["handle"]
    assert env.llm.calls == []
    done = (
        await env.call(
            "sentiment_analyze",
            {"source": {"texts": TEXTS}, "method": "llm", "confirm_cost": True},
        )
    ).structured
    assert done["cost"]["items_llm"] == 4 and done["cost"]["items_model"] == 0


async def _posts_handle(env: Env, texts: list[tuple[str, str]]) -> str:
    rows = []
    async with env.services.db.session() as session:
        for i, (day, body) in enumerate(texts):
            pid = uuid.uuid4()
            session.add(
                SocialPost(
                    id=pid,
                    source="x",
                    external_id=f"e{uuid.uuid4().hex[:12]}",
                    text=body,
                    posted_at=now(),
                    provider="browser",
                )
            )
            rows.append(
                {
                    "id": str(pid),
                    "source": "x",
                    "posted_at": f"{day}T03:00:00+00:00",
                    "author": f"u{i}",
                    "text": body[:20],
                    "likes": i,
                    "keywords": ["kopi"],
                }
            )
    rs = await env.services.results.create(
        kind="social_posts",
        columns=["id", "source", "posted_at", "author", "text", "likes", "keywords"],
        rows=rows,
        owner=Owner(client_id=env.client.id),
        ref_id="id",
    )
    return rs.handle


async def test_handle_group_by_day_and_summary_compare(env: Env) -> None:
    week1 = await _posts_handle(
        env,
        [
            ("2026-10-01", "enak sekali kopinya"),
            ("2026-10-01", "kecewa, mahal"),
            ("2026-10-02", "rasanya mantap"),
        ],
    )
    week2 = await _posts_handle(
        env, [("2026-10-08", "kecewa banget"), ("2026-10-08", "mahal tidak sepadan")]
    )
    a = (
        await env.call("sentiment_analyze", {"source": {"handle": week1}, "group_by": "day"})
    ).structured
    assert [r[0] for r in a["by_group"]["rows"]] == ["2026-10-01", "2026-10-02"]
    assert a["examples"]["positive"][0]["text"]
    b = (await env.call("sentiment_analyze", {"source": {"handle": week2}})).structured

    summary = (
        await env.call(
            "sentiment_summary",
            {"handle": b["handle"], "group_by": "keyword", "compare_handle": a["handle"]},
        )
    ).structured
    assert summary["net_sentiment"] == -1.0
    assert summary["compare"]["net_sentiment_delta"] == pytest.approx(-1.0 - 1 / 3, abs=1e-3)
    wrong = await env.call("sentiment_summary", {"handle": week1})
    assert wrong.error_code == "VALIDATION_ERROR"
