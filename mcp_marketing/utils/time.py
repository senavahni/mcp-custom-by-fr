"""Waktu: semua disimpan UTC; input tanpa offset ditafsirkan di zona `TIMEZONE`."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from mcp_marketing.core.errors import ValidationFailed

DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def now() -> datetime:
    """Waktu sekarang (UTC, timezone-aware). Bisa di-freeze dengan freezegun."""
    return datetime.now(UTC)


def _parse_one(value: str, tz: ZoneInfo, *, end_of_day: bool, field: str) -> datetime:
    value = value.strip()
    try:
        if DATE_ONLY_RE.match(value):
            day = date.fromisoformat(value)
            local = datetime.combine(day, time.max if end_of_day else time.min, tzinfo=tz)
            return local.astimezone(UTC)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationFailed(
            f"{field} harus YYYY-MM-DD atau ISO-8601", details={"field": field}
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(UTC)


def parse_period(since: str, until: str, tz: str = "Asia/Jakarta") -> tuple[datetime, datetime]:
    """`since` = awal hari, `until` = akhir hari (inklusif) untuk input tanggal saja."""
    zone = ZoneInfo(tz)
    start = _parse_one(since, zone, end_of_day=False, field="since")
    end = _parse_one(until, zone, end_of_day=True, field="until")
    if end <= start:
        raise ValidationFailed("until harus > since", details={"field": "until"})
    return start, end


def today_in(tz: str) -> date:
    return now().astimezone(ZoneInfo(tz)).date()


def is_workday(day: date) -> bool:
    return day.weekday() < 5


def next_workday(day: date) -> date:
    while not is_workday(day):
        day += timedelta(days=1)
    return day


def add_workdays(start: date, workdays: int) -> date:
    """Tanggal hari kerja ke-`workdays` dihitung dari `start` (start = hari ke-1)."""
    day = next_workday(start)
    remaining = max(1, workdays) - 1
    while remaining > 0:
        day += timedelta(days=1)
        if is_workday(day):
            remaining -= 1
    return day
