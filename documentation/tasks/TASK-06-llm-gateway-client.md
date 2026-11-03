# TASK-06 — Klien LLM Gateway & Route Policy

| Item | Nilai |
|---|---|
| Milestone | M1 Platform |
| Prioritas | P0 |
| Estimasi | 2 hari |
| Dependensi | TASK-02 (TASK-05 untuk cache & budget) |
| Referensi | MCP_SPEC §6 · PRD FR-07 · `llm-genai/docs/client-integration.md` |

## Tujuan
Satu klien untuk semua panggilan LLM & embedding melalui LLM Gateway, yang memilih route paling hemat sesuai jenis pekerjaan, mendukung JSON terstruktur dan tool calling, menangani error Gateway dengan benar, serta mencatat usage per panggilan.

## Spesifikasi

### Prasyarat di Gateway (dikerjakan sekali oleh admin Gateway)
- Project `mcp-marketing` + API key (`manage.py create-project --slug mcp-marketing` lalu `create-key`), scope `chat`, `embeddings`, `models:read`, `usage:read`; kuota harian disepakati.
- Cek `GET /v1/models` memuat route `fast`, `smart`, `embed-default` pada profil aktif.

### `services/llm/gateway_client.py`
- `AsyncOpenAI(base_url=LLM_GATEWAY_URL, api_key=LLM_GATEWAY_API_KEY, timeout=LLM_TIMEOUT_S, max_retries=0)` — retry ditangani sendiri.
- API:
```python
async def chat(purpose: Purpose, messages, *, tools=None, json_schema: type[BaseModel] | None = None,
               max_tokens=None, temperature=None, route_override=None) -> ChatResult
async def chat_json(purpose, messages, schema: type[T], *, repair_attempts=1) -> tuple[T, ChatResult]
async def embed(texts: list[str], *, model=None) -> list[list[float]]
```
- `ChatResult`: `content`, `tool_calls`, `finish_reason`, `usage{prompt, completion}`, `model_resolved` (field `model` respons), `gateway_request_id` (header respons), `latency_ms`, `cached`.
- `chat_json`: kirim `response_format={"type":"json_object"}` (atau `json_schema` bila route mendukung), parse dengan Pydantic; gagal parse → satu kali **repair** (route `fast`, kirim error validasi ringkas + output sebelumnya dipotong 2.000 char) → gagal lagi → `UPSTREAM_ERROR` dengan `details.reason="invalid_json"`.
- Header keluar: `X-Request-Id`; parameter `user = f"{client_name}:{run_id or '-'}"`.

### Route policy (`services/llm/route_policy.py`)
Tabel MCP_SPEC §6 sebagai data (`Purpose → RouteConfig(route, temperature, max_tokens, json)`), bisa dioverride lewat env (`LLM_ROUTE_FAST`, `LLM_ROUTE_SMART`, `LLM_ROUTE_EMBED`) dan per profil agent.

### Error & retry
| Respons Gateway | Tindakan |
|---|---|
| 429 `rate_limited` / `quota_exceeded` | retry dengan `Retry-After` (maks 2×); quota → `BUDGET_EXCEEDED` (gateway) non-retryable |
| 502 `all_candidates_failed`, `upstream_error` | retry backoff 1 s, 3 s (maks 2×) → `UPSTREAM_ERROR retryable:true` |
| 504 `upstream_timeout` | retry 1× dengan `max_tokens` dikurangi 30% → `UPSTREAM_TIMEOUT` |
| 400/422 | tidak retry → `UPSTREAM_ERROR retryable:false` (pesan ringkas) |
| 401/403 | tidak retry → log `critical` (key Gateway salah) |

### Pencatatan & budget
- Setiap panggilan: `budget.reserve(estimate)` (estimasi = token prompt + `max_tokens`) → panggil → `budget.commit(usage)` → insert `mcp.llm_calls` (async).
- Cache LLM (TASK-05) untuk `temperature == 0` dan tanpa `tools`.

### Embedding
- Batch 64, potong teks > 8.000 token (log warning), cache `embedding_cache` per `sha256(text)+model`.
- `EMBED_PROVIDER=local`: adapter ONNX (`services/kb/embedder.py`, TASK-19) dengan antarmuka sama.

### Prompt library (`services/llm/prompts/`)
- Prompt disimpan sebagai file `.md`/Jinja kecil dengan versi (`estimate.v1.md`, …); `render(name, **vars)`; versi prompt dicatat di `llm_calls` (`purpose` + `prompt_version` dalam kolom metadata atau `purpose` berformat `estimate@v1`).

## Checklist Implementasi
- [ ] Setup project & key di Gateway; tambahkan ke `.env.example`.
- [ ] `gateway_client` (chat, chat_json, embed) + error mapping + retry.
- [ ] Route policy + override.
- [ ] Integrasi budget, cache, `llm_calls`.
- [ ] Prompt library + versi.
- [ ] `scripts/smoke_gateway.py`: cek `/v1/models`, satu chat `fast`, satu embed.

## Pengujian
- `respx` mock Gateway: sukses, 429 + Retry-After, 502 lalu sukses, 504, JSON invalid → repair sukses / gagal.
- Tool calling: respons dengan `tool_calls` diparse ke struktur internal.
- Usage tercatat dan budget berkurang sesuai `usage` respons.
- `-m live`: smoke terhadap Gateway lokal sungguhan.

## Acceptance Criteria
1. `smoke_gateway.py` lulus terhadap Gateway profil `remote-only`.
2. 100% panggilan LLM di kode MCP lewat `gateway_client` (test lint: tidak ada import `openai` di luar modul ini).
3. Usage di `mcp.llm_calls` cocok (±0) dengan `GET /v1/usage` Gateway untuk 10 panggilan uji.
4. JSON invalid diperbaiki otomatis ≥ 90% pada 50 sampel uji (live, route `fast`).

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi.
- [ ] Coverage `services/llm/` ≥ 90%.
- [ ] Dokumen `docs/llm-usage.md`: route per purpose, cara menambah prompt.
- [ ] Review disetujui.
