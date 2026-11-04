"""Enkripsi simetris AES-256-GCM untuk secret yang harus bisa dibaca ulang (key LLM Gateway).

Token = base64url(nonce 12 byte || ciphertext+tag). `aad` mengikat ciphertext ke barisnya (mis.
nama credential) sehingga menyalin ciphertext ke baris lain gagal didekripsi.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12


class SecretBoxError(ValueError):
    """Kunci salah, data rusak, atau aad tidak cocok (pesan tidak memuat isi secret)."""


def _cipher(key_b64: str) -> AESGCM:
    return AESGCM(base64.b64decode(key_b64, validate=True))


def seal(key_b64: str, plaintext: str, aad: str) -> str:
    nonce = os.urandom(NONCE_BYTES)
    ct = _cipher(key_b64).encrypt(nonce, plaintext.encode(), aad.encode())
    return base64.urlsafe_b64encode(nonce + ct).decode()


def open_sealed(key_b64: str, token: str, aad: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        return _cipher(key_b64).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], aad.encode()).decode()
    except (InvalidTag, ValueError) as exc:
        raise SecretBoxError(
            "secret tidak dapat didekripsi (kunci/aad salah atau data rusak)"
        ) from exc


def generate_key() -> str:
    """Kunci baru untuk `MCP_SECRET_KEY` (32 byte, base64)."""
    return base64.b64encode(os.urandom(32)).decode()
