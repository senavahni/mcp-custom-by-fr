# TASK-04 — MCP Server Core: Transport, Auth, Toolset Registry

| Item | Nilai |
|---|---|
| Milestone | M1 Platform |
| Prioritas | P0 |
| Estimasi | 3 hari |
| Dependensi | TASK-02, TASK-03 |
| Referensi | MCP_SPEC §1, §2 · PRD FR-01, FR-02, FR-03, FR-27 · README D2, D13 |

## Tujuan
Server MCP Streamable HTTP di `:4100/mcp` dengan autentikasi API key, registry toolset yang mengatur visibilitas tool per klien, dan pipeline middleware standar untuk setiap tool call.

## Spesifikasi

### Aplikasi (`mcp_marketing/main.py`)
- `FastMCP("mcp-marketing", stateless_http=True, json_response=True)`; app ASGI dari `streamable_http_app()` di-mount di `/mcp` dalam Starlette bersama route `/health/live`, `/health/ready`, `/metrics` (TASK-24), `/admin/*`.
- Lifespan: buka engine DB, HTTP client bersama (`httpx.AsyncClient`), muat registry, mulai background task (outbox sender TASK-08, run supervisor TASK-10).
- Middleware ASGI: `RequestContextMiddleware` (buat/terima `X-Request-Id`), `BodyLimitMiddleware` (2 MB), `AuthMiddleware`.

### Autentikasi (`server/auth.py`)
- Ambil `Authorization: Bearer …`.
  - Bila sama (constant-time) dengan `MCP_SERVER_API_KEY` → klien sistem `dashboard`.
  - Bila format `mcpk_<8>_<43>` → lookup `api_clients` by prefix (cache in-memory TTL 60 s), verifikasi `HMAC_SHA256(pepper, key)`, cek `status`, `expires_at`.
  - Lainnya → 401 JSON `{"error":{"code":"UNAUTHORIZED"}}` sebelum masuk MCP.
- Rate limit sederhana per klien (token bucket in-memory, `rpm_limit`) → error tool `RATE_LIMITED retryable:true`.
- Update `last_used_at` throttled (maks 1×/menit).

### Registry (`server/registry.py`)
```python
@dataclass(frozen=True)
class ToolSpec:
    name: str; toolset: str; summary: str        # summary ≤ 80 char untuk toolbox_list
    description: str                             # ≤ 300 char (diuji)
    input_model: type[BaseModel]; output_model: type[BaseModel]
    handler: Callable[[BaseModel, ToolContext], Awaitable[BaseModel | JobAccepted]]
    cache_ttl_s: int | None = None; default_fields: tuple[str, ...] = ()
    admin_only: bool = False; writes: bool = False
```
- Dekorator `@tool(toolset="social", …)` mendaftarkan `ToolSpec` dan sekaligus `FastMCP.tool(...)` dengan input schema dari Pydantic dan **output schema** (sehingga klien menerima `structuredContent`).
- `ToolContext`: `client`, `request_id`, `run_id?`, `session` factory, `settings`, `budget` (TASK-05), `llm` (TASK-06), `dashboard` (TASK-08), `jobs` (TASK-07).

### Visibilitas
- Override handler `tools/list`: filter `ToolSpec` dengan `toolset ∈ client.toolsets` dan (`admin_only` → `client.is_admin`); query `?toolsets=` mempersempit.
- `tools/call` untuk tool di luar scope → `FORBIDDEN` (tool memang tidak terlihat; tetap ditolak bila dipanggil langsung).
- Dashboard (klien `dashboard`) hanya melihat toolset `pm`.

### Pipeline per tool call (`server/pipeline.py`)
Urutan: `bind_context` → validasi input (Pydantic; error ringkas) → cek scope → **cache lookup** (TASK-05) → **budget pre-check** (TASK-05) → handler (timeout 25 s) → **compact output** (TASK-05) → audit `tool_calls` (async, tidak memblokir) → kembalikan `CallToolResult(structuredContent=…, content=[TextContent(summary)])`. Exception → `ToolError` → `isError: true`.

### Tool `toolbox_list`
- Input `toolset?`; output daftar toolset yang terlihat + `[{name, summary}]`. Tanpa schema (hemat token).

### Admin minimal (`/admin`, key `is_admin`)
- `GET /admin/clients`, `POST /admin/clients` (kembalikan key sekali), `POST /admin/clients/{id}/revoke`.
- CLI setara di `manage.py`: `create-client`, `list-clients`, `revoke-client`, `rotate-client`.

## Checklist Implementasi
- [ ] App Starlette + mount FastMCP + lifespan.
- [ ] Middleware context, body limit, auth (+ cache, rate limit).
- [ ] Registry + dekorator `@tool` + `ToolContext`.
- [ ] Filter `tools/list` & penolakan `tools/call`.
- [ ] Pipeline + audit async.
- [ ] `toolbox_list`.
- [ ] Admin endpoint & CLI klien.
- [ ] Tool contoh `toolbox_ping` (dev only) untuk test.

## Pengujian
- Klien MCP resmi (`mcp.client.streamable_http`) terhadap server uvicorn sungguhan (fixture): `initialize` → `tools/list` → `tools/call`.
- Tanpa header → 401; key revoked → 401 (≤ 60 s setelah revoke, test dengan TTL di-mock).
- Klien dengan toolset `trends` tidak melihat tool `social`; memanggil `x_search` → `FORBIDDEN`.
- Dashboard key melihat tepat 5 tool `pm_*`.
- Error validasi → `isError:true`, `structuredContent.code == "VALIDATION_ERROR"`.
- Test deskripsi: semua `ToolSpec.description` ≤ 300 karakter, `summary` ≤ 80.

## Acceptance Criteria
1. Dashboard (`@modelcontextprotocol/sdk` `StreamableHTTPClientTransport`) dapat `initialize` & `tools/list` ke `http://localhost:4100/mcp` dengan `MCP_SERVER_API_KEY` (diuji manual dari repo Dashboard).
2. Claude Desktop/MCP Inspector dapat terhubung memakai key `mcpk_…` dan hanya melihat toolset sesuai scope.
3. Overhead pipeline (tool no-op) p95 < 20 ms lokal.
4. Setiap tool call tercatat di `mcp.tool_calls` dengan `request_id`, `client_id`, `status`, `duration_ms`.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi.
- [ ] Test unit + integration lulus; coverage `server/` ≥ 85%.
- [ ] MCP_SPEC §1–2 sesuai implementasi.
- [ ] Review disetujui.
