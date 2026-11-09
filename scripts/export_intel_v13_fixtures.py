"""Bangkitkan fixture kontrak 1.3 (job kind v1, event & pagination baru) ke `contracts/1.3/`.

    python scripts/export_intel_v13_fixtures.py [--dashboard <path repo marketingdashboardjtp>]

Sumber kebenaran = repo MCP (`schemas/intel_v13.py`). Fixture valid divalidasi Pydantic; fixture
invalid HARUS ditolak. Fixture 1.2 (`contracts/intel-1.2/`) tidak disentuh. `--dashboard` menyalin
folder ke repo Dashboard agar contract test Zod memakai byte identik (dicek lewat `SHA256SUMS`).
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

from mcp_marketing.schemas import intel_v13 as v13  # noqa: E402

OUT = ROOT / "contracts" / "1.3"
FIXTURES = OUT / "fixtures"

WS = "00000000-0000-4000-8000-000000000001"
JOB = "55555555-5555-4555-8555-555555555555"
USER = "44444444-4444-4444-8444-444444444444"
EVENT = "66666666-6666-4666-8666-666666666666"
PRODUCT = "77777777-7777-4777-8777-777777777777"
SEGMENT = "88888888-8888-4888-8888-888888888888"
UPLOAD = "99999999-9999-4999-8999-999999999999"
BATCH = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
COMPANY = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ASSET = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
REPORT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
MCP_JOB = "j_k3m9x2qa1b"
TS = "2026-11-09T01:00:00Z"
PERIOD = {"since": "2026-11-01", "until": "2026-11-30"}

SCHEMAS: dict[str, type[Any]] = {
    "job_create_input": v13.KindJobCreateIn,
    "job_estimate_input": v13.KindJobEstimateIn,
    "estimate": v13.KindEstimate,
    "job_accepted": v13.KindJobAcceptedOut,
    "job_view": v13.KindJobView,
    "page_input": v13.PageIn,
    "page_meta": v13.PageMeta,
}

PARAMS: dict[str, dict[str, Any]] = {
    "company_discovery": {
        "kind": "company_discovery",
        "product_id": PRODUCT,
        "region_codes": ["32", "31.71"],
        "segment_ids": [SEGMENT],
        "max_companies": 100,
        "review_mode": "review",
    },
    "company_import": {
        "kind": "company_import",
        "upload_id": UPLOAD,
        "sheet": "Daftar Instansi",
        "header_row": 2,
        "mode": "contact_targets",
    },
    "contact_finder": {"kind": "contact_finder", "import_batch_id": BATCH},
    "social_listening": {
        "kind": "social_listening",
        "product_id": PRODUCT,
        "period": PERIOD,
        "keywords": ["aplikasi kasir"],
        "sources": ["x", "threads", "trends"],
        "review_mode": "review",
    },
    "book_ingest": {"kind": "book_ingest", "asset_id": ASSET, "extract_cards": True},
    "opportunity_scoring": {
        "kind": "opportunity_scoring",
        "product_ids": [PRODUCT],
        "company_ids": [],
        "scoring_model_version": 1,
    },
    "strategy": {
        "kind": "strategy",
        "product_id": PRODUCT,
        "segment_ids": [SEGMENT],
        "objective": "Menambah 20 lead instansi pendidikan di Jawa Barat",
        "period": PERIOD,
    },
    "content_bank": {
        "kind": "content_bank",
        "strategy_report_id": REPORT,
        "item_count": 30,
        "platforms": ["instagram", "linkedin"],
        "period": PERIOD,
    },
}

ESTIMATE: dict[str, Any] = {
    "seconds": 14_400,
    "units": [
        {"unit": "google_search", "count": 40},
        {"unit": "google_maps", "count": 30},
        {"unit": "web_page", "count": 800},
        {"unit": "embedding", "count": 2_400},
    ],
    "llm": {"tokens_est": 30_000, "usd_est": 0.05},
    "requires_confirmation": False,
    "warnings": [
        {"code": "SLOW_SOURCE", "message": "Rem volume Google Maps: perkiraan selesai ±4 jam"}
    ],
}

COST: dict[str, Any] = {
    "llm_tokens": 28_000,
    "usd_llm": 0.047,
    "external_units": 70,
    "usd_external": 0.0,
    "by_stage": [
        {
            "stage": "product_analysis",
            "agent_key": "analyst",
            "llm_tokens": 28_000,
            "usd_llm": 0.047,
        }
    ],
}


def create(kind: str, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "contract_version": "1.3",
        "dashboard_job_id": JOB,
        "workspace_id": WS,
        "requested_by": {"user_id": USER, "name": "Member Demo"},
        "params": copy.deepcopy(PARAMS[kind]),
        "llm_assist": False,
        "budget_usd": 1.0,
        "confirm_cost": True,
    }
    body.update(over)
    return body


def params(kind: str, **over: Any) -> dict[str, Any]:
    body = copy.deepcopy(PARAMS[kind])
    body.update(over)
    return body


def stage(name: str, agent: str | None, status: str, done: int = 0, total: int | None = None):  # type: ignore[no-untyped-def]
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
    view: dict[str, Any] = {
        "mcp_job_id": MCP_JOB,
        "dashboard_job_id": JOB,
        "kind": "company_discovery",
        "status": status,
        "llm_assist": False,
        "budget_usd": 1.0,
        "stages": [
            stage("product_analysis", "analyst", "completed", 1, 1),
            stage("discovery", "prospector", "running", 12, 70),
            stage("enrichment", "prospector", "pending"),
            stage("index", None, "pending"),
            stage("fit", None, "pending"),
        ],
        "paused": None,
        "review": None,
        "result": None,
        "cost": copy.deepcopy(COST),
        "error": None,
    }
    view.update(over)
    return view


def event(event_type: str, data: dict[str, Any], **over: Any) -> dict[str, Any]:
    env: dict[str, Any] = {
        "contract_version": "1.3",
        "event_id": EVENT,
        "event_type": event_type,
        "occurred_at": TS,
        "workspace_id": WS,
        "job_id": JOB,
        "mcp_job_id": MCP_JOB,
        "sequence": 1,
        "data": data,
    }
    env.update(over)
    return env


def _valid(add: Any) -> None:
    for kind in v13.JOB_KINDS:
        add(f"valid_create_{kind}", "job_create_input", True, create(kind))
    add(
        "valid_create_contact_finder_company_ids",
        "job_create_input",
        True,
        create(
            "contact_finder",
            params={"kind": "contact_finder", "company_ids": [COMPANY]},
            budget_usd=None,
        ),
    )
    add(
        "valid_create_minimal_defaults",
        "job_create_input",
        True,
        {
            "contract_version": "1.3",
            "dashboard_job_id": JOB,
            "workspace_id": WS,
            "requested_by": {"user_id": USER, "name": "x"},
            "params": {"kind": "company_discovery", "product_id": PRODUCT},
        },
    )
    add(
        "valid_estimate_input",
        "job_estimate_input",
        True,
        {"workspace_id": WS, "params": PARAMS["company_discovery"], "llm_assist": True},
    )
    add("valid_estimate", "estimate", True, ESTIMATE)
    add(
        "valid_job_accepted",
        "job_accepted",
        True,
        {
            "accepted": True,
            "mcp_job_id": MCP_JOB,
            "kind": "company_discovery",
            "estimate": ESTIMATE,
        },
    )
    add("valid_job_view_running", "job_view", True, job_view("running"))
    paused_stages = job_view("running")["stages"]
    paused_stages[1]["status"] = "paused"
    add(
        "valid_job_view_paused_captcha",
        "job_view",
        True,
        job_view(
            "paused",
            stages=paused_stages,
            paused={
                "reason": "captcha",
                "stage": "discovery",
                "message": "Google meminta verifikasi; selesaikan di jendela Chrome",
                "since": TS,
            },
        ),
    )
    review_stages = job_view("running")["stages"]
    review_stages[0]["status"] = "awaiting_review"
    review_stages[1] = stage("discovery", "prospector", "pending")
    add(
        "valid_job_view_awaiting_review",
        "job_view",
        True,
        job_view(
            "awaiting_review",
            stages=review_stages,
            review={
                "stage": "product_analysis",
                "subject_type": "product_analysis",
                "subject_id": "a1b2c3",
            },
        ),
    )
    add(
        "valid_job_view_completed",
        "job_view",
        True,
        job_view(
            "completed",
            stages=[
                stage("product_analysis", "analyst", "completed", 1, 1),
                stage("discovery", "prospector", "completed", 70, 70),
                stage("enrichment", "prospector", "completed", 52, 52),
                stage("index", None, "completed", 52, 52),
                stage("fit", None, "skipped"),
            ],
            result={"companies": {"created": 48, "updated": 4}},
        ),
    )
    add("valid_page_input", "page_input", True, {"cursor": "eyJpIjo1MH0", "limit": 200})
    add("valid_page_input_defaults", "page_input", True, {})
    add(
        "valid_page_meta",
        "page_meta",
        True,
        {"next_cursor": "eyJpIjoxMDB9", "total_estimate": 3500},
    )
    add("valid_page_meta_last", "page_meta", True, {"next_cursor": None, "total_estimate": None})

    for name, et, data, over in (
        (
            "awaiting_review",
            "intel.job.awaiting_review",
            {
                "stage": "product_analysis",
                "subject_type": "product_analysis",
                "subject_id": "a1b2c3",
                "message": "8 segmen menunggu review",
            },
            {},
        ),
        (
            "paused_captcha",
            "intel.job.paused",
            {
                "stage": "discovery",
                "reason": "captcha",
                "message": "Google meminta verifikasi",
                "resume_hint": "Selesaikan CAPTCHA di Chrome lalu klik Lanjutkan",
            },
            {},
        ),
        (
            "paused_local_model",
            "intel.job.paused",
            {
                "stage": "embed",
                "reason": "local_model_unavailable",
                "message": "embed-local tidak tersedia",
            },
            {},
        ),
        (
            "stage_started_v1_stage",
            "intel.job.stage.started",
            {"stage": "collect", "agent_key": "researcher"},
            {},
        ),
        (
            "company_upserted",
            "intel.company.upserted",
            {
                "company_id": COMPANY,
                "name": "SMAN 3 Bandung",
                "action": "created",
                "origin": "import",
            },
            {},
        ),
        (
            "company_upserted_manual_without_job",
            "intel.company.upserted",
            {"company_id": COMPANY, "name": "PT Contoh", "action": "updated", "origin": "manual"},
            {"job_id": None, "mcp_job_id": None},
        ),
        (
            "import_status",
            "ci.import.status",
            {
                "batch_id": BATCH,
                "status": "importing",
                "rows_total": 1200,
                "rows_ok": 800,
                "rows_review": 30,
                "rows_failed": 2,
            },
            {},
        ),
        (
            "asset_status",
            "kn.asset.status",
            {"asset_id": ASSET, "status": "embedding", "pages_done": 120, "pages_total": 480},
            {},
        ),
        (
            "scores_updated",
            "opp.scores.updated",
            {
                "product_id": PRODUCT,
                "scoring_model_version": 1,
                "scored": 3500,
                "high": 120,
                "medium": 900,
            },
            {},
        ),
        (
            "content_batch_ready",
            "ct.batch.ready",
            {"batch_id": BATCH, "strategy_report_id": REPORT, "items": 30, "flagged": 1},
            {},
        ),
    ):
        add(f"valid_event_{name}", "event", True, event(et, data, **over))


def _invalid(add: Any) -> None:
    add(
        "invalid_create_contract_1_2",
        "job_create_input",
        False,
        create("company_discovery", contract_version="1.2"),
        "contract_version",
    )
    add(
        "invalid_create_unknown_kind",
        "job_create_input",
        False,
        create("company_discovery", params={"kind": "lead_scraping", "product_id": PRODUCT}),
        "params",
    )
    add(
        "invalid_create_unknown_field",
        "job_create_input",
        False,
        create("company_discovery", spec={}),
        "spec",
    )
    add(
        "invalid_create_budget_zero",
        "job_create_input",
        False,
        create("company_discovery", budget_usd=0),
        "budget_usd",
    )
    add(
        "invalid_create_budget_too_high",
        "job_create_input",
        False,
        create("company_discovery", budget_usd=v13.BUDGET_USD_MAX + 1),
        "budget_usd",
    )
    for name, kind, over, path in (
        (
            "discovery_max_companies",
            "company_discovery",
            {"max_companies": v13.DISCOVERY_MAX_COMPANIES + 1},
            "params.max_companies",
        ),
        (
            "discovery_region_code",
            "company_discovery",
            {"region_codes": ["Jawa Barat"]},
            "params.region_codes.0",
        ),
        ("import_mode", "company_import", {"mode": "overwrite"}, "params.mode"),
        ("contact_finder_both_scopes", "contact_finder", {"company_ids": [COMPANY]}, "params"),
        ("social_duplicate_source", "social_listening", {"sources": ["x", "x"]}, "params"),
        ("social_source_web", "social_listening", {"sources": ["web"]}, "params.sources.0"),
        ("strategy_empty_objective", "strategy", {"objective": "  "}, "params.objective"),
        (
            "opportunity_no_products",
            "opportunity_scoring",
            {"product_ids": []},
            "params.product_ids",
        ),
        ("content_platform", "content_bank", {"platforms": ["facebook"]}, "params.platforms.0"),
        (
            "content_item_count",
            "content_bank",
            {"item_count": v13.CONTENT_MAX_ITEMS + 1},
            "params.item_count",
        ),
    ):
        add(
            f"invalid_params_{name}",
            "job_create_input",
            False,
            create(kind, params=params(kind, **over)),
            path,
        )
    add(
        "invalid_page_limit",
        "page_input",
        False,
        {"limit": v13.PAGE_LIMIT_MAX + 1},
        "limit",
    )
    add("invalid_page_meta_negative", "page_meta", False, {"total_estimate": -1}, "total_estimate")
    add(
        "invalid_job_view_paused_without_info",
        "job_view",
        False,
        job_view("paused"),
        "",
    )
    add(
        "invalid_job_view_status",
        "job_view",
        False,
        job_view("collecting"),
        "status",
    )
    add(
        "invalid_event_unknown_type",
        "event",
        False,
        event("ci.company.exploded", {}),
        "event_type",
    )
    add(
        "invalid_event_paused_reason",
        "event",
        False,
        event("intel.job.paused", {"stage": "discovery", "reason": "proxy", "message": "x"}),
        "data.reason",
    )
    add(
        "invalid_event_job_without_job_id",
        "event",
        False,
        event(
            "intel.job.paused",
            {"stage": "discovery", "reason": "manual", "message": "x"},
            job_id=None,
        ),
        "",
    )
    add(
        "invalid_event_missing_workspace",
        "event",
        False,
        {k: v for k, v in event("ct.batch.ready", {}).items() if k != "workspace_id"},
        "workspace_id",
    )
    add(
        "invalid_event_import_counts",
        "event",
        False,
        event(
            "ci.import.status",
            {
                "batch_id": BATCH,
                "status": "completed",
                "rows_total": 10,
                "rows_ok": 9,
                "rows_review": 2,
                "rows_failed": 0,
            },
        ),
        "data",
    )
    add(
        "invalid_event_company_action",
        "event",
        False,
        event(
            "intel.company.upserted",
            {"company_id": COMPANY, "name": "x", "action": "deleted", "origin": "import"},
        ),
        "data.action",
    )


def build() -> list[dict[str, Any]]:
    fx: list[dict[str, Any]] = []

    def add(name: str, schema: str, valid: bool, payload: Any, path: str | None = None) -> None:
        item: dict[str, Any] = {"name": name, "schema": schema, "valid": valid, "payload": payload}
        if path is not None:
            item["expect_path"] = path
        fx.append(item)

    _valid(add)
    _invalid(add)
    return fx


def _path(loc: tuple[Any, ...]) -> str:
    """Path error gaya Zod: buang tag union (`params.company_discovery.x` -> `params.x`)."""
    parts: list[str] = []
    for i, p in enumerate(loc):
        if i == 1 and loc[0] == "params" and p in v13.JOB_KINDS:
            continue
        parts.append(str(p))
    return ".".join(parts)


def check(fx: dict[str, Any]) -> list[str]:
    """Kembalikan daftar path error (kosong = valid) memakai Pydantic."""
    from pydantic import ValidationError

    try:
        if fx["schema"] == "event":
            env = v13.EventEnvelope13.model_validate(fx["payload"])
            try:
                v13.validate_event_data_v13(env.event_type, env.data)
            except ValidationError as exc:
                return [
                    "data." + _path(tuple(e["loc"])) if e["loc"] else "data" for e in exc.errors()
                ]
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
        if not fx["valid"] and errors and not any(e.startswith(fx["expect_path"]) for e in errors):
            problems.append(f"{fx['name']}: path {errors} != {fx['expect_path']}")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    write(fixtures)
    print(f"{len(fixtures)} fixture ditulis ke {OUT}")
    if args.dashboard:
        target = args.dashboard / "contracts" / "1.3"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(OUT, target)
        print(f"disalin ke {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
