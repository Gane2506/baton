"""Host-level usage cache (issue #396, §6.4/D5).

One JSON file per provider under `~/.jev/` (`usage-cache-<provider>.json`),
mode 600, atomic write (temp file + rename, reusing the approach of
`write_secret` but not its refusal behaviour — a cache write never refuses).
One snapshot per provider, overwritten in place. No token values, no usage
history. Storage only — single-flight lives in the fetcher, not the cache.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jev_agent.core.usage import UsageWindow, _utcnow, iso_z, parse_iso_z

log = logging.getLogger(__name__)

# Corrupt cache files are logged once per provider per dashboard process.
_CORRUPT_LOGGED: set[str] = set()


@dataclass(frozen=True)
class UsageCacheRecord:
    """One provider's cached usage snapshot + attempt metadata (D5).

    `fetched_at` is always the last *successful* fetch; `last_attempt_at`
    is the last attempt. `next_retry_at` survives a dashboard restart, so
    a restart never retries into a rate limit early. `plan` (the Codex
    `planType` verbatim) and `spend_limit_reached` are additive: the poll
    JSON reports the plan, and the card renders the spend sub-line, so both
    must survive a restart with the snapshot.
    """

    schema_version: int = 1
    enabled: bool = False  # D3 opt-in: nothing is fetched until the user clicks once
    fetched_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_status: str | None = None  # "ok" after a successful fetch, else the last reason code
    last_reason: str | None = None
    next_retry_at: datetime | None = None
    source_version: str | None = None
    account_hash: str | None = None  # Codex only
    plan: str | None = None
    spend_limit_reached: bool = False
    windows: list[UsageWindow] = field(default_factory=list)


class UsageCache:
    """Host-level cache records, one file per provider. `read` never blocks
    and never fetches; `write` applies the stale-write guard (§6.4)."""

    def __init__(self, *, home: Path, clock: Any = _utcnow) -> None:
        self._home = home
        self._clock = clock

    def _path(self, provider: str) -> Path:
        return self._home / ".jev" / f"usage-cache-{provider}.json"

    def read(self, provider: str) -> UsageCacheRecord | None:
        """The stored record, or None when there is none or it is corrupt.
        A corrupt file is treated as empty and logged once; the card then
        shows "Check usage"."""
        try:
            raw = json.loads(self._path(provider).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        record = self._parse(raw)
        if record is None and provider not in _CORRUPT_LOGGED:
            _CORRUPT_LOGGED.add(provider)
            log.warning("usage cache: ignoring corrupt record for %s", provider)
        return record

    def _parse(self, raw: Any) -> UsageCacheRecord | None:
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            return None
        try:
            windows = []
            raw_windows = raw.get("windows", [])
            if not isinstance(raw_windows, list):
                return None
            for item in raw_windows:
                if not isinstance(item, dict):
                    return None
                kind = item.get("kind")
                used = item.get("used_percent")
                if not isinstance(kind, str) or isinstance(used, bool) or not isinstance(
                    used, (int, float)
                ):
                    return None
                resets_at = parse_iso_z(item.get("resets_at"))
                if item.get("resets_at") is not None and resets_at is None:
                    return None
                windows.append(
                    UsageWindow(kind=kind, used_percent=float(used), resets_at=resets_at)
                )
            fetched_at = parse_iso_z(raw.get("fetched_at"))
            last_attempt_at = parse_iso_z(raw.get("last_attempt_at"))
            next_retry_at = parse_iso_z(raw.get("next_retry_at"))
            if (
                (raw.get("fetched_at") is not None and fetched_at is None)
                or (raw.get("last_attempt_at") is not None and last_attempt_at is None)
                or (raw.get("next_retry_at") is not None and next_retry_at is None)
                or (windows and fetched_at is None)
            ):
                return None
            enabled = raw.get("enabled", False)
            if not isinstance(enabled, bool):
                return None
            spend = raw.get("spend_limit_reached", False)
            if not isinstance(spend, bool):
                return None
            plan = raw.get("plan")
            if plan is not None and not isinstance(plan, str):
                return None
            account_hash = raw.get("account_hash")
            if account_hash is not None and not isinstance(account_hash, str):
                return None
            last_status = raw.get("last_status")
            last_reason = raw.get("last_reason")
            source_version = raw.get("source_version")
            for value in (last_status, last_reason, source_version):
                if value is not None and not isinstance(value, str):
                    return None
            return UsageCacheRecord(
                schema_version=1,
                enabled=enabled,
                fetched_at=fetched_at,
                last_attempt_at=last_attempt_at,
                last_status=last_status,
                last_reason=last_reason,
                next_retry_at=next_retry_at,
                source_version=source_version,
                account_hash=account_hash,
                plan=plan,
                spend_limit_reached=spend,
                windows=windows,
            )
        except (TypeError, ValueError):
            return None

    def write(self, provider: str, record: UsageCacheRecord) -> None:
        """Store `record`, applying the stale-write guard (§6.4).

        The snapshot (windows, `fetched_at`, `source_version`,
        `account_hash`, `plan`, `spend_limit_reached`) is replaced only
        when the incoming `fetched_at` is at least as recent as the
        stored one; attempt metadata (`last_attempt_at`, `last_status`,
        `last_reason`, `next_retry_at`) always updates — so a stale write
        can never overwrite a fresher snapshot. A fingerprint change
        (`account_hash`) treats the stored snapshot as empty: the incoming
        record replaces it unconditionally, so a stale cross-account
        write can never resurrect another account's snapshot. The
        `enabled` flag is only ever changed by `set_enabled`; `write`
        preserves the stored value when a record exists.
        """
        existing = self.read(provider)
        if existing is not None:
            if (
                existing.account_hash is not None
                and record.account_hash is not None
                and existing.account_hash != record.account_hash
            ):
                merged = record
            elif (
                record.fetched_at is not None
                and existing.fetched_at is not None
                and record.fetched_at < existing.fetched_at
            ):
                merged = dataclasses.replace(
                    existing,
                    last_attempt_at=record.last_attempt_at,
                    last_status=record.last_status,
                    last_reason=record.last_reason,
                    next_retry_at=record.next_retry_at,
                )
            else:
                merged = dataclasses.replace(record, enabled=existing.enabled)
            self._write_raw(provider, merged)
            return
        self._write_raw(provider, record)

    def set_enabled(self, provider: str, enabled: bool) -> None:
        """The opt-in write path (D3): creates the record with `enabled`
        when none exists, preserving existing windows/`fetched_at`
        otherwise. This is the only writer of the `enabled` flag."""
        existing = self.read(provider)
        if existing is None:
            record = UsageCacheRecord(enabled=enabled)
        else:
            record = dataclasses.replace(existing, enabled=enabled)
        self._write_raw(provider, record)

    def _write_raw(self, provider: str, record: UsageCacheRecord) -> None:
        """Atomic write: temp file (mode 600 before rename) + `os.replace`.
        `~/.jev/` is created if missing (mode 700). A cache write never
        refuses; a failed write is logged and the old file stands."""
        path = self._path(provider)
        existed = path.parent.is_dir()
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not existed:
                os.chmod(path.parent, 0o700)
        except OSError as exc:
            log.warning("usage cache: could not create %s: %s", path.parent, exc)
            return
        payload = {
            "schema_version": 1,
            "enabled": record.enabled,
            "fetched_at": iso_z(record.fetched_at),
            "last_attempt_at": iso_z(record.last_attempt_at),
            "last_status": record.last_status,
            "last_reason": record.last_reason,
            "next_retry_at": iso_z(record.next_retry_at),
            "source_version": record.source_version,
            "account_hash": record.account_hash,
            "plan": record.plan,
            "spend_limit_reached": record.spend_limit_reached,
            "windows": [
                {
                    "kind": w.kind,
                    "used_percent": w.used_percent,
                    "resets_at": iso_z(w.resets_at),
                }
                for w in record.windows
            ],
        }
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".usage-cache-{provider}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            log.warning("usage cache: could not write %s: %s", path, exc)
