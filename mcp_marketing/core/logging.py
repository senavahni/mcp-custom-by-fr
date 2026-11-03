"""Log JSON terstruktur (structlog + orjson) ke stdout dan file harian, dengan redaksi secret."""

from __future__ import annotations

import logging
import re
import sys
import threading
from collections.abc import Mapping, MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import structlog

from mcp_marketing.core import context

SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "token",
        "secret",
        "password",
        "cookie",
        "set-cookie",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "x-mcp-signature",
        "pepper",
        "key_hash",
    }
)
# "token" hanya sebagai akhiran `_token` agar metrik seperti `output_tokens` tidak tersamarkan
SENSITIVE_KEY_PARTS = ("secret", "password", "api_key", "pepper", "authorization", "cookie")
REDACTED = "***"
SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=\-]+", re.IGNORECASE),
    re.compile(r"\blgw_[A-Za-z0-9_\-]+"),
    re.compile(r"\bmcpk_[A-Za-z0-9_\-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?<=://)[^:/@\s]+:[^@\s]+(?=@)"),
    re.compile(r"sha256=[0-9a-f]{64}"),
    re.compile(r"(?<=[?&]sig=)[0-9a-f]{64}"),
]
FILE_PREFIX = "mcp-"
KEEP_FILES = 14


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return (
        lowered in SENSITIVE_KEYS
        or lowered.endswith("_token")
        or any(part in lowered for part in SENSITIVE_KEY_PARTS)
    )


def redact_text(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if isinstance(k, str) and _is_sensitive_key(k) else redact(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v, depth + 1) for v in value]
    return value


def _redact_processor(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> Any:
    return redact(event_dict)


def _context_processor(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> Any:
    for key, value in context.snapshot().items():
        event_dict.setdefault(key, value)
    return event_dict


def _orjson_dumps(obj: Any, default: Any = None) -> str:
    return orjson.dumps(obj, default=default or str).decode()


class DailyFileHandler(logging.Handler):
    """Tulis ke `LOG_DIR/mcp-YYYY-MM-DD.log` (UTC); simpan `KEEP_FILES` file terakhir."""

    def __init__(self, log_dir: Path, keep: int = KEEP_FILES) -> None:
        super().__init__()
        self.log_dir = log_dir
        self.keep = keep
        self._day: str | None = None
        self._stream: Any = None
        self._lock_io = threading.Lock()
        log_dir.mkdir(parents=True, exist_ok=True)

    def _rollover(self, day: str) -> None:
        if self._stream:
            self._stream.close()
        self._stream = (self.log_dir / f"{FILE_PREFIX}{day}.log").open("a", encoding="utf-8")
        self._day = day
        files = sorted(self.log_dir.glob(f"{FILE_PREFIX}*.log"))
        for old in files[: max(0, len(files) - self.keep)]:
            old.unlink(missing_ok=True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            with self._lock_io:
                if day != self._day:
                    self._rollover(day)
                self._stream.write(line + "\n")
                self._stream.flush()
        except Exception:  # noqa: BLE001 - logging tidak boleh menjatuhkan aplikasi
            self.handleError(record)

    def close(self) -> None:
        with self._lock_io:
            if self._stream:
                self._stream.close()
                self._stream = None
        super().close()


def _shared_processors() -> list[Any]:
    return [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        _context_processor,
        structlog.processors.format_exc_info,
        _redact_processor,
    ]


def configure_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """Konfigurasi structlog + stdlib (uvicorn, sqlalchemy) agar semua log berformat JSON sama."""
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(serializer=_orjson_dumps),
        ],
        foreign_pre_chain=[structlog.stdlib.ExtraAdder(), *_shared_processors()],
    )
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir is not None:
        handlers.append(DailyFileHandler(log_dir))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
        old.close()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "mcp.server.lowlevel.server"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
