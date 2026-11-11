"""Konfigurasi tervalidasi (ARCHITECTURE §8), dikelompokkan per domain.

Semua nilai dibaca dari environment (nama variabel = nama field dalam huruf besar). File `.env`
dimuat sekali oleh `get_settings()` tanpa menimpa variabel yang sudah ada di environment.
"""

from __future__ import annotations

import base64
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLACEHOLDER = "CHANGE_ME"
MIN_SECRET_LEN = 32
SECRET_KEY_BYTES = 32
DASHBOARD_RUN_TIMEOUT_MINUTES = 120
CONTRACT_VERSION_RE = re.compile(r"^1\.\d+$")


class ConfigError(RuntimeError):
    """Konfigurasi wajib salah/kurang. Pesan menyebut nama variabel, tidak pernah nilainya."""


def _abs_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


class _Group(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)


class DbSettings(_Group):
    database_url: str = "postgresql+asyncpg://mcp_app:CHANGE_ME@127.0.0.1:5432/marketing_dashboard"
    db_schema: str = "mcp"
    db_pool_size: int = Field(default=10, ge=1, le=100)
    migration_database_url: str = (
        "postgresql+asyncpg://mcp_owner:CHANGE_ME@127.0.0.1:5432/marketing_dashboard"
    )


class LlmSettings(_Group):
    llm_gateway_url: str = "http://127.0.0.1:8000/v1"
    llm_gateway_api_key: SecretStr = SecretStr("lgw_CHANGE_ME")
    # admin key Gateway untuk rotasi key dari Dashboard (intel_llm_key_rotate); kosong = rotasi
    # lewat tool nonaktif. Tidak pernah ditulis ke DB maupun log.
    llm_gateway_admin_key: SecretStr | None = None
    llm_route_fast: str = "fast"
    llm_route_smart: str = "smart"
    llm_route_embed: str = "embed-default"
    llm_timeout_s: float = Field(default=120, gt=0)
    llm_cache_ttl_s: int = Field(default=86_400, ge=0)

    @field_validator("llm_gateway_admin_key", mode="before")
    @classmethod
    def _empty_none(cls, v: Any) -> Any:
        return None if v == "" else v


class DashboardSettings(_Group):
    dashboard_api_url: str = "http://localhost:3000/api/v1"
    mcp_server_api_key: SecretStr = SecretStr("CHANGE_ME_server_api_key_min_32_characters")
    mcp_webhook_secret: SecretStr = SecretStr("CHANGE_ME_webhook_secret_min_32_characters")
    mcp_service_token: SecretStr = SecretStr("CHANGE_ME_service_token_min_32_characters")
    contract_version: str = "1.0"
    webhook_max_retry_minutes: int = Field(default=30, ge=1)
    agent_run_timeout_minutes: int = Field(default=110, ge=1)

    @field_validator("contract_version")
    @classmethod
    def _contract(cls, v: str) -> str:
        if not CONTRACT_VERSION_RE.match(v):
            raise ValueError("CONTRACT_VERSION harus berformat 1.x")
        return v

    @field_validator("agent_run_timeout_minutes")
    @classmethod
    def _timeout(cls, v: int) -> int:
        if v >= DASHBOARD_RUN_TIMEOUT_MINUTES:
            raise ValueError(
                f"AGENT_RUN_TIMEOUT_MINUTES harus < {DASHBOARD_RUN_TIMEOUT_MINUTES} "
                "(timeout Dashboard)"
            )
        return v


class AgentSettings(_Group):
    agent_default_run_token_budget: int = Field(default=120_000, ge=1_000)
    agent_max_steps: int = Field(default=25, ge=1, le=100)
    agent_history_compact_at_tokens: int = Field(default=12_000, ge=1_000)


class TokenSettings(_Group):
    tool_output_soft_tokens: int = Field(default=1_500, ge=100)
    tool_output_hard_tokens: int = Field(default=4_000, ge=200)
    inline_items_max: int = Field(default=20, ge=1, le=100)
    result_ttl_days: int = Field(default=30, ge=1)
    tool_llm_tokens_default: int = Field(default=20_000, ge=100)
    tool_llm_tokens_deep: int = Field(default=60_000, ge=100)


class WorkerSettings(_Group):
    worker_concurrency: int = Field(default=2, ge=1, le=32)
    worker_poll_interval_ms: int = Field(default=500, ge=50)
    job_default_timeout_s: int = Field(default=1_800, ge=1)
    audit_retention_days: int = Field(default=180, ge=1)
    raw_retention_days: int = Field(default=14, ge=1)
    social_retention_days: int = Field(default=180, ge=1)


