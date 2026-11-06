"""M3_TASK_009 P1 (unit): urutan rotasi key Gateway & tool `intel_llm_key_rotate` tanpa PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from mcp_marketing.core.errors import Conflict, NotFound, UnknownAgent, UpstreamError
from mcp_marketing.services.llm import gateway_admin
from mcp_marketing.services.llm.gateway_admin import GatewayAdmin, rotate_credential
from mcp_marketing.tools import intel_keys
from mcp_marketing.tools.intel_keys import LlmKeyRotateIn

OLD_ID = uuid.uuid4()
NEW_ID = uuid.uuid4()
NEW_KEY = "lgw_newkey12_" + "A" * 43
ROTATED_AT = datetime(2026, 11, 6, 1, 0, tzinfo=UTC)


class _Session:
    def __init__(self, current: object) -> None:
        self.current = current

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def scalar(self, _query: object) -> object:
        return self.current


def _db(current: object) -> SimpleNamespace:
    return SimpleNamespace(session=lambda: _Session(current))


class _Admin(GatewayAdmin):
    """GatewayAdmin dengan jejak panggilan; tidak ada HTTP sungguhan."""

    def __init__(self, *, revoke_error: Exception | None = None) -> None:
        super().__init__(http=None, base_url="http://gw.test", admin_key="lgw_admin")  # type: ignore[arg-type]
        self.events: list[tuple[str, str]] = []
        self.revoke_error = revoke_error

    async def ensure_project(self, slug: str) -> dict[str, Any]:
        return {"id": "project-1", "slug": slug}

    async def create_key(self, project_id: str, name: str, *, rpm: int | None) -> dict[str, Any]:
        self.events.append(("create", name))
        return {"id": str(NEW_ID), "key": NEW_KEY}

    async def revoke_key(self, key_id: str, reason: str) -> None:
        self.events.append(("revoke", key_id))
        if self.revoke_error is not None:
            raise self.revoke_error


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_save(_session: object, secret: str, **kwargs: Any) -> SimpleNamespace:
        calls.append({"secret": secret, **kwargs})
        return SimpleNamespace(key_prefix=NEW_KEY[:12], rotated_at=ROTATED_AT)

    monkeypatch.setattr(gateway_admin, "save_credential", fake_save)
    return calls


async def test_rotate_creates_saves_then_revokes_old(saved: list[dict[str, Any]]) -> None:
    admin = _Admin()
    result = await rotate_credential(
        _db(SimpleNamespace(gateway_key_id=OLD_ID)), "secret", admin, agent_key="analyst"
    )

    assert admin.events == [("create", "mcp-analyst"), ("revoke", str(OLD_ID))]
    assert (result.prefix, result.rotated_at, result.old_key_revoked) == (
        NEW_KEY[:12],
        ROTATED_AT,
        True,
    )
    assert saved[0]["agent_key"] == "analyst" and saved[0]["gateway_key_id"] == NEW_ID
    assert NEW_KEY not in repr(result)


async def test_rotate_unknown_credential_does_not_touch_gateway(
    saved: list[dict[str, Any]],
) -> None:
    admin = _Admin()
    with pytest.raises(NotFound):
        await rotate_credential(_db(None), "secret", admin, agent_key="analyst")
    assert admin.events == [] and saved == []


async def test_rotate_imported_key_without_gateway_id_is_not_revoked(
    saved: list[dict[str, Any]],
) -> None:
    admin = _Admin()
    result = await rotate_credential(
        _db(SimpleNamespace(gateway_key_id=None)), "secret", admin, agent_key=None
    )
    assert result.old_key_revoked is False and admin.events == [("create", "mcp-system")]


async def test_rotate_keeps_new_key_when_old_revoke_fails(saved: list[dict[str, Any]]) -> None:
    admin = _Admin(revoke_error=UpstreamError("gagal", details={"status": 500}))
    result = await rotate_credential(
        _db(SimpleNamespace(gateway_key_id=OLD_ID)), "secret", admin, agent_key="analyst"
    )
    assert result.old_key_revoked is False and len(saved) == 1


async def test_rotate_revokes_new_key_when_save_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(gateway_admin, "save_credential", boom)
    admin = _Admin()
    with pytest.raises(RuntimeError):
        await rotate_credential(
            _db(SimpleNamespace(gateway_key_id=OLD_ID)), "secret", admin, agent_key="analyst"
        )
    assert admin.events == [("create", "mcp-analyst"), ("revoke", str(NEW_ID))]


@pytest.mark.parametrize("value", ["a", "Bad Key", "x" * 65, "ab/cd", "../x", ""])
def test_input_rejects_invalid_agent_key(value: str) -> None:
    with pytest.raises(ValidationError):
        LlmKeyRotateIn(agent_key=value)


@pytest.mark.parametrize("value", ["analyst", "system", "agent-1", "a_b"])
def test_input_accepts_valid_agent_key(value: str) -> None:
    assert LlmKeyRotateIn(agent_key=value).agent_key == value


def _ctx(
    *,
    secret: str | None = "set",
    admin_key: str | None = "lgw_admin",
    known: str | None = "analyst",
) -> SimpleNamespace:
    session = _Session(known)
    invalidated: list[bool] = []
    credentials = SimpleNamespace(invalidate=lambda: invalidated.append(True))
    return SimpleNamespace(
        invalidated=invalidated,
        settings=SimpleNamespace(
            mcp_secret_key=SecretStr(secret) if secret else None,
            llm=SimpleNamespace(
                llm_gateway_url="http://gw.test/v1",
                llm_gateway_admin_key=SecretStr(admin_key) if admin_key else None,
            ),
        ),
        services=SimpleNamespace(
            db=SimpleNamespace(session=lambda: session),
            http=None,
            llm=SimpleNamespace(credentials=credentials),
        ),
    )


async def test_tool_requires_secret_key_and_admin_key() -> None:
    inp = LlmKeyRotateIn(agent_key="analyst")
    with pytest.raises(Conflict, match="MCP_SECRET_KEY"):
        await intel_keys.intel_llm_key_rotate(inp, _ctx(secret=None))
    with pytest.raises(Conflict, match="LLM_GATEWAY_ADMIN_KEY"):
        await intel_keys.intel_llm_key_rotate(inp, _ctx(admin_key=None))


async def test_tool_rejects_unknown_agent_but_allows_system() -> None:
    with pytest.raises(UnknownAgent):
        await intel_keys.intel_llm_key_rotate(LlmKeyRotateIn(agent_key="ghost"), _ctx(known=None))


async def test_tool_returns_prefix_only_and_invalidates_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_rotate(_db: object, secret: str, admin: GatewayAdmin, **kw: Any) -> Any:
        seen.update(secret=secret, admin_key=admin.admin_key, base_url=admin.base_url, **kw)
        return gateway_admin.RotatedKey(NEW_KEY[:12], ROTATED_AT, True)

    monkeypatch.setattr(intel_keys, "rotate_credential", fake_rotate)
    ctx = _ctx()
    out = await intel_keys.intel_llm_key_rotate(LlmKeyRotateIn(agent_key="analyst"), ctx)

    assert out.model_dump(mode="json") == {
        "agent_key": "analyst",
        "key_prefix": NEW_KEY[:12],
        "rotated_at": "2026-11-06T01:00:00Z",
        "old_key_revoked": True,
    }
    assert seen == {
        "secret": "set",
        "admin_key": "lgw_admin",
        "base_url": "http://gw.test",
        "agent_key": "analyst",
    }
    assert ctx.invalidated == [True]


async def test_tool_maps_admin_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    errors: list[Exception] = [
        UpstreamError("Admin API 401", details={"status": 401}),
        httpx.ConnectError("down"),
    ]

    async def failing(*_a: Any, **_k: Any) -> Any:
        raise errors.pop(0)

    monkeypatch.setattr(intel_keys, "rotate_credential", failing)
    inp = LlmKeyRotateIn(agent_key="system")
    with pytest.raises(UpstreamError, match="ditolak") as rejected:
        await intel_keys.intel_llm_key_rotate(inp, _ctx())
    assert rejected.value.retryable is False
    with pytest.raises(UpstreamError, match="tidak bisa dihubungi") as down:
        await intel_keys.intel_llm_key_rotate(inp, _ctx())
    assert down.value.retryable is True
