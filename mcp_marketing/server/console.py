"""Admin Console (`/console`): halaman statis pengelola kredensial, hanya dari localhost.

Halaman tidak memuat data; semua data diambil lewat `/admin/*` memakai key klien admin yang diketik
operator dan disimpan di memori tab saja. Guard di sini lapis tambahan: koneksi harus dari loopback
(peer socket, bukan X-Forwarded-For) dengan Host lokal (anti DNS rebinding).
"""

from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Route

CONSOLE_DIR = Path(__file__).resolve().parents[1] / "console"
ASSETS = {"console.js": "text/javascript", "console.css": "text/css", "kit.js": "text/javascript"}
LOOPBACK_PEERS = {"127.0.0.1", "::1"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _hostname(host_header: str) -> str:
    if host_header.startswith("["):
        return host_header.split("]", 1)[0] + "]"
    return host_header.rsplit(":", 1)[0]


def is_local_request(request: Request) -> bool:
    peer = request.client.host if request.client else None
    return peer in LOOPBACK_PEERS and _hostname(request.headers.get("host", "")) in LOCAL_HOSTS


def _forbidden() -> Response:
    return PlainTextResponse(
        "Admin Console hanya bisa dibuka dari komputer ini (http://127.0.0.1).",
        status_code=403,
        headers=SECURITY_HEADERS,
    )


async def console_page(request: Request) -> Response:
    if not is_local_request(request):
        return _forbidden()
    return FileResponse(
        CONSOLE_DIR / "index.html", media_type="text/html", headers=SECURITY_HEADERS
    )


async def console_asset(request: Request) -> Response:
    if not is_local_request(request):
        return _forbidden()
    name = request.path_params["name"]
    media_type = ASSETS.get(name)
    if media_type is None:
        return PlainTextResponse("not found", status_code=404, headers=SECURITY_HEADERS)
    return FileResponse(CONSOLE_DIR / name, media_type=media_type, headers=SECURITY_HEADERS)


def build_console_routes() -> list[Route]:
    return [
        Route("/console", console_page, methods=["GET"]),
        Route("/console/{name}", console_asset, methods=["GET"]),
    ]