class BrowserSettings(_Group):
    chrome_binary: Path = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    chromedriver_path: Path = Path("./bin/chromedriver.exe")
    browser_profiles_dir: Path = Path("./browser_profiles")
    browser_headless: bool = False
    browser_page_timeout_s: int = Field(default=45, ge=1)
    scrape_respect_robots: bool = True
    scrape_per_domain_rpm: int = Field(default=20, ge=1)
    scrape_allow_private_network: bool = False

    # keberadaan file dicek lazy di modul pemakai (server tetap start tanpa browser)
    @field_validator("chromedriver_path", "browser_profiles_dir", "chrome_binary")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return _abs_path(v)


class DiscoverySettings(_Group):
    """Rem volume Google Search/Maps via Chrome (MCP-V1-06, V1-K4). Angka awal; kalibrasi spike."""

    discovery_profile: str = "riset-google"
    discovery_search_per_hour: int = Field(default=40, ge=1, le=200)
    discovery_search_per_day: int = Field(default=300, ge=1)
    discovery_maps_per_hour: int = Field(default=30, ge=1, le=200)
    discovery_maps_per_day: int = Field(default=200, ge=1)
    # jeda acak minimal-maksimal antar navigasi Google (detik)
    discovery_pause_min_s: float = Field(default=20.0, ge=0)
    discovery_pause_max_s: float = Field(default=60.0, ge=0)
    discovery_cache_ttl_days: int = Field(default=30, ge=0)
    # domain tambahan yang dibuang dari hasil Search (dipisah koma), di luar daftar bawaan
    discovery_blocked_domains: str = ""

    @model_validator(mode="after")
    def _pause_range(self) -> DiscoverySettings:
        if self.discovery_pause_max_s < self.discovery_pause_min_s:
            raise ValueError("DISCOVERY_PAUSE_MAX_S harus >= DISCOVERY_PAUSE_MIN_S")
        return self


class UploadSettings(_Group):
    """`POST /uploads` & ingest PDF (MCP-V1-11, V1-K9)."""

    upload_pdf_max_mb: int = Field(default=100, ge=1, le=500)
    upload_sheet_max_mb: int = Field(default=20, ge=1, le=100)
    pdf_max_pages: int = Field(default=2_000, ge=1)
    # batas waktu ekstraksi teks seluruh PDF (detik); lewat -> asset failed
    pdf_extract_timeout_s: int = Field(default=1_800, ge=10)


class SocialSettings(_Group):
    x_bearer_token: SecretStr | None = None
    x_max_reads_per_day: int = Field(default=2_000, ge=0)
    # tarif X API pay-per-use (USD per post yang dikembalikan), mudah diubah tanpa deploy
    x_price_per_read_usd: float = Field(default=0.005, ge=0)
    x_api_base_url: str = "https://api.x.com/2"
    threads_access_token: SecretStr | None = None
    threads_user_id: str | None = None
    threads_api_base_url: str = "https://graph.threads.net/v1.0"

    @field_validator("x_bearer_token", "threads_access_token", mode="before")
    @classmethod
    def _empty_none(cls, v: Any) -> Any:
        return None if v == "" else v


class TrendsSettings(_Group):
    trends_provider: Literal["unofficial", "browser", "official"] = "unofficial"
    trends_default_geo: str = "ID"
    trends_cache_ttl_s: int = Field(default=86_400, ge=0)
    # jeda minimal antar permintaan ke Google Trends (detik)
    trends_min_interval_s: float = Field(default=5.0, ge=0)
    trends_base_url: str = "https://trends.google.com"
    # diisi bila akses Google Trends API resmi (alpha) disetujui
    trends_official_api_key: SecretStr | None = None

    @field_validator("trends_official_api_key", mode="before")
    @classmethod
    def _empty_none(cls, v: Any) -> Any:
        return None if v == "" else v


class NlpSettings(_Group):
    sentiment_model_dir: Path = Path("./models/sentiment-id")
    sentiment_llm_threshold: float = Field(default=0.60, ge=0, le=1)
    embed_provider: Literal["gateway", "local"] = "gateway"
    embed_dim: int = 1536
    local_embed_model_dir: Path = Path("./models/embed-local")
    # v1 (MCP-V1-01): pgvector wajib; true = server/worker menolak start dalam mode cadangan real[]
    kb_require_pgvector: bool = True

    @field_validator("sentiment_model_dir", "local_embed_model_dir")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return _abs_path(v)


