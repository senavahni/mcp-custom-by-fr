"""HMAC, perbandingan constant-time, dan pembuatan API key klien (`mcpk_<prefix8>_<secret43>`)."""

import hashlib
import hmac
import re
import secrets
import string

CLIENT_KEY_RE = re.compile(r"^mcpk_([a-z0-9]{8})_([A-Za-z0-9_\-]{43})$")
_PREFIX_ALPHABET = string.ascii_lowercase + string.digits


def hmac_sha256_hex(key: str | bytes, message: str | bytes) -> str:
    k = key.encode() if isinstance(key, str) else key
    m = message.encode() if isinstance(message, str) else message
    return hmac.new(k, m, hashlib.sha256).hexdigest()


def sha256_hex(value: str | bytes) -> str:
    data = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def constant_time_eq(a: str | bytes, b: str | bytes) -> bool:
    ab = a.encode() if isinstance(a, str) else a
    bb = b.encode() if isinstance(b, str) else b
    return hmac.compare_digest(ab, bb)


def generate_client_key() -> tuple[str, str]:
    """Kembalikan `(key_lengkap, prefix)`. Key hanya ditampilkan sekali ke admin."""
    prefix = "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(8))
    secret = secrets.token_urlsafe(32)  # 43 karakter
    return f"mcpk_{prefix}_{secret}", prefix


def parse_client_key(key: str) -> str | None:
    """Prefix key bila formatnya benar, selain itu None."""
    match = CLIENT_KEY_RE.match(key)
    return match.group(1) if match else None


def hash_client_key(pepper: str, key: str) -> str:
    return hmac_sha256_hex(pepper, key)
