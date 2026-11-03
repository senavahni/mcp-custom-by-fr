"""Signature webhook MCP -> Dashboard: `sha256=HMAC_SHA256(secret, "<ts>.<raw_body>")`.

Setara `signMcpPayload` di `backend/src/utils/helpers/crypto.util.ts`; body yang dikirim harus
byte yang sama persis dengan yang ditandatangani.
"""

from __future__ import annotations

import hashlib
import hmac
import time


def sign(secret: str, timestamp: str, raw_body: bytes) -> str:
    mac = hmac.new(secret.encode(), digestmod=hashlib.sha256)
    mac.update(f"{timestamp}.".encode())
    mac.update(raw_body)
    return f"sha256={mac.hexdigest()}"


def signed_headers(secret: str, raw_body: bytes, *, now: float | None = None) -> dict[str, str]:
    ts = str(int(now if now is not None else time.time()))
    return {
        "Content-Type": "application/json",
        "X-MCP-Timestamp": ts,
        "X-MCP-Signature": sign(secret, ts, raw_body),
    }


def verify(
    secrets: list[str],
    timestamp: str | None,
    signature: str | None,
    raw_body: bytes,
    *,
    tolerance_s: int = 300,
    now: float | None = None,
) -> str:
    """Port `verifyMcpSignature` (test & webhook palsu): ok|missing|expired|invalid."""
    if not timestamp or not signature or raw_body is None:
        return "missing"
    try:
        ts = int(timestamp)
    except ValueError:
        return "expired"
    current = int(now if now is not None else time.time())
    if abs(current - ts) > tolerance_s:
        return "expired"
    ok = any(s and hmac.compare_digest(sign(s, timestamp, raw_body), signature) for s in secrets)
    return "ok" if ok else "invalid"