class Settings(_Group):
    app_env: Literal["dev", "test", "staging", "prod"] = "dev"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=4100, ge=1, le=65535)
    log_level: str = "INFO"
    log_dir: Path = Path("./logs")
    timezone: str = "Asia/Jakarta"
    mcp_key_pepper: SecretStr = SecretStr("CHANGE_ME_key_pepper_min_32_characters_xx")
    # kunci AES-256-GCM (32 byte, base64) untuk key LLM Gateway per agent; kosong = semua agent
    # memakai LLM_GATEWAY_API_KEY (M3_TASK_009)
    mcp_secret_key: SecretStr | None = None
    default_toolsets: str = "toolbox,data,job"
    public_base_url: str = "http://127.0.0.1:4100"
    storage_dir: Path = Path("./storage")
    enable_dev_tools: bool = False

    db: DbSettings = Field(default_factory=DbSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    token: TokenSettings = Field(default_factory=TokenSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    social: SocialSettings = Field(default_factory=SocialSettings)
    trends: TrendsSettings = Field(default_factory=TrendsSettings)
    nlp: NlpSettings = Field(default_factory=NlpSettings)

    @field_validator("log_dir", "storage_dir")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return _abs_path(v)

    @property
    def default_toolset_list(self) -> list[str]:
        return [t.strip() for t in self.default_toolsets.split(",") if t.strip()]

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @model_validator(mode="after")
    def _cross_checks(self) -> Settings:
        secrets = {
            "MCP_KEY_PEPPER": self.mcp_key_pepper.get_secret_value(),
            "MCP_SERVER_API_KEY": self.dashboard.mcp_server_api_key.get_secret_value(),
            "MCP_WEBHOOK_SECRET": self.dashboard.mcp_webhook_secret.get_secret_value(),
            "MCP_SERVICE_TOKEN": self.dashboard.mcp_service_token.get_secret_value(),
        }
        short = [name for name, value in secrets.items() if len(value) < MIN_SECRET_LEN]
        if short:
            raise ValueError(f"minimal {MIN_SECRET_LEN} karakter: {', '.join(short)}")
        dashboard_secrets = [
            secrets["MCP_SERVER_API_KEY"],
            secrets["MCP_WEBHOOK_SECRET"],
            secrets["MCP_SERVICE_TOKEN"],
        ]
        if len(set(dashboard_secrets)) != len(dashboard_secrets):
            raise ValueError(
                "MCP_SERVER_API_KEY, MCP_WEBHOOK_SECRET, MCP_SERVICE_TOKEN "
                "harus berbeda satu sama lain"
            )
        if secrets["MCP_KEY_PEPPER"] in dashboard_secrets:
            raise ValueError("MCP_KEY_PEPPER harus berbeda dari ketiga secret Dashboard")
        if self.mcp_secret_key is not None:
            raw = self.mcp_secret_key.get_secret_value()
            if raw in secrets.values():
                raise ValueError(
                    "MCP_SECRET_KEY harus berbeda dari MCP_KEY_PEPPER & secret Dashboard"
                )
            try:
                ok = len(base64.b64decode(raw, validate=True)) == SECRET_KEY_BYTES
            except ValueError:
                ok = False
            if not ok:
                raise ValueError(f"MCP_SECRET_KEY harus {SECRET_KEY_BYTES} byte dalam base64")
        if self.app_env in ("prod", "staging"):
            placeholders = sorted(_placeholder_vars(self))
            if placeholders:
                raise ValueError(
                    f"APP_ENV={self.app_env} tetapi masih bernilai {PLACEHOLDER}: "
                    f"{', '.join(placeholders)}"
                )
        return self


def _placeholder_vars(model: BaseSettings) -> list[str]:
    found: list[str] = []
    for name in type(model).model_fields:
        value = getattr(model, name)
        if isinstance(value, BaseSettings):
            found += _placeholder_vars(value)
            continue
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(raw, str) and PLACEHOLDER in raw:
            found.append(name.upper())
    return found


def load_settings(**overrides: Any) -> Settings:
    """Bangun Settings; error validasi diringkas menjadi ConfigError yang menyebut nama variabel."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            loc = [str(p) for p in err["loc"] if isinstance(p, str)]
            # loc grup ("db", "db_pool_size") -> nama env = segmen terakhir
            name = loc[-1].upper() if loc else "SETTINGS"
            message = err["msg"].removeprefix("Value error, ")
            problems.append(f"{name}: {message}" if loc else message)
        raise ConfigError("Konfigurasi tidak valid -> " + "; ".join(problems)) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # test men-set environment sendiri; .env developer tidak boleh ikut terbaca
    if os.environ.get("APP_ENV") != "test":
        load_dotenv(PROJECT_ROOT / ".env", override=False)
    return load_settings()
