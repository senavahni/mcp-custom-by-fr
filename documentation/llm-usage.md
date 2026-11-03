# Pemakaian LLM — Route per Purpose & Prompt

Semua panggilan LLM lewat `mcp_marketing/services/llm/gateway_client.py` (diuji: tidak ada `import openai`
di modul lain). Gateway yang memilih model lokal/remote untuk route `fast`/`smart`/`embed-default`.

## Route per purpose (`services/llm/route_policy.py`)

| Purpose | Route | temperature | max_tokens | JSON |
|---|---|---|---|---|
| `classify`, `extract`, `estimate`, `summarize_short` | `fast` | 0 | 800 | ya |
| `sentiment_fallback` | `fast` | 0 | 800 | tidak (format baris) |
| `reasoning`, `strategy`, `report` | `smart` | 0.3 | 4.000 | ya |
| `agent_loop` | profil agent (default `smart`; mode ringan `fast`) | 0.3 | 2.000 | tidak (tool calling) |
| `compact` (ringkas riwayat agent) | `fast` | 0 | 400 | tidak |
| `repair` (perbaikan JSON) | `fast` | 0 | 800 | ya |
| embedding | `embed-default` | — | batch 64 | — |

Nama route Gateway bisa diganti lewat env `LLM_ROUTE_FAST`, `LLM_ROUTE_SMART`, `LLM_ROUTE_EMBED`;
`route_override` menerima `fast`/`smart` atau nama route langsung.

## Perilaku klien
- Budget: `reserve(prompt + max_tokens)` sebelum panggilan, `commit(usage)` sesudahnya -> `BUDGET_EXCEEDED`
  muncul **sebelum** token terbuang.
- Cache: hanya `temperature == 0` dan tanpa `tools` (TTL `LLM_CACHE_TTL_S`).
- Retry: 429 -> `Retry-After` (maks 2×; `quota_exceeded` -> `BUDGET_EXCEEDED`), 5xx/koneksi -> 1 s, 3 s,
  504/timeout -> 1× dengan `max_tokens` −30 %, 400/422/401/403 -> tidak di-retry.
- Header: `X-Request-Id` unik per panggilan (primary key request di Gateway), `X-Correlation-Id` = request
  id MCP, `user = <client_name>:<run_id>`.
- Setiap panggilan dicatat di `mcp.llm_calls` (`purpose@versi_prompt`, route, model, token, latency).

## Menambah prompt
1. Buat file `mcp_marketing/services/llm/prompts/<nama>.v1.md` dengan placeholder `{{var}}`.
2. Pakai: `prompts.render("<nama>", version="v1", var=...)` lalu
   `llm.chat_json("<purpose>", messages, Schema, prompt_version="v1", budget=ctx.budget)`.
3. Mengubah prompt = buat `v2` (jangan edit `v1`) agar pemakaian token per versi bisa dibandingkan.

Prasyarat Gateway: project `mcp-marketing` + key (scope `chat`, `embeddings`, `models:read`,
`usage:read`) lalu `scripts\conda-run.bat python scripts\smoke_gateway.py`.
