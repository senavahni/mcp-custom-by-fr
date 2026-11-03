# TASK-08 — Dashboard Client: Schema Kontrak, Agent API & Webhook Outbox

| Item | Nilai |
|---|---|
| Milestone | M1 Platform |
| Prioritas | P0 |
| Estimasi | 4 hari |
| Dependensi | TASK-03, TASK-05 |
| Referensi | `M1_MCP_INTEGRATION.md` §1, §2, §5, §6, §7, §8 · `backend/src/shared/schemas/mcp.schema.ts`, `block.schema.ts` · MCP_SPEC §5 |

## Tujuan
Lapisan komunikasi MCP → Dashboard yang patuh kontrak 1.0: model Pydantic yang setara dengan Zod Dashboard, klien Agent API, dan pengirim webhook ber-HMAC yang idempotent, berurutan per run, tahan Dashboard down, dan berhenti saat run tidak aktif.

## Spesifikasi

### 1. Schema kontrak (`schemas/contract.py`, `schemas/blocks.py`)
- Port 1:1 dari `mcp.schema.ts` & `block.schema.ts`: `TaskSnapshot`, `ContextBlock`, input/output 5 tool `pm_*`, `WebhookEnvelope`, data tiap event (`agent.task.started` … `agent.task.failed`), `BlockInput`, `BLOCK_SCHEMAS` per `block_type` (paragraph, heading, toggleListItem, bulletListItem, numberedListItem, checkListItem, quote, codeBlock, table, image, divider, bookmark, aiPrompt) termasuk batas (teks 10.000, inline ≤ 2.000, tabel ≤ 50 baris × 20 kolom, URL http/https).
- Batas kontrak sebagai konstanta: `MAX_BLOCK_OPS=50`, `MAX_SUBTASKS=20`, `MAX_DEPTH=3`, `MAX_BODY=1_000_000`, `MAX_COMMENT=10_000`, `MAX_BLOCK_JSON=65_536`.
- `scripts/export_contract_fixtures.py`: membangkitkan contoh payload valid/invalid ke `tests/contract/fixtures/*.json`. Di repo Dashboard, fixture divalidasi terhadap Zod (lihat Pengujian) → menjamin kesetaraan.

### 2. Agent API client (`services/dashboard/agent_api.py`)
- Base dari `callback.agent_api_base_url` run (fallback `DASHBOARD_API_URL + /agent-api`), Bearer `MCP_SERVICE_TOKEN`.
- `get_run(run_id)`, `get_task(run_id, task_id)`, `get_project_brief(run_id)`, `query_context(run_id, query, scope, task_id, top_k)`.
- 409 `RUN_NOT_ACTIVE` → raise `RunNotActive` (runtime menghentikan agent). 429 → hormati rate limit 300/menit/run (token bucket lokal 4 rps per run).
- Respons dipangkas sebelum masuk LLM (teks blok ≤ 600 char, komentar ≤ 50 terakhir → 10).

### 3. Webhook outbox (`services/dashboard/outbox.py`, `signer.py`)
- `emit(run_id, event_type, data, task_id)` → validasi data dengan schema event → insert `webhook_outbox` dengan `event_id=uuid4()`, `seq = max(seq)+1` per run (dalam transaksi, `SELECT … FOR UPDATE` pada baris `runs`).
- **Sender loop** (background task di proses server):
  - Ambil baris `pending` dengan `next_attempt_at <= now()` yang merupakan **seq terkecil yang belum delivered untuk run-nya** (satu in-flight per run, berbagai run paralel maks 8).
  - Header: `Content-Type: application/json`, `X-MCP-Timestamp: <unix>`, `X-MCP-Signature: sha256=<hex HMAC_SHA256(secret, f"{ts}.{raw_body}")>`; body = bytes yang sama persis dengan yang ditandatangani (orjson).
  - Tabel respons (kontrak §5):

| HTTP | Status outbox | Tindakan |
|---|---|---|
| 200 `processed` | `delivered` | simpan `response.result` (applied/conflicts/created/comment_id) → callback ke runtime (mis. peta `client_ref → id`) |
| 200 `duplicate` | `duplicate` | lanjut seq berikutnya |
| 400 / 404 / 422 | `failed` | jangan retry; beri tahu runtime (`OutboxRejected`) → agent memperbaiki atau gagal |
| 401 | `failed` | jangan retry; log `critical` (secret/jam salah) → run gagal `WEBHOOK_AUTH` |
| 409 `RUN_NOT_ACTIVE` | `discarded` + semua pending run tsb `discarded` | runtime stop |
| 429 / 5xx / network | `pending`, `next_attempt_at` = backoff 1,2,4,… maks 300 s | berhenti bila `now - first_attempt_at > WEBHOOK_MAX_RETRY_MINUTES` → `failed` |

  - API untuk runtime: `await outbox.emit_and_wait(…, timeout_s=30)` → mengembalikan `response.result` (dibutuhkan untuk peta subtask & conflict blok).
- Rotasi secret: `MCP_WEBHOOK_SECRET` dipakai untuk menandatangani; Dashboard menerima lama & baru selama transisi (tidak perlu dual-sign di MCP).

### 4. Pemecah batch
- Helper `chunk_block_ops(ops)` (≤ 50 per event, ≤ 900 KB body) dan `chunk_subtasks(subtasks)` (≤ 20) agar agent/tool tidak perlu memikirkan batas.

## Checklist Implementasi
- [ ] Schema Pydantic kontrak & blok + konstanta batas.
- [ ] Ekspor fixture + test kesetaraan di repo Dashboard (PR kecil ke `marketingdashboardjtp`: `backend/test/contract/mcp-fixtures.spec.ts`).
- [ ] Agent API client + rate limit + pemangkasan.
- [ ] Outbox: emit, seq, sender loop, signer, tabel respons, backoff, discard, `emit_and_wait`.
- [ ] Chunker blok & subtask.
- [ ] Metrics: `mcp_webhook_sent_total{status}`, `mcp_webhook_latency_seconds`, `mcp_outbox_pending`.

## Pengujian
- Unit: signature cocok dengan implementasi Dashboard (`mcp-signature.guard.ts`) untuk vektor uji tetap (timestamp, body, secret).
- Unit: urutan — seq 2 tidak dikirim sebelum seq 1 delivered, walau seq 1 sedang backoff.
- Integration (`respx`/server palsu): 500,500,200 → delivered dengan `event_id` sama 3×; 409 → seluruh pending run `discarded`.
- Contract (`-m contract`, Dashboard lokal + `npm run mcp:send-event`-style): kirim 7 tipe event untuk run nyata → Dashboard memproses (task `done`, blok tampil, subtask dibuat).
- Restart server saat 5 event pending → semua terkirim berurutan setelah start.

## Acceptance Criteria
1. Semua fixture payload MCP lolos validasi Zod Dashboard; semua fixture invalid ditolak dengan alasan yang sama.
2. Dashboard dimatikan 10 menit lalu dinyalakan → seluruh event terkirim berurutan tanpa duplikasi pemrosesan.
3. Tidak ada event yang dikirim setelah 409 `RUN_NOT_ACTIVE` untuk run tersebut.
4. Signature diterima Dashboard (0 respons 401 pada uji contract).

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi.
- [ ] Coverage `services/dashboard/` ≥ 90%.
- [ ] PR fixture test di repo Dashboard disetujui tim Dashboard.
- [ ] MCP_SPEC §5 diperbarui bila ada catatan implementasi.
