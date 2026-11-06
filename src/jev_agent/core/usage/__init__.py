"""Provider usage: one adapter per provider behind a protocol (issue #396).

The normalized model (`UsageWindow` / `UsageSnapshot` / `UsageUnavailable`) is
the only shape the page, route, live poll, and template ever see. Adapters
parse their vendor response exactly once, inside their own module, behind a
versioned parser; unknown fields are ignored, and missing or mistyped
expected fields produce `UsageUnavailable(reason="unexpected_response")`,
never an exception and never a fabricated zero. Token values never appear
here — adapters read them through the shared reader in `provider_auth`
(Phase B for Claude's Keychain path) and never log or return them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Literal, Protocol

from jev_agent.core.provider_auth import LoginSource

if TYPE_CHECKING:
    from jev_agent.core.usage.cache import UsageCacheRecord

__all__ = [
    "CADENCE_FLOOR",
    "MAX_AGES",
    "REASON_APP_SERVER_FAILED",
    "REASON_DISABLED_BY_CONFIG",
    "REASON_ENDPOINT_CHANGED",
    "REASON_KEYCHAIN_DENIED",
    "REASON_KEYCHAIN_UNAVAILABLE",
    "REASON_NETWORK_ERROR",
    "REASON_NOT_APPLICABLE_API_KEY",
    "REASON_NOT_CHECKED_YET",
    "REASON_NO_LOGIN",
    "REASON_RATE_LIMITED",
    "REASON_SETUP_TOKEN_REJECTED",
    "REASON_TIMEOUT",
    "REASON_TOKEN_EXPIRED_REFRESH_FAILED",
    "REASON_UNAUTHORIZED",
    "REASON_UNEXPECTED_RESPONSE",
    "REASON_USAGE_BLOCKED",
    "WINDOW_LABELS",
    "LoginSource",
    "ProviderUsageSource",
    "RefreshResult",
    "UsageSnapshot",
    "UsageUnavailable",
    "UsageWindow",
    "Wording",
    "compact_line",
    "derive_display_state",
    "format_relative",
    "format_reset",
    "fresh_windows",
    "iso_z",
    "left_percent",
    "parse_iso_z",
]

# Reason codes (§6.5). Provider-neutral; the human wording lives in each
# adapter's WORDING mapping, and the template holds no reason wording.
REASON_NOT_CHECKED_YET = "not_checked_yet"
REASON_NO_LOGIN = "no_login"
REASON_NOT_APPLICABLE_API_KEY = "not_applicable_api_key"
REASON_KEYCHAIN_DENIED = "keychain_denied"
REASON_KEYCHAIN_UNAVAILABLE = "keychain_unavailable"
REASON_TOKEN_EXPIRED_REFRESH_FAILED = "token_expired_refresh_failed"
REASON_UNAUTHORIZED = "unauthorized"
REASON_SETUP_TOKEN_REJECTED = "setup_token_rejected"
REASON_RATE_LIMITED = "rate_limited"
REASON_NETWORK_ERROR = "network_error"
REASON_TIMEOUT = "timeout"
REASON_UNEXPECTED_RESPONSE = "unexpected_response"
REASON_ENDPOINT_CHANGED = "endpoint_changed"
REASON_APP_SERVER_FAILED = "app_server_failed"
REASON_USAGE_BLOCKED = "usage_blocked"
REASON_DISABLED_BY_CONFIG = "disabled_by_config"

# Window kinds, derived by the adapter from the response (never hardcoded
# as two fields on the snapshot).
WINDOW_KINDS: tuple[str, ...] = (
    "session",
    "weekly",
    "monthly",
    "weekly_opus",
    "weekly_sonnet",
)

WINDOW_LABELS: dict[str, str] = {
    "session": "Session",
    "weekly": "Weekly",
    "monthly": "Monthly",
    "weekly_opus": "Weekly (Opus)",
    "weekly_sonnet": "Weekly (Sonnet)",
}

# Cadence floors (D6): minimum time between attempts, manual or otherwise,
# measured from `last_attempt_at`.
CADENCE_FLOOR: dict[str, timedelta] = {
    "claude_code": timedelta(minutes=15),
    "codex": timedelta(minutes=5),
}

# Per-window max-ages (D20): the session window is fresh while younger than
# four times the cadence floor; weekly and monthly while younger than 24 h.
MAX_AGES: dict[str, dict[str, timedelta]] = {
    "claude_code": {
        "session": timedelta(minutes=60),
        "weekly": timedelta(hours=24),
        "monthly": timedelta(hours=24),
    },
    "codex": {
        "session": timedelta(minutes=20),
        "weekly": timedelta(hours=24),
        "monthly": timedelta(hours=24),
    },
}

# Reason code -> (UI wording, next step). The shared layer and the template
# only pass and render reason codes; each adapter module exposes its own
# WORDING mapping (§6.1).
Wording = dict[str, tuple[str, str]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def iso_z(moment: datetime | None) -> str | None:
    """ISO-8601 UTC with a Z suffix (None stays None)."""
    if moment is None:
        return None
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso_z(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (Z suffix or offset) to aware UTC.

    Returns None for a missing or unparsable value; a naive value is
    treated as UTC.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class UsageWindow:
    kind: str  # "session" | "weekly" | "monthly" | "weekly_opus" | "weekly_sonnet"
    used_percent: float  # 0-100, as reported
    resets_at: datetime | None  # as reported; None -> bar with "no reset reported"


@dataclass(frozen=True)
class UsageSnapshot:
    provider: str
    windows: list[UsageWindow]  # derived from the response, never hardcoded as two fields
    plan: str | None  # Codex planType, verbatim; Claude: None
    fetched_at: datetime  # last successful fetch
    source_version: str  # "cli/<cli_version> parser/<PARSER_VERSION>"; "unknown" when unavailable
    # Additive (needed by the cache/fetcher layers): the SHA-256 hex digest
    # of the Codex accountId (None for Claude), and whether the provider
    # reported a spend limit reached (§6.3).
    account_hash: str | None = None
    spend_limit_reached: bool = False


@dataclass(frozen=True)
class UsageUnavailable:
    provider: str
    reason: str  # reason code, section 6.5
    fetched_at: datetime | None  # last successful fetch, if any
    last_good: UsageSnapshot | None  # last-good windows, if any
    # Additive: for `rate_limited`, the earliest next attempt (from Retry-After).
    next_retry_at: datetime | None = None


class ProviderUsageSource(Protocol):
    """One usage source behind the protocol. `fetch` takes no arguments —
    adapters take what they need in their constructors."""

    def fetch(self) -> UsageSnapshot | UsageUnavailable: ...


@dataclass(frozen=True)
class RefreshResult:
    outcome: str  # "started" | "already_running" | "too_soon" | "disabled" |
    # "not_enabled" | "auth_stopped" | "no_login"
    wait_seconds: float | None = None  # for "too_soon": seconds until the next attempt


def _max_age_for(
    kind: str,
    session_max_age: timedelta,
    weekly_max_age: timedelta,
    monthly_max_age: timedelta,
) -> timedelta:
    if kind == "monthly":
        return monthly_max_age
    if kind in ("weekly", "weekly_opus", "weekly_sonnet"):
        return weekly_max_age
    return session_max_age


def fresh_windows(
    record: UsageCacheRecord,
    now: datetime,
    session_max_age: timedelta,
    weekly_max_age: timedelta,
    monthly_max_age: timedelta,
) -> list[UsageWindow]:
    """The record's windows still within their own max-age (D20)."""
    if record.fetched_at is None:
        return []
    age = now - record.fetched_at
    return [
        w
        for w in record.windows
        if age <= _max_age_for(w.kind, session_max_age, weekly_max_age, monthly_max_age)
    ]


