# TASK-01 — Setup Environment & Struktur Repository

| Item | Nilai |
|---|---|
| Milestone | M0 Fondasi |
| Prioritas | P0 |
| Estimasi | 1 hari |
| Dependensi | — |
| Referensi | README D1, D11 · ARCHITECTURE §2, §3, §9 |

## Tujuan
Menyiapkan repository `mcp-marketing` yang bisa di-install dan dijalankan di Windows (tanpa admin) dengan Python yang **sama dengan LLM Gateway (3.11)**, beserta tooling kualitas kode yang identik dengan Gateway agar tim bisa berpindah proyek tanpa adaptasi.

## Ruang Lingkup
- **Termasuk:** conda env, `pyproject.toml`, file requirements terpisah, struktur folder, skrip `.bat`, ruff/mypy/pytest, `.gitignore`, `.env.example` awal, secret scan.
- **Tidak termasuk:** kode bisnis (mulai TASK-02).

## Spesifikasi

### Versi Python
- `requires-python = ">=3.11,<3.13"`; target `py311` (sama dengan `llm-genai/pyproject.toml`). Conda env: `mcp-marketing` (`python=3.11`).

### File requirements
| File | Isi |
|---|---|
| `requirements.txt` | inti: `mcp`, `uvicorn[standard]`, `starlette`, `pydantic>=2.7`, `pydantic-settings>=2.6`, `sqlalchemy[asyncio]>=2.0`, `asyncpg>=0.29`, `alembic>=1.13`, `pgvector`, `httpx>=0.27`, `tenacity>=8.5`, `openai>=1.40`, `tiktoken`, `structlog>=24.1`, `orjson`, `prometheus-client`, `pyyaml`, `python-dotenv`, `pandas`, `numpy`, `scipy`, `croniter`; dev: `pytest>=8`, `pytest-asyncio>=0.24`, `respx`, `ruff>=0.6`, `mypy>=1.10`, `freezegun` |
| `requirements-browser.txt` | `selenium>=4.20`, `trafilatura`, `selectolax`, `markdownify`, `tldextract`, `protego` (robots.txt) |
| `requirements-ml.txt` | `onnxruntime`, `tokenizers`, `huggingface-hub` (unduh model sekali), `langdetect` atau `lingua-language-detector` |
| `requirements.lock.txt` | hasil `pip freeze` setelah instalasi bersih (versi persis) |

> Versi `mcp` dipin ke rilis stable terbaru saat task dikerjakan; catat di `CHANGELOG.md` hasil spike: `FastMCP` + `streamable_http_app()` + `structuredContent` (output schema) berjalan di Python 3.11.

### Struktur folder
Sesuai [ARCHITECTURE §3](../ARCHITECTURE.md#3-struktur-repository). Semua package memiliki `__init__.py`; folder `browser_profiles/`, `models/`, `logs/`, `storage/`, `bin/` ada `.gitkeep` dan di-ignore isinya.

### Skrip Windows (`scripts/`)
| Skrip | Fungsi |
|---|---|
| `env.bat` | Set `CONDA_ENV`, memuat `.env` baris per baris (pola sama dengan Gateway; tanpa komentar di akhir baris) |
| `conda-run.bat` | Menjalankan perintah di env conda |
| `check.bat` | `ruff check .` + `ruff format --check .` + `mypy mcp_marketing` + `pytest -m "not integration and not browser and not live"` |
| `test.bat` | pytest dengan argumen diteruskan |
| `migrate.bat` | `alembic upgrade head` (TASK-03) |
| `start-server.bat`, `start-worker.bat`, `start-all.bat`, `stop-all.bat` | Placeholder (diisi TASK-04/07/27) |
| `secret_scan.py` | Port dari Gateway: tolak commit yang berisi pola key (`lgw_`, `mcpk_`, `sk-`, `AKIA`, token X/Threads) |

### Konfigurasi tooling (`pyproject.toml`)
- ruff: `line-length = 100`, `select = ["E","F","I","UP","B"]`, `ignore = ["B008"]`, `known-first-party = ["mcp_marketing"]`, exclude `migrations/versions/*` dari format.
- mypy: `python_version = "3.11"`, `ignore_missing_imports = true`, override `disallow_untyped_defs = true` untuk `mcp_marketing.services.*`, `mcp_marketing.tools.*`, `mcp_marketing.server.*`.
- pytest: `asyncio_mode = "auto"`, markers: `integration` (PostgreSQL), `browser` (Chrome nyata), `live` (API eksternal/Gateway nyata), `contract` (Dashboard nyata), `eval` (eval token).

## Checklist Implementasi
- [ ] Buat repo + `README.md` singkat (link ke `docs/`).
- [ ] `pyproject.toml`, 3 file requirements, lock file.
- [ ] Struktur folder & `__init__.py`.
- [ ] `.gitignore` (`.env`, `browser_profiles/*`, `models/*`, `logs/*`, `storage/*`, `bin/*.exe`, `__pycache__`, `.mypy_cache`).
- [ ] `.env.example` kerangka (diisi bertahap per task).
- [ ] Skrip `.bat` + `secret_scan.py`.
- [ ] `tests/test_smoke.py` (import package, versi Python 3.11).
- [ ] Spike MCP SDK: server `FastMCP` minimal dengan 1 tool yang mengembalikan `structuredContent`, dipanggil dengan klien SDK resmi; hasil dicatat.
- [ ] `CHANGELOG.md` entri `0.1.0-dev`.

## Pengujian
- `scripts\check.bat` lulus di mesin bersih.
- `python -c "import sys; assert sys.version_info[:2]==(3,11)"` di env.

## Acceptance Criteria
1. Developer baru dapat clone → `conda create` → `pip install -r …` → `scripts\check.bat` hijau dalam < 15 menit (diuji 1 orang di luar penulis).
2. Versi Python env = 3.11.x, sama dengan env `llm-genai`.
3. Spike MCP SDK terdokumentasi (versi, contoh kode, keterbatasan).
4. `secret_scan.py` mendeteksi contoh key palsu di file uji.

## Definition of Done
- [ ] Semua checklist tercentang & Acceptance Criteria terpenuhi.
- [ ] `check.bat` lulus; tidak ada warning ruff/mypy.
- [ ] Lock file ter-commit; tidak ada secret di repo.
- [ ] README menjelaskan instalasi; reviewer menyetujui.
