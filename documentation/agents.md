# Agent — Menambah Agent, Menyetel Prompt & Budget

## Cara kerja singkat
Dashboard memanggil `pm_dispatch_task_to_agent` / `pm_notify_agent_mention` / `pm_execute_prompt_block`
→ MCP menyimpan run (`mcp.runs`), mengirim `agent.task.started`, lalu agent runtime menjalankan loop:

```
system(profil + aturan) + brief terpadatkan -> LLM (route profil, tools = toolset profil + tool virtual)
  -> tool_calls: tool baca paralel (≤ 3), tool tulis berurutan -> hasil ringkas kembali ke LLM
  -> riwayat > AGENT_HISTORY_COMPACT_AT_TOKENS -> diringkas (fast), 2 langkah terakhir utuh
  -> finish(summary) -> komentar ringkasan + agent.task.completed
```
Tool virtual (menulis ke Dashboard via webhook): `report_progress`, `write_blocks` (markdown → blok),
`create_subtasks`, `post_comment`, `finish`, `fail`. Agent hanya bisa menulis ke task run & turunannya.
Mode ringan (prompt block / mention sederhana ≤ 500 char tanpa kata data/riset): maks 6 langkah, toolset
`kb` + `pm_agent`, route `fast`.

## Menambah agent baru

### Lewat Admin Console (tanpa deploy)
Konsol MCP → **Agent** → **+ Agent baru**: isi key agent, nama, prompt peran (aturan umum
`agent_rules` digabung otomatis), route/model, toolset, budget token, dan maks langkah. Route boleh
`fast`/`smart` atau alias model Gateway (mis. `openrouter/gemma-4-31b`; izinkan polanya di project
Gateway). Ubah/nonaktifkan dari halaman yang sama; berlaku untuk run berikutnya. API:
`GET/POST /admin/agents`, `PATCH /admin/agents/{agent_key}` (klien admin). Lalu buat agent di
Dashboard dengan `mcp_agent_key` yang sama, dan beri key LLM (menu Key LLM per agent).

### Lewat kode (profil bawaan)
1. **Prompt peran**: `mcp_marketing/services/llm/prompts/agent_<key>.v1.md` (aturan umum di
   `agent_rules.v1.md` otomatis digabung; total ≤ 600 token, diuji).
2. **Profil**: tambahkan `ProfileSeed` di `mcp_marketing/services/agent/profiles.py` (toolsets,
   `tool_denylist`, route, `run_token_budget`, `max_steps`), lalu `manage.py seed`.
3. **Dashboard**: Admin → Agents → buat agent dengan `mcp_agent_key = <key>` yang sama.

## Menyetel di produksi (tanpa deploy)
```sql
UPDATE mcp.agent_profiles
SET run_token_budget = 150000, max_steps = 30, route = 'smart', updated_at = now(), version = version + 1
WHERE agent_key = 'researcher';
```
`manage.py seed` tidak menimpa profil yang sudah ada; `manage.py seed --force` menimpa dari kode.

## Kode kegagalan run (`agent.task.failed`)
`BUDGET_EXCEEDED`, `LLM_UNAVAILABLE` (retryable), `MAX_STEPS`, `RUN_TIMEOUT` (retryable),
`WEBHOOK_REJECTED`, `UNKNOWN_AGENT`, `INTERNAL`, atau kode dari `fail(error_code, …)` agent.
Pantau token per run: `SELECT run_id, steps, tokens_used, token_budget, status FROM mcp.runs ORDER BY created_at DESC;`
