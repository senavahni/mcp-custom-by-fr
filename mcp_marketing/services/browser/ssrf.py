"""Guard SSRF: hanya http/https ke alamat publik (cek ulang setelah redirect).

Alamat private/loopback/link-local/metadata ditolak kecuali `SCRAPE_ALLOW_PRIVATE_NETWORK=true`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from mcp_marketing.core.errors import Forbidden, ValidationFailed

ALLOWED_SCHEMES = frozenset({"http", "https"})
METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata"})


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def parse_http_url(url: str) -> tuple[str, str]:
    """Kembalikan `(scheme, host)`; tolak skema selain http/https."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise ValidationFailed(f"URL tidak valid: {url[:200]}") from None
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname:
        raise ValidationFailed("Hanya URL http/https yang diizinkan", details={"url": url[:200]})
    return parts.scheme.lower(), parts.hostname.lower()


async def resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValidationFailed(f"Host tidak dapat di-resolve: {host}") from None
    return sorted({str(info[4][0]) for info in infos})


async def check_url(url: str, *, allow_private: bool = False) -> None:
    """Lempar `Forbidden` (code FORBIDDEN, status halaman `ssrf_denied`) bila tujuan tidak aman."""
    _, host = parse_http_url(url)
    if allow_private:
        return
    if host in METADATA_HOSTS:
        raise Forbidden(f"Host diblokir (metadata): {host}", details={"reason": "ssrf_denied"})
    try:
        addresses = [str(ipaddress.ip_address(host.strip("[]")))]
    except ValueError:
        addresses = await resolve(host)
    for addr in addresses:
        if _blocked_ip(ipaddress.ip_address(addr)):
            raise Forbidden(
                f"Alamat private/internal tidak diizinkan: {host}",
                details={"reason": "ssrf_denied", "address": addr},
            )
