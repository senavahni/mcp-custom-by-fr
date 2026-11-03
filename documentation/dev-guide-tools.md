# Panduan Developer — Membuat Tool yang Hemat Token

Semua tool lewat **pipeline yang sama** (klien MCP maupun agent runtime), sehingga kompaksi, cache,
budget, dan audit otomatis. Tugas developer: input/output schema yang jelas dan data besar sebagai handle.

## Contoh lengkap

```python
# mcp_marketing/tools/social_demo.py  (daftarkan nama modul di mcp_marketing/tools/__init__.py)
from typing import Any
from pydantic import Field
from mcp_marketing.schemas.common import Envelope, ReadInput
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.results.result_store import Owner


class PostsIn(ReadInput):                       # ReadInput = response_format + cache
    keyword: str = Field(min_length=2, max_length=100)
    limit: int = Field(default=10, ge=1, le=20)  # maks 20 inline


@tool(
    name="demo_posts",                           # <toolset>_<verb>[_<obj>]
    toolset="social",
    summary="Contoh: post untuk satu kata kunci",             # <= 80 char (toolbox_list)
    description="Return posts for a keyword as a handle plus top samples and stats.",  # <= 300 char, EN
    input_model=PostsIn,
    output_model=Envelope,
    cache_ttl_s=3_600,                           # hasil deterministik -> cache
)
async def demo_posts(inp: PostsIn, ctx: ToolContext) -> dict[str, Any]:
    rows = await fetch_rows(inp.keyword)          # data mentah TIDAK dikirim ke LLM
    rs = await ctx.services.results.create(
        kind="social_posts", columns=["id", "posted_at", "author", "text", "likes"], rows=rows,
        owner=Owner(client_id=ctx.client.id, run_id=ctx.run_id), sort_ts="posted_at", sort_num="likes",
    )
    top = sorted(rows, key=lambda r: r["likes"], reverse=True)[: inp.limit]
    return {
        "summary": f"{rs.total} post untuk '{inp.keyword}'",   # <= 300 char
        "handle": rs.handle,
        "total": rs.total,
        "stats": {"likes_avg": ..., "top_authors": [...]},     # agregat dihitung di Python/SQL
        "items": {"cols": ["id", "author", "text", "likes"],
                  "rows": [[r["id"], r["author"], r["text"], r["likes"]] for r in top]},
        "cost": {"llm_tokens": 0, "external_units": len(rows), "cache_hit": False},
    }
```

## Aturan (MCP_SPEC §3)

| Aturan | Cara di kode |
|---|---|
| T1 kompaksi | otomatis oleh pipeline (`services/results/compact.py`); field yang boleh dibuang pertama: `drop_first=(...)` di `@tool` |
| T2 handle | data > 20 baris -> `results.create(...)`; bila lupa, pipeline memindahkan otomatis + log `tool.auto_handle` (perbaiki tool-nya) |
| T3 proyeksi | kolom `items` minimal; klien memilih kolom lewat `data_get(fields=…)` |
| T4 agregasi | hitung di server; LLM memakai `data_aggregate` |
| T6 model termurah | panggil LLM via `ctx.services.llm.chat(purpose=…)`; purpose menentukan route (lihat [llm-usage.md](llm-usage.md)) |
| T7 cache | `cache_ttl_s`; tool yang menulis -> `writes=True` (tidak pernah di-cache) |
| T8 budget | selalu teruskan `budget=ctx.budget` ke `llm.chat`; tambahkan token ke `ctx.llm_tokens` |
| T11 no echo | jangan mengulang input panjang di output |
| Pekerjaan > 25 s | kembalikan `JobAccepted` dari `ctx.services.jobs.enqueue(...)` + `returns_job=True` |
| Error | lempar subclass `ToolError` (`ValidationFailed`, `NotFound`, `UpstreamError`, …); jangan string bebas |
| Konten eksternal | toolset `web`/`social` otomatis dibungkus `<untrusted_content>` saat dikirim ke agent |

## Checklist sebelum merge
- Input & output model Pydantic; deskripsi ≤ 300 char, summary ≤ 80 char (`tests/unit/test_registry.py`).
- Unit test: sukses, validasi gagal, error upstream; output `concise` ≤ 1.500 token.
- Contoh pemakaian ditambahkan di `MCP_SPEC.md`; env baru di `.env.example` + ARCHITECTURE §8.