def derive_display_state(
    *,
    record: UsageCacheRecord | None,
    fetch_running: bool,
    now: datetime,
    session_max_age: timedelta,
    weekly_max_age: timedelta,
    monthly_max_age: timedelta,
) -> Literal["never", "collecting", "ready", "failed", "stale"]:
    """The card's display state, derived at read time (§6.4).

    The value is shown only while a window is fresh — stale wins over
    collecting/failed for showing the value. A failed refetch never
    overwrites the snapshot: on a still-fresh snapshot the state stays
    "ready" and the view adds the sub-line "last check failed — <reason>".
    """
    if record is None:
        return "collecting" if fetch_running else "never"
    fresh = fresh_windows(record, now, session_max_age, weekly_max_age, monthly_max_age)
    if fresh:
        return "collecting" if fetch_running else "ready"
    if fetch_running:
        return "collecting"
    if record.windows:
        return "stale"
    if record.last_status and record.last_status != "ok":
        return "failed"
    return "never"


def left_percent(used_percent: float) -> int:
    """100 - used, integer, nearest, half up (D14: 62.5 → 63)."""
    value = (Decimal(100) - Decimal(str(used_percent))).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(value)


def format_relative(delta: timedelta) -> str:
    """D14 relative phrasing: under 1 minute → "just now"; under 1 hour →
    minutes ("9 min"); under 1 day → hours + minutes, minutes shown when
    ≥ 1 ("2 h 14 min"); 1 day or more → days only, rounded down ("3 d")."""
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        hours, rem = divmod(seconds, 3600)
        minutes = rem // 60
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    return f"{seconds // 86400} d"


def format_reset(resets_at: datetime | None, now: datetime) -> str:
    """The reset half of a window line: None → "no reset reported"; in the
    past → "resetting…"; otherwise "resets in <relative>"."""
    if resets_at is None:
        return "no reset reported"
    if resets_at <= now:
        return "resetting…"
    return f"resets in {format_relative(resets_at - now)}"


def compact_line(windows: list[UsageWindow], now: datetime) -> str:
    """The one compact line per provider (D14): "Session 62% left · resets
    in 2 h 14 min · Weekly 81% left · resets in 3 d" — for a monthly-only
    account, "Monthly 52% left · resets in 20 d"."""
    parts = []
    for window in windows:
        label = WINDOW_LABELS.get(window.kind, window.kind)
        parts.append(
            f"{label} {left_percent(window.used_percent)}% left · "
            f"{format_reset(window.resets_at, now)}"
        )
    return " · ".join(parts)
