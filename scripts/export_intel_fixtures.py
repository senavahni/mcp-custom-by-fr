"""Bangkitkan fixture kontrak 1.2 (toolset `intel`) ke `contracts/intel-1.2/` (M3_TASK_000).

    python scripts/export_intel_fixtures.py [--dashboard <path repo marketingdashboardjtp>]

Sumber kebenaran = repo MCP. Fixture valid divalidasi model Pydantic `schemas/intel.py`; fixture
invalid HARUS ditolak. `--dashboard` menyalin folder yang sama ke repo Dashboard agar contract test
Zod memakai byte yang identik (dicek lewat `SHA256SUMS`).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import shutil
import sys
from pathlib import Path
from typing import Any

import orjson

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_marketing.schemas import intel  # noqa: E402

OUT = ROOT / "contracts" / "intel-1.2"
FIXTURES = OUT / "fixtures"

WS = "00000000-0000-4000-8000-000000000001"
JOB = "55555555-5555-4555-8555-555555555555"
USER = "44444444-4444-4444-8444-444444444444"
EVENT = "66666666-6666-4666-8666-666666666666"
MCP_JOB = "j_k3m9x2qa1b"
HANDLE = "h_7qk2m9x1za"
TS = "2026-11-04T01:00:00Z"

SCHEMAS: dict[str, type[Any]] = {
    "job_spec": intel.JobSpec,
    "job_estimate_input": intel.JobEstimateIn,
    "job_create_input": intel.JobCreateIn,
    "items_input": intel.ItemsIn,
    "agents_list": intel.AgentsListOut,
    "sources_status": intel.SourcesStatusOut,
    "estimate": intel.Estimate,
    "job_accepted": intel.JobAcceptedOut,
    "job_view": intel.JobView,
    "cancel": intel.CancelOut,
    "items_page": intel.ItemsOut,
    "llm_status": intel.LlmStatusOut,
}

SPEC: dict[str, Any] = {
    "title": "X · AI marketing tools · Okt 2026",
    "language": "id",
    "period": {"since": "2026-10-01", "until": "2026-10-30"},
    "sources": [
        {
            "type": "x",
            "keywords": ["AI marketing tools"],
            "match": "any",
            "lang": "id",
            "exclude_retweets": True,
            "exclude_replies": False,
            "max_items": 500,
            "provider": "auto",
        },
        {
            "type": "threads",
            "keywords": ["AI marketing"],
            "search_type": "RECENT",
            "max_items": 300,
        },
        {"type": "web", "mode": "scrape", "urls": ["https://contoh.id/artikel"]},
        {"type": "trends", "keywords": ["AI marketing"], "geo": "ID"},
    ],
    "stages": {
        "collect": {"agent_key": "researcher"},
        "analyze": {"agent_key": "analyst", "aspects": ["harga", "fitur"], "method": "auto"},
    },
}

ESTIMATE: dict[str, Any] = {
    "seconds": 360,
    "sources": [{"type": "x", "mode": "browser", "external_units": 500, "usd_external": 0.0}],
    "llm": {
        "tokens_est": 40000,
        "usd_est": 0.1,
        "by_stage": [
            {"stage": "analyze", "agent_key": "analyst", "route": "smart", "tokens_est": 32000}
        ],
    },
    "requires_confirmation": True,
    "warnings": [
        {"code": "X_API_WINDOW", "message": "X API hanya 7 hari terakhir; memakai mode browser"}
    ],
}

COST: dict[str, Any] = {
    "llm_tokens": 38211,
    "usd_llm": 0.094,
    "external_units": 500,
    "usd_external": 0.0,
    "by_stage": [
        {"stage": "analyze", "agent_key": "analyst", "llm_tokens": 38211, "usd_llm": 0.094}
    ],
}

RESULT: dict[str, Any] = {
    "totals": {"items": 500, "by_source": {"x": 500}},
    "sentiment": {
        "distribution": {"positive": 0.6, "neutral": 0.3, "negative": 0.1},
        "net_sentiment": 0.5,
        "by_day": {
            "cols": ["day", "total", "positive", "neutral", "negative", "net"],
            "rows": [["2026-10-01", 20, 12, 6, 2, 0.5]],
        },
        "aspects": {
            "cols": ["aspect", "mentions", "positive", "neutral", "negative", "net"],
            "rows": [["harga", 40, 10, 10, 20, -0.25]],
        },
        "examples": {
            "positive": [
                {
                    "id": "p1",
                    "source": "x",
                    "author": "akun_a",
                    "text": "Tools AI ini menghemat waktu kami.",
                    "url": "https://x.com/akun_a/status/1",
                    "created_at": "2026-10-02T03:00:00Z",
                }
            ],
            "negative": [],
        },
    },
    "trends": {
        "topics": [{"label": "otomasi konten", "count": 84, "share": 0.17}],
        "hashtags": [["ai", 120]],
        "authors": [["akun_a", 14]],
    },
    "volume_by_day": {"cols": ["day", "source", "count"], "rows": [["2026-10-01", "x", 20]]},
    "google_trends": None,
    "summary_md": "## Ringkasan\n- Sentimen dominan **positif** (60%).",
    "recommendations": ["Perbanyak konten studi kasus."],
    "items_handle": HANDLE,
    "items_expire_at": "2026-12-04T00:00:00Z",
    "partial": False,
    "warnings": [],
}


def stage(
    name: str, agent: str, status: str, done: int = 0, total: int | None = None
) -> dict[str, Any]:
    return {
        "stage": name,
        "agent_key": agent,
        "status": status,
        "progress": {"done": done, "total": total},
        "started_at": TS if status != "pending" else None,
        "finished_at": TS if status in ("completed", "skipped", "failed", "cancelled") else None,
        "error": None,
    }


def job_view(status: str, **over: Any) -> dict[str, Any]:
    view = {
        "mcp_job_id": MCP_JOB,
        "dashboard_job_id": JOB,
        "status": status,
        "stages": [
            stage("collect", "researcher", "completed", 500, 500),
            stage("analyze", "analyst", "completed", 500, 500),
        ],
        "result": copy.deepcopy(RESULT),
        "cost": copy.deepcopy(COST),
        "error": None,
    }
    view.update(over)
    return view


def event(event_type: str, data: dict[str, Any], **over: Any) -> dict[str, Any]:
    env = {
        "contract_version": "1.2",
        "event_id": EVENT,
        "event_type": event_type,
        "occurred_at": TS,
        "job_id": JOB,
        "mcp_job_id": MCP_JOB,
        "sequence": 1,
        "data": data,
    }
    env.update(over)
    return env


def with_spec(**over: Any) -> dict[str, Any]:
    spec = copy.deepcopy(SPEC)
    spec.update(over)
    return spec


def build() -> list[dict[str, Any]]:
    fx: list[dict[str, Any]] = []

    def add(name: str, schema: str, valid: bool, payload: Any, path: str | None = None) -> None:
        item: dict[str, Any] = {"name": name, "schema": schema, "valid": valid, "payload": payload}
        if path is not None:
            item["expect_path"] = path
        fx.append(item)

    # ---- valid
    add("valid_spec_all_sources", "job_spec", True, SPEC)
    add(
        "valid_spec_web_crawl_minimal",
        "job_spec",
        True,
        {
            "title": "Crawl blog kompetitor",
            "period": {"since": "2026-10-01", "until": "2026-10-31"},
            "sources": [{"type": "web", "mode": "crawl", "start_url": "https://contoh.id/blog"}],
            "stages": {"collect": {"agent_key": "researcher"}, "analyze": {"agent_key": "analyst"}},
        },
    )
    add(
        "valid_estimate_input",
        "job_estimate_input",
        True,
        {"workspace_id": WS, "spec": SPEC},
    )
    add(
        "valid_create_input",
        "job_create_input",
        True,
        {
            "contract_version": "1.2",
            "dashboard_job_id": JOB,
            "workspace_id": WS,
            "requested_by": {"user_id": USER, "name": "Member Demo"},
            "spec": SPEC,
            "confirm_cost": True,
        },
    )
    add(
        "valid_items_input",
        "items_input",
        True,
        {
            "workspace_id": WS,
            "mcp_job_id": MCP_JOB,
            "filter": {"source": "x", "sentiment": "negative", "q": "mahal"},
            "order_by": "-engagement",
            "limit": 50,
        },
    )
    add(
        "valid_agents_list",
        "agents_list",
        True,
        {
            "agents": [
                {
                    "agent_key": "researcher",
                    "display_name": "Agent Researcher",
                    "is_active": True,
                    "route": "smart",
                    "run_token_budget": 150000,
                    "toolsets": [
                        "pm_agent",
                        "web",
                        "social",
                        "trends",
                        "kb",
                        "analysis",
                        "data",
                        "job",
                    ],
                    "capabilities": ["collect", "analyze", "index"],
                    "gateway_key": {"source": "env", "prefix": None},
                }
            ]
        },
    )
    add(
        "valid_sources_status",
        "sources_status",
        True,
        {
            "sources": [
                {
                    "source": "x",
                    "available": True,
                    "mode": "browser",
                    "detail": "browser profile 'default' login",
                    "checked_at": TS,
                },
                {
                    "source": "trends",
                    "available": True,
                    "mode": "unofficial",
                    "detail": "provider unofficial",
                    "checked_at": TS,
                },
            ],
            "llm": {
                "routes": [
                    {"route": "fast", "available": True},
                    {"route": "smart", "available": True},
                ]
            },
            "checked_at": TS,
        },
    )
    add("valid_estimate", "estimate", True, ESTIMATE)
    add(
        "valid_job_accepted",
        "job_accepted",
        True,
        {"accepted": True, "mcp_job_id": MCP_JOB, "estimate": ESTIMATE},
    )
    add("valid_job_view_completed", "job_view", True, job_view("completed"))
    add(
        "valid_job_view_running",
        "job_view",
        True,
        job_view(
            "analyzing",
            stages=[
                stage("collect", "researcher", "completed", 500, 500),
                stage("analyze", "analyst", "running", 120, 500),
            ],
            result=None,
        ),
    )
    partial = copy.deepcopy(RESULT)
    partial.update(
        partial=True,
        summary_md=None,
        warnings=[{"code": "SUMMARY_UNAVAILABLE", "message": "Ringkasan tidak tersedia"}],
    )
    add(
        "valid_job_view_failed_partial",
        "job_view",
        True,
        job_view(
            "failed",
            result=partial,
            error={
                "code": "LLM_QUOTA_EXCEEDED",
                "message": "Kuota project Gateway habis",
                "retryable": False,
                "stage": "analyze",
            },
        ),
    )
    add("valid_cancel", "cancel", True, {"cancelled": True, "status": "cancelling"})
    add(
        "valid_items_page",
        "items_page",
        True,
        {
            "cols": [
                "id",
                "source",
                "posted_at",
                "author",
                "text",
                "url",
                "engagement",
                "sentiment",
                "score",
            ],
            "rows": [
                [
                    "p1",
                    "x",
                    "2026-10-02T03:00:00Z",
                    "akun_a",
                    "Tools AI ini menghemat waktu.",
                    "https://x.com/a/1",
                    12,
                    "positive",
                    0.91,
                ]
            ],
            "total": 500,
            "next_cursor": "eyJpIjo1MH0",
            "expires_at": "2026-12-04T00:00:00Z",
        },
    )
    add(
        "valid_llm_status",
        "llm_status",
        True,
        {
            "agents": [
                {
                    "agent_key": "analyst",
                    "key_source": "env",
                    "key_prefix": None,
                    "key_status": "unknown",
                    "last_error": None,
                    "usage": {
                        "calls": 10,
                        "prompt_tokens": 30000,
                        "completion_tokens": 8000,
                        "usd": 0.09,
                    },
                }
            ],
            "project": None,
        },
    )
    for name, et, data in (
        ("started", "intel.job.started", {}),
        (
            "stage_started",
            "intel.job.stage.started",
            {"stage": "collect", "agent_key": "researcher"},
        ),
        (
            "progress",
            "intel.job.progress",
            {"stage": "collect", "done": 312, "total": 500, "message": "312 post"},
        ),
        (
            "stage_completed",
            "intel.job.stage.completed",
            {"stage": "collect", "status": "completed", "stats": {"total": 500}, "warnings": []},
        ),
        (
            "stage_skipped",
            "intel.job.stage.completed",
            {
                "stage": "analyze",
                "status": "skipped",
                "warnings": [{"code": "NO_ITEMS", "message": "Tidak ada data"}],
            },
        ),
        (
            "completed",
            "intel.job.completed",
            {
                "summary_preview": "Sentimen dominan positif",
                "totals": {"items": 500, "by_source": {"x": 500}},
                "cost": COST,
                "partial": False,
            },
        ),
        (
            "failed",
            "intel.job.failed",
            {
                "stage": "analyze",
                "error": {
                    "code": "LLM_KEY_INVALID",
                    "message": "Key Gateway ditolak",
                    "retryable": False,
                },
            },
        ),
        ("cancelled", "intel.job.cancelled", {}),
    ):
        add(f"valid_event_{name}", "event", True, event(et, data))

    # ---- invalid (setiap fixture punya satu pelanggaran; expect_path = lokasi error pertama)
    bad = with_spec()
    bad["sources"][0]["max_items"] = intel.X_MAX_ITEMS + 1
    add("invalid_spec_x_max_items", "job_spec", False, bad, "sources.0.max_items")
    bad = with_spec()
    bad["sources"][1]["max_items"] = intel.THREADS_MAX_ITEMS + 1
    add("invalid_spec_threads_max_items", "job_spec", False, bad, "sources.1.max_items")
    bad = with_spec()
    bad["sources"][0]["keywords"] = [f"k{i}" for i in range(intel.KEYWORDS_MAX + 1)]
    add("invalid_spec_too_many_keywords", "job_spec", False, bad, "sources.0.keywords")
    bad = with_spec()
    bad["sources"][0]["keywords"] = ["x" * (intel.KEYWORD_MAX_CHARS + 1)]
    add("invalid_spec_keyword_too_long", "job_spec", False, bad, "sources.0.keywords.0")
    bad = with_spec()
    bad["sources"][3]["keywords"] = [f"k{i}" for i in range(intel.TRENDS_KEYWORDS_MAX + 1)]
    add("invalid_spec_trends_keywords", "job_spec", False, bad, "sources.3.keywords")
    bad = with_spec()
    bad["sources"][2]["urls"] = ["javascript:alert(1)"]
    add("invalid_spec_url_scheme", "job_spec", False, bad, "sources.2.urls.0")
    bad = with_spec()
    bad["sources"][2] = {"type": "web", "mode": "scrape"}
    add("invalid_spec_web_scrape_without_urls", "job_spec", False, bad, "sources.2")
    bad = with_spec()
    bad["sources"][2] = {
        "type": "web",
        "mode": "crawl",
        "start_url": "https://contoh.id",
        "max_pages": intel.CRAWL_MAX_PAGES + 1,
    }
    add("invalid_spec_crawl_max_pages", "job_spec", False, bad, "sources.2.max_pages")
    bad = with_spec()
    bad["sources"] = [SPEC["sources"][0], SPEC["sources"][0]]
    add("invalid_spec_duplicate_source_type", "job_spec", False, bad, "sources")
    add("invalid_spec_no_sources", "job_spec", False, with_spec(sources=[]), "sources")
    bad = with_spec()
    bad["sources"][3] = {"type": "meta", "keywords": ["x"]}
    add("invalid_spec_unknown_source", "job_spec", False, bad, "sources.3")
    add(
        "invalid_spec_period_reversed",
        "job_spec",
        False,
        with_spec(period={"since": "2026-10-30", "until": "2026-10-01"}),
        "period",
    )
    add(
        "invalid_spec_period_too_long",
        "job_spec",
        False,
        with_spec(period={"since": "2025-01-01", "until": "2026-10-01"}),
        "period",
    )
    add("invalid_spec_empty_title", "job_spec", False, with_spec(title="   "), "title")
    bad = with_spec()
    bad["stages"]["analyze"]["aspects"] = [f"a{i}" for i in range(intel.ASPECTS_MAX + 1)]
    add("invalid_spec_too_many_aspects", "job_spec", False, bad, "stages.analyze.aspects")
    bad = with_spec()
    bad["stages"]["collect"]["agent_key"] = "Bad Key!"
    add("invalid_spec_agent_key", "job_spec", False, bad, "stages.collect.agent_key")
    bad = with_spec()
    bad["extra_field"] = 1
    add("invalid_spec_unknown_field", "job_spec", False, bad, "extra_field")
    add(
        "invalid_create_contract_2",
        "job_create_input",
        False,
        {
            "contract_version": "2.0",
            "dashboard_job_id": JOB,
            "workspace_id": WS,
            "requested_by": {"user_id": USER, "name": "x"},
            "spec": SPEC,
        },
        "contract_version",
    )
    add(
        "invalid_items_limit",
        "items_input",
        False,
        {"workspace_id": WS, "mcp_job_id": MCP_JOB, "limit": intel.ITEMS_PAGE_MAX + 1},
        "limit",
    )
    add(
        "invalid_items_bad_job_id",
        "items_input",
        False,
        {"workspace_id": WS, "mcp_job_id": "job-1"},
        "mcp_job_id",
    )
    bad_view = job_view("completed")
    bad_view["status"] = "done"
    add("invalid_job_view_status", "job_view", False, bad_view, "status")
    bad_view = job_view("completed")
    bad_view["result"]["sentiment"]["distribution"]["positive"] = 60
    add(
        "invalid_job_view_distribution_percent",
        "job_view",
        False,
        bad_view,
        "result.sentiment.distribution.positive",
    )
    add("invalid_event_unknown_type", "event", False, event("intel.job.exploded", {}), "event_type")
    add(
        "invalid_event_sequence_zero",
        "event",
        False,
        event("intel.job.started", {}, sequence=0),
        "sequence",
    )
    add(
        "invalid_event_missing_job_id",
        "event",
        False,
        {k: v for k, v in event("intel.job.started", {}).items() if k != "job_id"},
        "job_id",
    )
    add(
        "invalid_event_progress_negative",
        "event",
        False,
        event("intel.job.progress", {"stage": "collect", "done": -1}),
        "data.done",
    )
    add(
        "invalid_event_failed_without_error",
        "event",
        False,
        event("intel.job.failed", {"stage": "analyze"}),
        "data.error",
    )
    return fx


SOURCE_TAGS = frozenset(("x", "threads", "web", "trends"))


def _path(loc: tuple[Any, ...]) -> str:
    """Path error gaya Zod: buang tag union (`sources.2.web.urls` -> `sources.2.urls`)."""
    parts: list[str] = []
    for i, p in enumerate(loc):
        if i >= 2 and loc[i - 2] == "sources" and isinstance(loc[i - 1], int) and p in SOURCE_TAGS:
            continue
        parts.append(str(p))
    return ".".join(parts)


def check(fx: dict[str, Any]) -> list[str]:
    """Kembalikan daftar path error (kosong = valid) memakai Pydantic."""
    from pydantic import ValidationError

    try:
        if fx["schema"] == "event":
            env = intel.IntelEventEnvelope.model_validate(fx["payload"])
            try:
                intel.validate_intel_event_data(env.event_type, env.data)
            except (ValidationError, ValueError) as exc:
                if isinstance(exc, ValidationError):
                    return ["data." + _path(tuple(e["loc"])) for e in exc.errors()]
                return ["event_type"]
        else:
            SCHEMAS[fx["schema"]].model_validate(fx["payload"])
    except ValidationError as exc:
        return [_path(tuple(e["loc"])) for e in exc.errors()]
    return []


def write(fixtures: list[dict[str, Any]]) -> None:
    if FIXTURES.exists():
        shutil.rmtree(FIXTURES)
    FIXTURES.mkdir(parents=True)
    sums = []
    for fx in fixtures:
        body = orjson.dumps(fx, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n"
        name = f"{fx['name']}.json"
        (FIXTURES / name).write_bytes(body)
        sums.append(f"{hashlib.sha256(body).hexdigest()}  fixtures/{name}")
    (OUT / "SHA256SUMS").write_text("\n".join(sorted(sums)) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dashboard", type=Path, help="path repo marketingdashboardjtp")
    args = parser.parse_args()
    fixtures = build()
    problems = []
    for fx in fixtures:
        errors = check(fx)
        if fx["valid"] and errors:
            problems.append(f"{fx['name']}: seharusnya valid, error {errors}")
        if not fx["valid"] and not errors:
            problems.append(f"{fx['name']}: seharusnya ditolak Pydantic")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    write(fixtures)
    print(f"{len(fixtures)} fixture ditulis ke {OUT}")
    if args.dashboard:
        target = args.dashboard / "contracts" / "intel-1.2"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(OUT, target)
        print(f"disalin ke {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
