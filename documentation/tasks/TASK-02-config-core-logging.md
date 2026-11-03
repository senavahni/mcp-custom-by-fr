# TASK-02 — Config, Logging, Error Model & Request Context

| Item | Nilai |
|---|---|
| Milestone | M0 Fondasi |
| Prioritas | P0 |
| Estimasi | 2 hari |
| Dependensi | TASK-01 |
| Referensi | ARCHITECTURE §6, §8 · PRD NFR-07, NFR-10, NFR-13 |

## Tujuan
Fondasi lintas modul: konfigurasi tervalidasi, log JSON terstruktur yang konsisten dengan Gateway, model error tunggal untuk semua tool, dan konteks request (`request_id`, `client_id`, `run_id`, `job_id`) yang mengalir otomatis ke log dan panggilan keluar.

## Spesifikasi

### `mcp_marketing/core/config.py`
- `Settings(BaseSettings)` dengan **semua** variabel di ARCHITECTURE §8, dikelompokkan sebagai sub-model (`db`, `llm`, `dashboard`, `agent`, `token`, `worker`, `browser`, `social`, `trends`, `nlp`).
- Validasi:
  - `MCP_KEY_PEPPER`, `MCP_SERVER_API_KEY`, `MCP_WEBHOOK_SECRET`, `MCP_SERVICE_TOKEN` ≥ 32 karakter; ketiga secret Dashboard **harus berbeda** satu sama lain.
  - `APP_ENV=prod` + nilai mengandung `CHANGE_ME` → gagal start dengan pesan jelas (nama variabel, bukan nilainya).
  - `CONTRACT_VERSION` cocok `^1\.\d+$`.
  - `AGENT_RUN_TIMEOUT_MINUTES` < 120 (timeout Dashboard).
  - Path (`CHROMEDRIVER_PATH`, `BROWSER_PROFILES_DIR`, `SENTIMENT_MODEL_DIR`) dinormalisasi ke absolut; keberadaan file dicek **lazy** di modul pemakai (agar server tetap bisa start tanpa browser).
- `get_settings()` di-cache (`lru_cache`); override mudah di test.

### `core/logging.py`
- `structlog` + `orjson`, output JSON ke stdout dan file harian `LOG_DIR/mcp-YYYY-MM-DD.log` (rotasi 14 file).
- Field wajib: `ts`, `level`, `event`, `request_id`, `client_id`, `tool`, `run_id`, `job_id`, `duration_ms` (bila ada).
- **Redaksi**: prosesor yang menyamarkan key `authorization`, `token`, `secret`, `password`, `cookie`, `api_key`, `access_token`, dan pola `lgw_…`, `mcpk_…`, `Bearer …`.

### `core/context.py`
- `contextvars`: `request_id`, `client_id`, `client_name`, `run_id`, `job_id`, `tool`.
- Helper `bind_context(**kw)` (juga bind ke structlog) dan `outgoing_headers()` → `{"X-Request-Id": …}` untuk Gateway/Dashboard.

### `core/errors.py`
```python
class ToolError(Exception):
    code: ErrorCode; message: str; retryable: bool; details: dict | None
ErrorCode = Literal["VALIDATION_ERROR","NOT_FOUND","FORBIDDEN","RATE_LIMITED","BUDGET_EXCEEDED",
  "UPSTREAM_ERROR","UPSTREAM_TIMEOUT","JOB_FAILED","CONFLICT","UNSUPPORTED_CONTRACT_VERSION",
  "RUN_NOT_ACTIVE","PROFILE_BUSY","LOGIN_REQUIRED","UNKNOWN_AGENT","HIERARCHY_DEPTH_EXCEEDED","INTERNAL"]
```
- Subclass kecil (`NotFound`, `Forbidden`, `UpstreamTimeout`, …) dengan `retryable` default yang benar.
- `to_structured()` → `{code, message, retryable, details}`; pesan **tidak** pernah berisi stack trace atau secret.
- Pemetaan exception umum: `pydantic.ValidationError` → `VALIDATION_ERROR` (daftar field ringkas, maks 5), `httpx.TimeoutException` → `UPSTREAM_TIMEOUT`, `asyncio.TimeoutError` → `UPSTREAM_TIMEOUT`, lainnya → `INTERNAL` (detail hanya di log).

### `utils/`
- `ids.py`: `new_handle()` `h_` + 10 char base32 crockford lowercase; `new_job_id()` `j_…`; `new_report_id()` `r_…`; `new_mcp_run_id()` `mr_…`.
- `time.py`: `parse_period(since, until, tz)` → `(datetime_utc, datetime_utc)` menerima `YYYY-MM-DD` (since = awal hari, until = akhir hari inklusif) & ISO-8601; validasi `until > since`; `now()` yang bisa di-freeze.
- `tokens.py`: `count_tokens(text|obj)` via `tiktoken` (`o200k_base`), cepat untuk objek JSON (`orjson.dumps`).
- `text.py`: `truncate(text, max_chars)` aman unicode, `strip_control_chars`, `normalize_ws`.
- `crypto.py`: `hmac_sha256_hex`, `constant_time_eq`, `generate_client_key()`.

## Checklist Implementasi
- [ ] `Settings` + sub-model + validator; `.env.example` lengkap dengan komentar baris sendiri.
- [ ] Logging + redaksi + rotasi.
- [ ] Context vars + header keluar.
- [ ] `ToolError` & pemetaan exception.
- [ ] Utils `ids`, `time`, `tokens`, `text`, `crypto`.
- [ ] Unit test semua utils & validator.

## Pengujian
- `test_config.py`: secret pendek → error; secret sama → error; `CHANGE_ME` di prod → error; dev boleh.
- `test_logging.py`: log berisi `Authorization: Bearer abc` → tersamarkan; field konteks muncul.
- `test_time.py`: `parse_period("2026-10-01","2026-10-30","Asia/Jakarta")` → `2026-09-30T17:00Z` s/d `2026-10-30T16:59:59.999999Z`; until ≤ since → error.
- `test_tokens.py`: estimasi deterministik untuk string tetap.

## Acceptance Criteria
1. Server gagal start dengan pesan yang menyebut nama variabel bila konfigurasi wajib salah.
2. Tidak ada secret yang muncul di log dalam test redaksi (10 pola).
3. Semua error yang keluar dari tool dapat diserialisasi ke `{code, message, retryable}`.
4. Coverage `core/` dan `utils/` ≥ 90%.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi.
- [ ] `check.bat` lulus; mypy strict untuk `core/` & `utils/`.
- [ ] `.env.example` dan ARCHITECTURE §8 sinkron.
- [ ] Review disetujui.
