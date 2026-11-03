# Laporan Kontrak Dashboard (contract 1.0) — 2026-11-03

## Sudah diverifikasi

| Pemeriksaan | Cara | Hasil |
|---|---|---|
| Payload webhook & output tool vs Zod Dashboard | `tests/contract/validate_with_zod.cjs` memuat `mcp.schema.js` + `block.schema.js` dari build `dist/` Dashboard | 28/28 fixture sesuai (8 event valid lolos, 15 invalid ditolak, 5 output tool) |
| Validator Python setara Zod | `tests/unit/test_contract.py::test_fixture_python_validator_agrees` | 28/28 |
| Signature webhook | vektor uji dihitung `signMcpPayload()` Dashboard | identik |
| Transport MCP | klien SDK resmi mode `legacy` (handshake lama, dipakai SDK TS Dashboard) & `auto` | `initialize`, `tools/list`, `tools/call`, `structuredContent`, `isError` OK |
| Dashboard key melihat tool | `tools/list` dengan `MCP_SERVER_API_KEY` | tepat 5 tool `pm_*` |
| Siklus run (Dashboard palsu) | `tests/integration/test_agent.py`, `test_outbox.py` | dispatch idempoten, urutan event, cancel ≤ 5 s tanpa webhook lanjutan, 409 → discard, resume setelah restart, timeout run |

Fixture dibangkitkan ulang dengan `python scripts/export_contract_fixtures.py`.

## Belum diverifikasi (butuh sistem nyata)

| Pemeriksaan | Langkah |
|---|---|
| E2E UC-01/UC-03/UC-04 dengan Dashboard & Gateway lokal | bootstrap DB + `.env` (README), jalankan Gateway, Dashboard (`MCP_SERVER_URL=http://localhost:4100/mcp`), `scripts\start-all.bat`, assign task ke Agent Strategist |
| Contract test di repo Dashboard | salin `documentation/dashboard-pr/mcp-fixtures.spec.ts` + folder fixture ke `backend/test/contract/` (PR ke tim Dashboard) |
| Token run UC-01 ≤ 40.000 | lihat `mcp.runs.tokens_used` setelah UC-01 nyata |
