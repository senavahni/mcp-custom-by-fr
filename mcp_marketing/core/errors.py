"""Model error tunggal untuk semua tool: `{code, message, retryable, details?}` (MCP_SPEC §2.3)."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import httpx
from pydantic import ValidationError

ErrorCode = Literal[
    "VALIDATION_ERROR",
    "NOT_FOUND",
    "FORBIDDEN",
    "RATE_LIMITED",
    "BUDGET_EXCEEDED",
    "UPSTREAM_ERROR",
    "UPSTREAM_TIMEOUT",
    "JOB_FAILED",
    "CONFLICT",
    "UNSUPPORTED_CONTRACT_VERSION",
    "RUN_NOT_ACTIVE",
    "PROFILE_BUSY",
    "LOGIN_REQUIRED",
    "UNKNOWN_AGENT",
    "HIERARCHY_DEPTH_EXCEEDED",
    "INTERNAL",
    # kontrak 1.2 toolset `intel`
    "AGENT_INACTIVE",
    "AGENT_NOT_CAPABLE",
    "COST_CONFIRMATION_REQUIRED",
    "SOURCE_UNAVAILABLE",
    "JOB_NOT_ACTIVE",
    "RESULT_EXPIRED",
    "LLM_KEY_INVALID",
    "LLM_QUOTA_EXCEEDED",
    "LLM_UNAVAILABLE",
    "LLM_REQUEST_INVALID",
    "WORKER_LOST",
    # kontrak 1.3 (v1.0.0)
    "UPLOAD_TOO_LARGE",
    "UNSUPPORTED_FILE",
    "REVIEW_REQUIRED",
    "CAPTCHA_REQUIRED",
    "LOCAL_MODEL_UNAVAILABLE",
]
MAX_VALIDATION_FIELDS = 5
MAX_MESSAGE_CHARS = 500


class ToolError(Exception):
    code: ErrorCode = "INTERNAL"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message[:MAX_MESSAGE_CHARS]
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.details = details

    def to_structured(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            out["details"] = self.details
        return out

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class ValidationFailed(ToolError):
    code = "VALIDATION_ERROR"


class NotFound(ToolError):
    code = "NOT_FOUND"


class Forbidden(ToolError):
    code = "FORBIDDEN"


class RateLimited(ToolError):
    code = "RATE_LIMITED"
    retryable = True


class BudgetExceeded(ToolError):
    code = "BUDGET_EXCEEDED"


class UpstreamError(ToolError):
    code = "UPSTREAM_ERROR"
    retryable = True


class UpstreamTimeout(ToolError):
    code = "UPSTREAM_TIMEOUT"
    retryable = True


class JobFailed(ToolError):
    code = "JOB_FAILED"


class Conflict(ToolError):
    code = "CONFLICT"


class UnsupportedContractVersion(ToolError):
    code = "UNSUPPORTED_CONTRACT_VERSION"


class RunNotActive(ToolError):
    code = "RUN_NOT_ACTIVE"


class ProfileBusy(ToolError):
    code = "PROFILE_BUSY"
    retryable = True


class LoginRequired(ToolError):
    code = "LOGIN_REQUIRED"


class UnknownAgent(ToolError):
    code = "UNKNOWN_AGENT"


class HierarchyDepthExceeded(ToolError):
    code = "HIERARCHY_DEPTH_EXCEEDED"


class InternalError(ToolError):
    code = "INTERNAL"


class AgentInactive(ToolError):
    code = "AGENT_INACTIVE"


class AgentNotCapable(ToolError):
    code = "AGENT_NOT_CAPABLE"


class CostConfirmationRequired(ToolError):
    code = "COST_CONFIRMATION_REQUIRED"


class SourceUnavailable(ToolError):
    code = "SOURCE_UNAVAILABLE"


class JobNotActive(ToolError):
    code = "JOB_NOT_ACTIVE"


class ResultExpired(ToolError):
    code = "RESULT_EXPIRED"


class UploadTooLarge(ToolError):
    code = "UPLOAD_TOO_LARGE"


class UnsupportedFile(ToolError):
    code = "UNSUPPORTED_FILE"


class ReviewRequired(ToolError):
    code = "REVIEW_REQUIRED"


class CaptchaRequired(ToolError):
    """Google menampilkan CAPTCHA/consent: job pause, user menyelesaikan manual di Chrome."""

    code = "CAPTCHA_REQUIRED"
    retryable = True


class LocalModelUnavailable(ToolError):
    """`embed-local` mati: job bulk pause, tidak jatuh ke model berdimensi lain (V1-K7)."""

    code = "LOCAL_MODEL_UNAVAILABLE"
    retryable = True


def validation_error_from(exc: ValidationError) -> ValidationFailed:
    """Ringkas error Pydantic: maks 5 field, tanpa nilai input (bisa berisi data sensitif)."""
    fields = []
    for err in exc.errors()[:MAX_VALIDATION_FIELDS]:
        loc = ".".join(str(p) for p in err["loc"]) or "input"
        fields.append({"field": loc, "error": err["msg"]})
    first = fields[0] if fields else {"field": "input", "error": "invalid"}
    return ValidationFailed(
        f"{first['field']}: {first['error']}",
        details={"fields": fields, "count": exc.error_count()},
    )


def to_tool_error(exc: BaseException) -> ToolError:
    """Petakan exception apa pun ke ToolError. Detail internal hanya untuk log, bukan pesan."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, ValidationError):
        return validation_error_from(exc)
    if isinstance(exc, httpx.TimeoutException | asyncio.TimeoutError | TimeoutError):
        return UpstreamTimeout("Upstream tidak merespons tepat waktu")
    return InternalError("Terjadi kesalahan internal")
