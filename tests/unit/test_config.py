from pathlib import Path

import pytest

from mcp_marketing.core.config import PROJECT_ROOT, ConfigError, get_settings, load_settings


def test_defaults_load_in_test_env() -> None:
    s = get_settings()
    assert s.app_env == "test"
    assert s.app_port == 4100
    assert s.dashboard.contract_version == "1.0"
    assert s.default_toolset_list == ["toolbox", "data", "job"]


def test_short_secret_rejected_with_variable_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_WEBHOOK_SECRET", "short")
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert "MCP_WEBHOOK_SECRET" in str(exc.value)
    assert "short" not in str(exc.value).replace("minimal", "")


def test_identical_dashboard_secrets_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    same = "x" * 40
    monkeypatch.setenv("MCP_SERVER_API_KEY", same)
    monkeypatch.setenv("MCP_SERVICE_TOKEN", same)
    with pytest.raises(ConfigError, match="harus berbeda"):
        load_settings()


def test_change_me_rejected_in_prod_names_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:CHANGE_ME@h/db")
    with pytest.raises(ConfigError) as exc:
        load_settings()
    msg = str(exc.value)
    assert "DATABASE_URL" in msg
    assert "u:CHANGE_ME@h" not in msg


def test_change_me_allowed_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:CHANGE_ME@h/db")
    assert load_settings().app_env == "dev"


def test_prod_with_real_values_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    for name in ("DATABASE_URL", "MIGRATION_DATABASE_URL"):
        monkeypatch.setenv(name, "postgresql+asyncpg://u:pw@h/db")
    monkeypatch.setenv("LLM_GATEWAY_API_KEY", "lgw_real")
    assert load_settings().is_prod


@pytest.mark.parametrize("version", ["2.0", "1", "v1.0", "1.0.1"])
def test_contract_version_format(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setenv("CONTRACT_VERSION", version)
    with pytest.raises(ConfigError, match="CONTRACT_VERSION"):
        load_settings()


def test_run_timeout_must_be_below_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUN_TIMEOUT_MINUTES", "120")
    with pytest.raises(ConfigError, match="AGENT_RUN_TIMEOUT_MINUTES"):
        load_settings()


def test_invalid_int_names_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "banyak")
    with pytest.raises(ConfigError, match="DB_POOL_SIZE"):
        load_settings()


def test_paths_normalized_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHROMEDRIVER_PATH", "./bin/chromedriver.exe")
    s = load_settings()
    assert s.browser.chromedriver_path.is_absolute()
    assert s.browser.chromedriver_path == (PROJECT_ROOT / "bin" / "chromedriver.exe").resolve()
    assert s.log_dir == (PROJECT_ROOT / "logs").resolve()
    assert isinstance(s.nlp.sentiment_model_dir, Path)


def test_empty_optional_tokens_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_BEARER_TOKEN", "")
    assert load_settings().social.x_bearer_token is None


def test_get_settings_cached() -> None:
    assert get_settings() is get_settings()
