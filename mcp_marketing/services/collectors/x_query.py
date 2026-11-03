"""Builder query X, estimasi biaya, dan aturan pemilihan provider (TASK-15)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from mcp_marketing.core.errors import ValidationFailed

X_API_WINDOW = timedelta(days=7)
MAX_PERIOD_DAYS = 366


def _term(keyword: str) -> str:
    k = keyword.strip()
    return f'"{k}"' if " " in k and not (k.startswith('"') and k.endswith('"')) else k


def build_query(
    keywords: list[str],
    *,
    match: str = "any",
    lang: str | None = "id",
    exclude_retweets: bool = True,
    exclude_replies: bool = False,
) -> str:
    """Query X (dipakai API resmi & pencarian web): `(k1 OR k2) lang:id -is:retweet [-is:reply]`."""
    terms = [_term(k) for k in keywords if k.strip()]
    if not terms:
        raise ValidationFailed("keywords minimal 1")
    joiner = " OR " if match == "any" else " "
    core = terms[0] if len(terms) == 1 else f"({joiner.join(terms)})"
    parts = [core]
    if lang:
        parts.append(f"lang:{lang}")
    if exclude_retweets:
        parts.append("-is:retweet")
    if exclude_replies:
        parts.append("-is:reply")
    return " ".join(parts)


def web_query(
    keywords: list[str],
    *,
    match: str,
    lang: str | None,
    day_start: str,
    day_end: str,
    exclude_retweets: bool,
    exclude_replies: bool,
) -> str:
    """Pencarian lanjutan web X memakai operator `since:`/`until:` dan `-filter:` (bukan `-is:`)."""
    q = build_query(keywords, match=match, lang=lang, exclude_retweets=False, exclude_replies=False)
    q += f" since:{day_start} until:{day_end}"
    if exclude_retweets:
        q += " -filter:retweets"
    if exclude_replies:
        q += " -filter:replies"
    return q


def validate_period(start: datetime, end: datetime) -> None:
    if end - start > timedelta(days=MAX_PERIOD_DAYS):
        raise ValidationFailed(
            f"Periode maksimal {MAX_PERIOD_DAYS} hari", details={"field": "until"}
        )


@dataclass(frozen=True)
class ProviderDecision:
    provider: str
    reason: str


def choose_provider(
    *,
    requested: str,
    has_token: bool,
    start: datetime,
    now: datetime,
    estimate_units: int,
    remaining_quota: int,
    browser_ready: bool,
) -> ProviderDecision:
    """Aturan MCP_SPEC §4.6 `provider=auto`; pilihan eksplisit divalidasi (tanpa memanggil API)."""
    within_window = start >= now - X_API_WINDOW
    if requested == "x_api":
        if not has_token:
            raise ValidationFailed("X_BEARER_TOKEN belum dikonfigurasi; pakai provider=browser")
        if not within_window:
            raise ValidationFailed(
                "X API recent search hanya 7 hari terakhir; pakai provider=browser",
                details={"field": "since", "suggest": "browser"},
            )
        if estimate_units > remaining_quota:
            from mcp_marketing.core.errors import BudgetExceeded

            raise BudgetExceeded(
                "Estimasi pembacaan X melebihi sisa kuota harian",
                details={
                    "requested": estimate_units,
                    "remaining": remaining_quota,
                    "scope": "external:x_reads",
                },
            )
        return ProviderDecision("x_api", "diminta")
    if requested == "browser":
        if not browser_ready:
            raise ValidationFailed(
                "Tidak ada profil browser dengan login x.com yang OK (browser_profile_check)"
            )
        return ProviderDecision("browser", "diminta")
    if has_token and within_window and estimate_units <= remaining_quota:
        return ProviderDecision("x_api", "token tersedia, periode ≤ 7 hari, kuota cukup")
    if browser_ready:
        return ProviderDecision("browser", "periode > 7 hari / token/kuota tidak cukup")
    from mcp_marketing.core.errors import UpstreamError

    raise UpstreamError(
        "Tidak ada provider X yang siap: isi X_BEARER_TOKEN (≤ 7 hari) atau siapkan profil "
        "browser login x.com",
        retryable=False,
    )


def estimate_cost(max_items: int, price_per_read: float) -> float:
    return round(max_items * price_per_read, 4)
