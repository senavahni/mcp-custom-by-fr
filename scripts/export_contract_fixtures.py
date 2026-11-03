"""Bangkitkan fixture payload kontrak (valid & invalid) ke tests/contract/fixtures/ (TASK-08).

    python scripts/export_contract_fixtures.py

Fixture valid dibangun lewat model Pydantic MCP (harus lolos Zod Dashboard); fixture invalid
dibangun mentah dan HARUS ditolak oleh validator MCP maupun Zod. Validasi Zod dijalankan oleh
`tests/contract/validate_with_zod.cjs` (memakai build `dist/` repo Dashboard).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import orjson

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_marketing.schemas.blocks import text_inline  # noqa: E402
from mcp_marketing.services.templates.md_to_blocks import markdown_to_blocks  # noqa: E402

OUT = ROOT / "tests" / "contract" / "fixtures"
RUN = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"
TASK = "33333333-3333-4333-8333-333333333333"
USER = "44444444-4444-4444-8444-444444444444"

SAMPLE_MD = """## Ringkasan
Kalender konten **3 minggu** untuk [brand](https://example.com).

- Pilar: edukasi
- Pilar: promo

1. Riset
2. Produksi

- [ ] Draft minggu 1
- [x] Brief disetujui

> Catatan: hindari klaim berlebihan.

```python
print("halo")
```

| Minggu | Tema |
|---|---|
| 1 | Edukasi |
| 2 | Promo |

---
"""


def envelope(event_type: str, data: dict[str, Any], **over: Any) -> dict[str, Any]:
    env = {
        "contract_version": "1.0",
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "occurred_at": "2026-11-03T03:15:00Z",
        "run_id": RUN,
        "agent_id": AGENT,
        "task_id": TASK,
        "data": data,
    }
    env.update(over)
    return env


def append(block_type: str, content_json: Any) -> dict[str, Any]:
    return {"op": "append", "block": {"block_type": block_type, "content_json": content_json}}


def build() -> list[dict[str, Any]]:
    from mcp_marketing.schemas.contract import validate_event_data

    blocks = markdown_to_blocks(SAMPLE_MD)
    extra = [
        {
            "block_type": "image",
            "content_json": {"props": {"url": "https://example.com/a.png", "caption": "x"}},
        },
        {
            "block_type": "bookmark",
            "content_json": {"props": {"url": "https://example.com", "title": "Ref"}},
        },
        {
            "block_type": "aiPrompt",
            "content_json": {"props": {"prompt": "Buat ringkasan", "agent_id": AGENT}},
        },
        {"block_type": "toggleListItem", "content_json": {"content": [text_inline("Detail")]}},
        {
            "block_type": "paragraph",
            "content_json": {
                "content": [
                    {
                        "type": "mention",
                        "props": {"mention_type": "user", "id": USER, "label": "Owner"},
                    },
                    text_inline(" tolong cek", italic=True),
                ]
            },
        },
    ]
    ops = [{"op": "append", "block": b} for b in [*blocks, *extra]]
    valid_events = {
        "started": ("agent.task.started", {}),
        "status_working": (
            "agent.status.changed",
            {"status": "working", "message": "Membaca brief…"},
        ),
        "block_appended_all_types": ("agent.block.appended", {"operations": ops}),
        "block_update": (
            "agent.block.appended",
            {
                "operations": [
                    {
                        "op": "update",
                        "expected_version": 3,
                        "block": {
                            "id": str(uuid.uuid4()),
                            "block_type": "paragraph",
                            "content_json": {"props": {}, "content": [text_inline("revisi")]},
                        },
                    }
                ]
            },
        ),
        "subtask_created": (
            "agent.subtask.created",
            {
                "subtasks": [
                    {
                        "client_ref": "week-1",
                        "title": "Draft Week 1",
                        "priority": "medium",
                        "start_date": "2026-11-02",
                        "end_date": "2026-11-03",
                        "tags": ["content"],
                        "assign_to_self": True,
                        "initial_blocks": [
                            {
                                "block_type": "paragraph",
                                "content_json": {"content": [text_inline("…")]},
                            }
                        ],
                    }
                ]
            },
        ),
        "comment_added": (
            "agent.comment.added",
            {"content": "Selesai, cek Week 1.", "mentions": [{"type": "user", "id": USER}]},
        ),
        "completed": ("agent.task.completed", {"summary": "Kalender 3 minggu dibuat."}),
        "failed": (
            "agent.task.failed",
            {"error_code": "BUDGET_EXCEEDED", "message": "Budget habis", "retryable": False},
        ),
    }
    fixtures: list[dict[str, Any]] = []
    for name, (event_type, data) in valid_events.items():
        fixtures.append(
            {
                "name": f"valid_{name}",
                "kind": "event",
                "valid": True,
                "payload": envelope(event_type, validate_event_data(event_type, data)),
            }
        )

    invalid_events = {
        "ops_51": ("agent.block.appended", {"operations": [append("divider", {})] * 51}),
        "ops_empty": ("agent.block.appended", {"operations": []}),
        "heading_level_4": (
            "agent.block.appended",
            {"operations": [append("heading", {"props": {"level": 4}})]},
        ),
        "image_javascript_url": (
            "agent.block.appended",
            {"operations": [append("image", {"props": {"url": "javascript:alert(1)"}})]},
        ),
        "unknown_block_type": ("agent.block.appended", {"operations": [append("video", {})]}),
        "table_51_rows": (
            "agent.block.appended",
            {
                "operations": [
                    append(
                        "table",
                        {
                            "content": {
                                "type": "tableContent",
                                "rows": [{"cells": [[text_inline("x")]]}] * 51,
                            }
                        },
                    )
                ]
            },
        ),
        "bad_color": (
            "agent.block.appended",
            {"operations": [append("paragraph", {"props": {"textColor": "red;"}})]},
        ),
        "subtasks_21": (
            "agent.subtask.created",
            {"subtasks": [{"title": f"T{i}"} for i in range(21)]},
        ),
        "subtask_bad_date": (
            "agent.subtask.created",
            {"subtasks": [{"title": "T", "start_date": "2026-13-01"}]},
        ),
        "subtask_empty_title": ("agent.subtask.created", {"subtasks": [{"title": "   "}]}),
        "comment_too_long": ("agent.comment.added", {"content": "x" * 10_001}),
        "status_idle": ("agent.status.changed", {"status": "idle"}),
        "failed_no_code": ("agent.task.failed", {"message": "x"}),
    }
    for name, (event_type, data) in invalid_events.items():
        fixtures.append(
            {
                "name": f"invalid_{name}",
                "kind": "event",
                "valid": False,
                "payload": envelope(event_type, data),
            }
        )
    fixtures.append(
        {
            "name": "invalid_contract_2",
            "kind": "event",
            "valid": False,
            "payload": envelope("agent.task.started", {}, contract_version="2.0"),
        }
    )
    fixtures.append(
        {
            "name": "invalid_event_type",
            "kind": "event",
            "valid": False,
            "payload": envelope("agent.task.paused", {}),
        }
    )

    outputs = [
        ("valid_dispatch", "dispatch", {"accepted": True, "mcp_run_id": "mr_0123456789"}, True),
        (
            "valid_estimation",
            "estimation",
            {
                "start_date": "2026-11-03",
                "end_date": "2026-11-05",
                "estimated_days": 3,
                "confidence": 0.7,
                "message": "Siap.",
            },
            True,
        ),
        ("valid_cancel", "cancel", {"cancelled": True}, True),
        (
            "invalid_estimation_date",
            "estimation",
            {"start_date": "03-11-2026", "end_date": "2026-11-05"},
            False,
        ),
        (
            "invalid_confidence",
            "estimation",
            {"start_date": "2026-11-03", "end_date": "2026-11-05", "confidence": 1.5},
            False,
        ),
    ]
    for name, schema, payload, valid in outputs:
        fixtures.append(
            {"name": name, "kind": "output", "schema": schema, "valid": valid, "payload": payload}
        )
    return fixtures


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    fixtures = build()
    for old in OUT.glob("*.json"):
        old.unlink()
    for f in fixtures:
        (OUT / f"{f['name']}.json").write_bytes(orjson.dumps(f, option=orjson.OPT_INDENT_2))
    print(f"{len(fixtures)} fixture ditulis ke {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
