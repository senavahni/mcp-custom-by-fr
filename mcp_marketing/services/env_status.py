"""Status variabel `.env` untuk Admin Console: hanya NAMA + keadaan, nilai secret tidak keluar.

Nilai non-secret (URL, port) boleh ditampilkan agar operator bisa mencocokkan dengan layanan lain.
"""

from __future__ import annotations

import base64
from typing import Any, Literal

from pydantic import SecretStr

from mcp_marketing.core.config import MIN_SECRET_LEN, PLACEHOLDER, SECRET_KEY_BYTES, Settings
from mcp_marketing.services.llm.credentials import GATEWAY_KEY_RE

EnvState = Literal["ok", "missing", "placeholder", "weak", "invalid"]

CORE = "Inti & keamanan"
DASHBOARD = "Koneksi Dashboard (harus sama dengan .env Dashboard)"
GATEWAY = "LLM Gateway"
SOURCES = "Sumber data (opsional)"


def secret_state(raw: str | None, *, min_len: int = 0) -> EnvState:
    if not raw:
        return "missing"
    if PLACEHOLDER in raw:
        return "placeholder"
    if min_len and len(raw) < min_len:
        return "weak"
    return "ok"


def _raw(value: SecretStr | str | None) -> str | None:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def secret_key_state(value: SecretStr | None) -> EnvState:
    raw = _raw(value)
    if not raw:
        return "missing"
    try:
        ok = len(base64.b64decode(raw, validate=True)) == SECRET_KEY_BYTES
    except ValueError:
        ok = False
    return "ok" if ok else "invalid"


def gateway_key_state(value: SecretStr) -> EnvState:
    state = secret_state(_raw(value))
    if state != "ok":
        return state
    return "ok" if GATEWAY_KEY_RE.match(_raw(value) or "") else "invalid"


def _var(
    name: str,
    group: str,
    help_text: str,
    *,
    state: EnvState = "ok",
    required: bool = True,
    value: Any = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "group": group,
        "required": required,
        "secret": value is None,
        "state": state,
        "value": value,
        "help": help_text,
    }


def env_vars(settings: Settings) -> list[dict[str, Any]]:
    d, llm, social = settings.dashboard, settings.llm, settings.social
    return [
        _var("APP_ENV", CORE, "dev / staging / prod.", value=settings.app_env),
        _var(
            "PUBLIC_BASE_URL",
            CORE,
            "URL server ini (link unduhan export).",
            value=settings.public_base_url,
        ),
        _var(
            "MCP_KEY_PEPPER",
            CORE,
            "Pepper hash key klien mcpk_. JANGAN diganti: semua key klien jadi tidak valid.",
            state=secret_state(_raw(settings.mcp_key_pepper), min_len=MIN_SECRET_LEN),
        ),
        _var(
            "MCP_SECRET_KEY",
            CORE,
            "Kunci enkripsi key LLM per agent. Kosong = semua agent memakai LLM_GATEWAY_API_KEY. "
            "JANGAN diganti setelah dipakai: key tersimpan tidak bisa dibuka lagi.",
            state=secret_key_state(settings.mcp_secret_key),
            required=False,
        ),
        _var(
            "DATABASE_URL",
            CORE,
            "Koneksi PostgreSQL (role aplikasi, berisi password).",
            state=secret_state(settings.db.database_url),
        ),
        _var(
            "MIGRATION_DATABASE_URL",
            CORE,
            "Koneksi role migrasi (hanya untuk alembic).",
            state=secret_state(settings.db.migration_database_url),
            required=False,
        ),
        _var("DASHBOARD_API_URL", DASHBOARD, "Alamat API Dashboard.", value=d.dashboard_api_url),
        _var(
            "MCP_SERVER_API_KEY",
            DASHBOARD,
            "Dashboard -> MCP. Nilainya harus SAMA di .env backend Dashboard.",
            state=secret_state(_raw(d.mcp_server_api_key), min_len=MIN_SECRET_LEN),
        ),
        _var(
            "MCP_WEBHOOK_SECRET",
            DASHBOARD,
            "Tanda tangan webhook MCP -> Dashboard. Harus SAMA di .env Dashboard.",
            state=secret_state(_raw(d.mcp_webhook_secret), min_len=MIN_SECRET_LEN),
        ),
        _var(
            "MCP_SERVICE_TOKEN",
            DASHBOARD,
            "MCP -> Agent/Service API Dashboard. Harus SAMA di .env Dashboard.",
            state=secret_state(_raw(d.mcp_service_token), min_len=MIN_SECRET_LEN),
        ),
        _var(
            "LLM_GATEWAY_URL",
            GATEWAY,
            "Alamat API Gateway (diakhiri /v1).",
            value=llm.llm_gateway_url,
        ),
        _var(
            "LLM_GATEWAY_API_KEY",
            GATEWAY,
            "Key cadangan bila agent tidak punya key sendiri. Boleh dibiarkan CHANGE_ME bila "
            "key per agent sudah di-provision.",
            state=gateway_key_state(llm.llm_gateway_api_key),
            required=False,
        ),
        _var(
            "X_BEARER_TOKEN",
            SOURCES,
            "Token X API resmi (berbayar per post).",
            state=secret_state(_raw(social.x_bearer_token)),
            required=False,
        ),
        _var(
            "THREADS_ACCESS_TOKEN",
            SOURCES,
            "Token Threads API (Meta).",
            state=secret_state(_raw(social.threads_access_token)),
            required=False,
        ),
        _var(
            "THREADS_USER_ID",
            SOURCES,
            "User ID Threads pemilik token.",
            state=secret_state(social.threads_user_id),
            required=False,
        ),
        _var(
            "TRENDS_PROVIDER",
            SOURCES,
            "unofficial / browser / official.",
            value=settings.trends.trends_provider,
        ),
    ]
