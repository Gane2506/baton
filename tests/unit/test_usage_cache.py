"""Usage cache (issue #396, §6.4/D5): atomic write, mode 600, stale-write
guard, fingerprint change, corrupt files, restart persistence.

Fake clock + temp home; no network, no real CLI.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jev_agent.core.usage import UsageWindow
from jev_agent.core.usage.cache import UsageCache, UsageCacheRecord

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
T1 = T0 - timedelta(hours=1)
T2 = T0 - timedelta(minutes=5)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _record(**overrides) -> UsageCacheRecord:
    record = UsageCacheRecord(
        enabled=True,
        fetched_at=T2,
        last_attempt_at=T2,
        last_status="ok",
        last_reason=None,
        next_retry_at=None,
        source_version="cli/0.167.0 parser/1",
        account_hash="aaa",
        plan="free",
        spend_limit_reached=False,
        windows=[UsageWindow(kind="monthly", used_percent=48.0, resets_at=T0)],
    )
    if overrides:
        import dataclasses

        record = dataclasses.replace(record, **overrides)
    return record


def _cache(tmp_path: Path) -> tuple[UsageCache, Path]:
    home = tmp_path / "home"
    return UsageCache(home=home, clock=_Clock(T0)), home


def test_missing_file_reads_none(tmp_path: Path) -> None:
    cache, _ = _cache(tmp_path)
    assert cache.read("codex") is None


def test_write_read_round_trip_verbatim(tmp_path: Path) -> None:
    cache, _ = _cache(tmp_path)
    record = _record(next_retry_at=T0 + timedelta(minutes=30), spend_limit_reached=True)
    cache.write("codex", record)
    assert cache.read("codex") == record


def test_file_mode_600_and_jev_dir_mode_700(tmp_path: Path) -> None:
    cache, home = _cache(tmp_path)
    cache.write("codex", _record())
    path = home / ".jev" / "usage-cache-codex.json"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert ((home / ".jev").stat().st_mode & 0o777) == 0o700


def test_interrupted_write_leaves_previous_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomic write: a failure at the rename leaves the old file readable
    (the temp file is cleaned up, never promoted)."""
    cache, home = _cache(tmp_path)
    first = _record()
    cache.write("codex", first)
    real_replace = os.replace

    def failing_replace(src, dst):
        raise OSError("simulated crash mid-rename")

    monkeypatch.setattr(os, "replace", failing_replace)
    cache.write("codex", _record(windows=[UsageWindow(kind="session", used_percent=1.0, resets_at=None)]))
    monkeypatch.setattr(os, "replace", real_replace)
    assert cache.read("codex") == first
    leftovers = [p for p in (home / ".jev").iterdir() if p.name.startswith(".usage-cache-")]
    assert leftovers == []


def test_account_hash_change_replaces_snapshot_unconditionally(tmp_path: Path) -> None:
    """A fingerprint change treats the stored snapshot as empty: a stale
    cross-account write can never resurrect another account's snapshot."""
    cache, _ = _cache(tmp_path)
    cache.write("codex", _record(account_hash="aaa", fetched_at=T2))
    stale_other_account = _record(
        account_hash="bbb",
        fetched_at=T1,  # staler — yet it must win across accounts
        windows=[UsageWindow(kind="session", used_percent=10.0, resets_at=None)],
        plan="plus",
    )
    cache.write("codex", stale_other_account)
    got = cache.read("codex")
    assert got is not None
    assert got.account_hash == "bbb"
    assert got.fetched_at == T1
    assert [(w.kind, w.used_percent) for w in got.windows] == [("session", 10.0)]
    assert got.plan == "plus"


def test_stale_write_guard_keeps_fresher_windows_updates_metadata(tmp_path: Path) -> None:
    """A write with an older `fetched_at` keeps the fresher snapshot but
    still updates the attempt metadata."""
    cache, _ = _cache(tmp_path)
    cache.write("codex", _record(fetched_at=T2, last_status="ok"))
    stale = _record(
        fetched_at=T1,
        last_attempt_at=T0,
        last_status="app_server_failed",
        last_reason="app_server_failed",
        windows=[UsageWindow(kind="session", used_percent=10.0, resets_at=None)],
    )
    cache.write("codex", stale)
    got = cache.read("codex")
    assert got is not None
    assert got.fetched_at == T2  # the fresher snapshot stands
    assert [(w.kind, w.used_percent) for w in got.windows] == [("monthly", 48.0)]
    assert got.last_attempt_at == T0  # ... but the attempt metadata updates
    assert got.last_status == "app_server_failed"
    assert got.last_reason == "app_server_failed"


def test_write_preserves_enabled_flag(tmp_path: Path) -> None:
    """`write` never flips the opt-in flag; only `set_enabled` does."""
    cache, _ = _cache(tmp_path)
    cache.set_enabled("codex", True)
    cache.write("codex", _record(enabled=False))
    assert cache.read("codex").enabled is True  # type: ignore[union-attr]
    cache.set_enabled("codex", False)
    assert cache.read("codex").enabled is False  # type: ignore[union-attr]


def test_set_enabled_creates_record_and_preserves_snapshot(tmp_path: Path) -> None:
    cache, _ = _cache(tmp_path)
    cache.set_enabled("codex", True)
    got = cache.read("codex")
    assert got is not None
    assert got.enabled is True
    assert got.windows == []
    cache.write("codex", _record())
    cache.set_enabled("codex", False)
    got = cache.read("codex")
    assert got is not None
    assert got.enabled is False
    assert got.fetched_at == T2  # the snapshot survives the opt-out
    assert [(w.kind, w.used_percent) for w in got.windows] == [("monthly", 48.0)]


def test_restart_preserves_record_verbatim_including_next_retry(tmp_path: Path) -> None:
    """`next_retry_at` survives a dashboard restart, so a restart never
    retries into a rate limit early."""
    cache, home = _cache(tmp_path)
    record = _record(next_retry_at=T0 + timedelta(minutes=30))
    cache.write("codex", record)
    restarted = UsageCache(home=home, clock=_Clock(T0 + timedelta(minutes=1)))
    assert restarted.read("codex") == record


def test_corrupt_file_reads_none(tmp_path: Path) -> None:
    cache, home = _cache(tmp_path)
    path = home / ".jev" / "usage-cache-codex.json"
    path.parent.mkdir(parents=True)
    for bad in (
        "not json at all",
        '{"schema_version": 2}',
        '{"schema_version": 1, "windows": "nope"}',
        '{"schema_version": 1, "windows": [{"kind": "monthly"}]}',  # no used_percent
        # Windows without a fetched_at is corrupt (the invariant).
        '{"schema_version": 1, "windows": [{"kind": "monthly", "used_percent": 48}]}',
        '{"schema_version": 1, "enabled": "yes"}',
        '["not", "a", "dict"]',
    ):
        path.write_text(bad, encoding="utf-8")
        assert cache.read("codex") is None, bad


def test_providers_are_separate_files(tmp_path: Path) -> None:
    cache, home = _cache(tmp_path)
    cache.write("codex", _record())
    cache.write("claude_code", _record(plan="pro"))
    assert cache.read("codex").plan == "free"  # type: ignore[union-attr]
    assert cache.read("claude_code").plan == "pro"  # type: ignore[union-attr]
    assert (home / ".jev" / "usage-cache-codex.json").exists()
    assert (home / ".jev" / "usage-cache-claude_code.json").exists()
