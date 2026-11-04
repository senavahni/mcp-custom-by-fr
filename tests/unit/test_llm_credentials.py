"""M3_TASK_009 (unit): enkripsi key Gateway, validasi MCP_SECRET_KEY, pemilihan sumber key."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mcp_marketing.core.config import ConfigError, load_settings
from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.services.llm.credentials import credential_name, gateway_key_prefix, key_info
from mcp_marketing.services.llm.gateway_admin import admin_base_url
from mcp_marketing.utils.secretbox import SecretBoxError, generate_key, open_sealed, seal

KEY = "lgw_ab12cd34_" + "x" * 43


def test_seal_roundtrip_and_binding() -> None:
    secret = generate_key()
    token = seal(secret, KEY, "agent:analyst")
    assert KEY not in token
    assert open_sealed(secret, token, "agent:analyst") == KEY
    with pytest.raises(SecretBoxError):
        open_sealed(secret, token, "agent:researcher")  # aad lain (baris lain)
    with pytest.raises(SecretBoxError):
        open_sealed(generate_key(), token, "agent:analyst")  # kunci lain
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(SecretBoxError):
        open_sealed(secret, tampered, "agent:analyst")


def test_seal_is_randomized() -> None:
    secret = generate_key()
    assert seal(secret, KEY, "system") != seal(secret, KEY, "system")


def test_gateway_key_prefix() -> None:
    assert gateway_key_prefix(KEY) == "lgw_ab12cd34"
    for bad in ("sk-openai", "lgw_short_x", "mcpk_ab12cd34_" + "x" * 43):
        with pytest.raises(ValidationFailed):
            gateway_key_prefix(bad)


def test_secret_key_validation() -> None:
    assert load_settings(mcp_secret_key=generate_key()).mcp_secret_key is not None
    assert load_settings().mcp_secret_key is None
    with pytest.raises(ConfigError, match="MCP_SECRET_KEY"):
        load_settings(mcp_secret_key="bukan-base64-32-byte")
    pepper = load_settings().mcp_key_pepper.get_secret_value()
    with pytest.raises(ConfigError, match="MCP_SECRET_KEY"):
        load_settings(mcp_secret_key=pepper)


def cred(status: str, prefix: str = "lgw_00000000") -> SimpleNamespace:
    return SimpleNamespace(status=status, key_prefix=prefix)


def test_key_info_resolution_order() -> None:
    creds = {
        credential_name("analyst"): cred("active", "lgw_analyst1"),
        credential_name("researcher"): cred("invalid", "lgw_research"),
        "system": cred("active", "lgw_system01"),
    }
    assert key_info(creds, "analyst", enabled=True)[0] == "agent"  # type: ignore[arg-type]
    source, shown = key_info(creds, "researcher", enabled=True)  # type: ignore[arg-type]
    assert (
        source == "system" and shown is not None and shown.status == "invalid"
    )  # tampilkan masalah
    assert key_info(creds, "strategist", enabled=True)[0] == "system"  # type: ignore[arg-type]
    assert key_info({}, "analyst", enabled=True)[0] == "env"
    assert key_info(creds, "analyst", enabled=False)[0] == "env"  # type: ignore[arg-type]


def test_admin_base_url() -> None:
    assert admin_base_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000"
    assert admin_base_url("http://gw.test/v1/") == "http://gw.test"
    assert admin_base_url("http://gw.test") == "http://gw.test"
