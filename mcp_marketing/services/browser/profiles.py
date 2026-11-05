"""Profil browser terdaftar (`mcp.browser_profiles`) + lock eksklusif per job.

Chrome mengunci `--user-data-dir`, jadi satu profil hanya boleh dipakai satu job. Lock diambil
atomik (`UPDATE … WHERE locked_by_job IS NULL`); job lain menunggu ≤ 60 s lalu `PROFILE_BUSY`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select, update

from mcp_marketing.core.database import Database
from mcp_marketing.core.errors import Conflict, NotFound, ProfileBusy, ValidationFailed
from mcp_marketing.models.browser import BrowserProfile
from mcp_marketing.utils.time import now

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
LOCK_WAIT_S = 60.0
LOCK_POLL_S = 1.0
STALE_LOCK = timedelta(hours=3)
DEFAULT_PROFILE = "default"


def profile_view(p: BrowserProfile) -> dict[str, Any]:
    return {
        "name": p.name,
        "purpose": p.purpose,
        "allowed_sites": list(p.allowed_sites or []),
        "logged_in_sites": sorted(
            s for s, v in (p.login_status or {}).items() if isinstance(v, dict) and v.get("ok")
        ),
        "headless": p.headless,
        "locked": bool(p.locked_by_job),
        "locked_by_job": p.locked_by_job,
        "last_used": p.last_used_at.isoformat() if p.last_used_at else None,
        "is_active": p.is_active,
    }


class ProfileStore:
    def __init__(
        self,
        db: Database,
        profiles_dir: Path,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.db = db
        self.profiles_dir = profiles_dir
        self._sleep = sleep

    async def add(
        self,
        *,
        name: str,
        purpose: str | None = None,
        sites: list[str] | None = None,
        headless: bool = False,
        user_agent: str | None = None,
        proxy_env: str | None = None,
        profile_directory: str = "Default",
        window_size: str = "1366,900",
    ) -> BrowserProfile:
        if not NAME_RE.match(name):
            raise ValidationFailed("nama profil: huruf kecil/angka/-/_ (2–63 karakter)")
        if proxy_env and not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", proxy_env):
            raise ValidationFailed("proxy_env harus NAMA variabel environment, bukan URL proxy")
        user_data_dir = (self.profiles_dir / name).resolve()
        async with self.db.session() as session:
            if await session.scalar(select(BrowserProfile.id).where(BrowserProfile.name == name)):
                raise Conflict(f"Profil '{name}' sudah ada")
            profile = BrowserProfile(
                name=name,
                purpose=purpose,
                user_data_dir=str(user_data_dir),
                profile_directory=profile_directory,
                user_agent=user_agent,
                window_size=window_size,
                proxy_url_env=proxy_env,
                headless=headless,
                allowed_sites=sorted({s.strip().lower() for s in sites or [] if s.strip()}),
            )
            session.add(profile)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        return profile

    async def ensure_default(self) -> BrowserProfile:
        existing = await self.find(DEFAULT_PROFILE)
        if existing is not None:
            return existing
        return await self.add(
            name=DEFAULT_PROFILE, purpose="Scraping umum tanpa login", headless=True
        )

    async def find(self, name: str) -> BrowserProfile | None:
        async with self.db.session() as session:
            return await session.scalar(select(BrowserProfile).where(BrowserProfile.name == name))

    async def get(self, name: str) -> BrowserProfile:
        profile = await self.find(name)
        if profile is None or not profile.is_active:
            raise NotFound(f"Profil browser '{name}' tidak ditemukan/nonaktif")
        return profile

    async def list(self, *, include_inactive: bool = False) -> list[BrowserProfile]:
        stmt = select(BrowserProfile).order_by(BrowserProfile.name)
        if not include_inactive:
            stmt = stmt.where(BrowserProfile.is_active.is_(True))
        async with self.db.session() as session:
            return list((await session.scalars(stmt)).all())

    async def for_site(self, site: str) -> BrowserProfile | None:
        """Profil aktif yang mengizinkan `site` dan berstatus login OK (untuk crawler X/Threads)."""
        for profile in await self.list():
            status = (profile.login_status or {}).get(site) or {}
            if site in (profile.allowed_sites or []) and status.get("ok"):
                return profile
        return None

    async def disable(self, name: str) -> None:
        async with self.db.session() as session:
            result = await session.execute(
                update(BrowserProfile).where(BrowserProfile.name == name).values(is_active=False)
            )
        if not result.rowcount:  # type: ignore[attr-defined]
            raise NotFound(f"Profil '{name}' tidak ditemukan")

    async def allow_sites(self, name: str, sites: Iterable[str]) -> None:
        """Tambahkan situs ke `allowed_sites` (setelah login manual dari Dashboard)."""
        async with self.db.session() as session:
            profile = await session.scalar(
                select(BrowserProfile).where(BrowserProfile.name == name)
            )
            if profile is None:
                raise NotFound(f"Profil '{name}' tidak ditemukan")
            profile.allowed_sites = sorted({*(profile.allowed_sites or []), *sites})

    async def set_login_status(self, name: str, status: dict[str, dict[str, Any]]) -> None:
        async with self.db.session() as session:
            profile = await session.scalar(
                select(BrowserProfile).where(BrowserProfile.name == name)
            )
            if profile is None:
                raise NotFound(f"Profil '{name}' tidak ditemukan")
            profile.login_status = {**(profile.login_status or {}), **status}

    async def acquire(
        self, name: str, job_id: str, *, wait_s: float = LOCK_WAIT_S
    ) -> BrowserProfile:
        profile = await self.get(name)
        waited = 0.0
        while True:
            async with self.db.session() as session:
                result = await session.execute(
                    update(BrowserProfile)
                    .where(
                        BrowserProfile.id == profile.id,
                        or_(
                            BrowserProfile.locked_by_job.is_(None),
                            BrowserProfile.locked_by_job == job_id,
                            BrowserProfile.locked_at < now() - STALE_LOCK,
                        ),
                    )
                    .values(locked_by_job=job_id, locked_at=now(), last_used_at=now())
                )
            if result.rowcount:  # type: ignore[attr-defined]
                return profile
            if waited >= wait_s:
                raise ProfileBusy(
                    f"Profil '{name}' sedang dipakai job lain", details={"profile": name}
                )
            await self._sleep(LOCK_POLL_S)
            waited += LOCK_POLL_S

    async def release(self, name: str, job_id: str) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(BrowserProfile)
                .where(BrowserProfile.name == name, BrowserProfile.locked_by_job == job_id)
                .values(locked_by_job=None, locked_at=None)
            )
