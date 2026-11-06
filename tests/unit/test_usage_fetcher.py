"""Usage fetcher (issue #396, §6.4): opt-in, cadence floor, single-flight,
401 counting + stop/re-arm, `next_retry_at`, shutdown, and the display
state (D20 per-window max-age).

Fake clock + fake adapters + temp home (a Codex `tokens` login is planted
in `~/.codex/auth.json`); no network, no real CLI, no wall-clock sleeps.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jev_agent.config.schema import JevConfig, UsageConfig
from jev_agent.core.usage import (
    REASON_UNAUTHORIZED,
    UsageSnapshot,
    UsageUnavailable,
    UsageWindow,
    derive_display_state,
)
from jev_agent.core.usage.cache import UsageCache, UsageCacheRecord
from jev_agent.core.usage.fetcher import UsageFetcher

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
RESETS = T0 + timedelta(days=20)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now += timedelta(**kwargs)


class _FakeAdapter:
    """A usage source: returns `outcome` per call (a list is consumed in
    order); `block` holds the fetch open until `release()` (single-flight
    + shutdown). `shutdown()` releases it and records the call."""

    def __init__(self, outcome: Any, *, block: bool = False) -> None:
        self._outcomes = outcome if isinstance(outcome, list) else [outcome]
        self.calls = 0
        self.shutdown_calls = 0
        self._block = block
        self._release = threading.Event()
        self.started = threading.Event()

    def fetch(self) -> Any:
        self.calls += 1
        self.started.set()
        if self._block:
            self._release.wait(timeout=30)
        outcome = self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def release(self) -> None:
        self._release.set()

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self._release.set()


def _snapshot(**overrides) -> UsageSnapshot:
    snapshot = UsageSnapshot(
        provider="codex",
        windows=[UsageWindow(kind="monthly", used_percent=48.0, resets_at=RESETS)],
        plan="free",
        fetched_at=T0,
        source_version="cli/0.167.0 parser/1",
        account_hash="aaa",
        spend_limit_reached=False,
    )
    if overrides:
        import dataclasses

        snapshot = dataclasses.replace(snapshot, **overrides)
    return snapshot


def _unavailable(reason: str, **overrides) -> UsageUnavailable:
    return UsageUnavailable(
        provider="codex", reason=reason, fetched_at=None, last_good=None, **overrides
    )


def _codex_login(home: Path) -> None:
    auth = home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(
        json.dumps(
            {"tokens": {"access_token": "fake-access", "refresh_token": "fake-refresh"}}
        ),
        encoding="utf-8",
    )


def _fetcher(
    tmp_path: Path,
    adapter: Any,
    *,
    config: JevConfig | None = None,
) -> tuple[UsageFetcher, _Clock, Path, JevConfig]:
    home = tmp_path / "home"
    _codex_login(home)
    clock = _Clock(T0)
    fetcher = UsageFetcher(
        home=home, env={}, adapters={"codex": adapter}, clock=clock
    )
    return fetcher, clock, home, config or JevConfig()


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _wait_done(fetcher: UsageFetcher, adapter: _FakeAdapter, calls: int) -> bool:
    """Wait for the Nth fetch to be called AND its thread to finish (the
    record is written). Waiting for the call alone races the next
    refresh into `already_running`."""
    return _wait_until(
        lambda: adapter.calls == calls and not fetcher.is_running("codex")
    )


# --- opt-in, floor, single-flight ----------------------------------------------


def test_no_fetch_before_opt_in(tmp_path: Path) -> None:
    adapter = _FakeAdapter(_snapshot())
    fetcher, _, _, config = _fetcher(tmp_path, adapter)
    assert fetcher.maybe_fetch("codex", config) is False
    assert adapter.calls == 0
    # A manual refresh is the first click: it opts in AND starts a fetch.
    result = fetcher.refresh("codex", config, manual=True)
    assert result.outcome == "started"
    assert _wait_done(fetcher, adapter, 1)
    assert fetcher.cache.read("codex").enabled is True  # type: ignore[union-attr]


def test_maybe_fetch_starts_when_enabled(tmp_path: Path) -> None:
    adapter = _FakeAdapter(_snapshot())
    fetcher, _, _, config = _fetcher(tmp_path, adapter)
    fetcher.cache.set_enabled("codex", True)
    assert fetcher.maybe_fetch("codex", config) is True
    assert _wait_done(fetcher, adapter, 1)


def test_single_flight_second_refresh_is_already_running(tmp_path: Path) -> None:
    adapter = _FakeAdapter(_snapshot(), block=True)
    fetcher, _, _, config = _fetcher(tmp_path, adapter)
    assert fetcher.refresh("codex", config, manual=True).outcome == "started"
    assert adapter.started.wait(timeout=5)
    assert fetcher.is_running("codex") is True
    assert fetcher.refresh("codex", config, manual=True).outcome == "already_running"
    assert fetcher.maybe_fetch("codex", config) is False
    adapter.release()
    assert _wait_until(lambda: not fetcher.is_running("codex"))


def test_minimum_interval_codex_5_min_from_last_attempt(tmp_path: Path) -> None:
    adapter = _FakeAdapter(_snapshot())
    fetcher, clock, _, config = _fetcher(tmp_path, adapter)
    assert fetcher.refresh("codex", config, manual=True).outcome == "started"
    assert _wait_done(fetcher, adapter, 1)
    # Inside the 5-minute floor a manual refresh is too soon, with the wait.
    result = fetcher.refresh("codex", config, manual=True)
    assert result.outcome == "too_soon"
    assert result.wait_seconds is not None
    assert 240 < result.wait_seconds <= 300
    clock.advance(minutes=5, seconds=1)
    assert fetcher.refresh("codex", config, manual=True).outcome == "started"
    assert _wait_done(fetcher, adapter, 2)


def test_next_retry_at_honoured_including_after_restart(tmp_path: Path) -> None:
    """A `Retry-After` from the adapter is honoured, and it survives a
    restart (a new fetcher over the same cache)."""
    retry_at = T0 + timedelta(minutes=10)
    adapter = _FakeAdapter(_unavailable("rate_limited", next_retry_at=retry_at))
    fetcher, clock, home, config = _fetcher(tmp_path, adapter)
    assert fetcher.refresh("codex", config, manual=True).outcome == "started"
    assert _wait_done(fetcher, adapter, 1)
    assert fetcher.cache.read("codex").next_retry_at == retry_at  # type: ignore[union-attr]
    # Inside the retry window a manual refresh is too soon (the floor
    # alone would have passed after 5 min, but next_retry_at is later).
    clock.advance(minutes=6)
    result = fetcher.refresh("codex", config, manual=True)
    assert result.outcome == "too_soon"
    assert result.wait_seconds is not None
    assert 230 < result.wait_seconds <= 240  # ~4 min left of the 10
    # A new fetcher (a restart) honours the same retry time.
    restarted = UsageFetcher(home=home, env={}, adapters={"codex": adapter}, clock=clock)
    result = restarted.refresh("codex", config, manual=True)
    assert result.outcome == "too_soon"
    clock.advance(minutes=5)
    assert restarted.refresh("codex", config, manual=True).outcome == "started"


def test_kill_switch_disabled(tmp_path: Path) -> None:
    adapter = _FakeAdapter(_snapshot())
    fetcher, _, _, config = _fetcher(tmp_path, adapter)
    off = config.model_copy(update={"usage": UsageConfig(codex=False)})
    assert fetcher.refresh("codex", off, manual=True).outcome == "disabled"
    assert fetcher.maybe_fetch("codex", off) is False
    assert adapter.calls == 0


def test_no_login_when_no_usage_source(tmp_path: Path) -> None:
    adapter = _FakeAdapter(_snapshot())
    home = tmp_path / "home"  # no ~/.codex/auth.json planted here
    clock = _Clock(T0)
    fetcher = UsageFetcher(home=home, env={}, adapters={"codex": adapter}, clock=clock)
    result = fetcher.refresh("codex", JevConfig(), manual=True)
    assert result.outcome == "no_login"
    assert adapter.calls == 0
    # ... and the no-login click does not spuriously opt in.
    assert fetcher.cache.read("codex") is None


def test_unregistered_provider_raises_and_maybe_fetch_is_false(tmp_path: Path) -> None:
    adapter = _FakeAdapter(_snapshot())
    fetcher, _, _, config = _fetcher(tmp_path, adapter)
    with pytest.raises(ValueError):
        fetcher.refresh("claude_code", config, manual=True)
    assert fetcher.maybe_fetch("claude_code", config) is False


def test_failed_fetch_never_overwrites_a_good_entry(tmp_path: Path) -> None:
    adapter = _FakeAdapter(
        [_snapshot(), _unavailable("app_server_failed")]
    )
    fetcher, clock, _, config = _fetcher(tmp_path, adapter)
    assert fetcher.refresh("codex", config, manual=True).outcome == "started"
    assert _wait_done(fetcher, adapter, 1)
    good = fetcher.cache.read("codex")
    assert good is not None and good.last_status == "ok"
    clock.advance(minutes=6)
    assert fetcher.refresh("codex", config, manual=True).outcome == "started"
    assert _wait_done(fetcher, adapter, 2)
    after = fetcher.cache.read("codex")
    assert after is not None
    assert after.last_status == "app_server_failed"  # the attempt metadata updates
    assert after.fetched_at == good.fetched_at  # ... but the good snapshot stands
    assert [(w.kind, w.used_percent) for w in after.windows] == [("monthly", 48.0)]


def test_success_clears_next_retry_at(tmp_path: Path) -> None:
    retry_at = T0 + timedelta(minutes=10)
    adapter = _FakeAdapter(
        [_unavailable("rate_limited", next_retry_at=retry_at), _snapshot()]
    )
    fetcher, clock, _, config = _fetcher(tmp_path, adapter)
    assert fetcher.refresh("codex", config, manual=True).outcome == "started"
    assert _wait_done(fetcher, adapter, 1)
    clock.advance(minutes=11)
    assert fetcher.refresh("codex", config, manual=True).outcome == "started"
    assert _wait_done(fetcher, adapter, 2)
    record = fetcher.cache.read("codex")
    assert record is not None
    assert record.last_status == "ok"
    assert record.next_retry_at is None


def test_raising_adapter_records_unexpected_response(tmp_path: Path) -> None:
    adapter = _FakeAdapter(RuntimeError("boom"))
    fetcher, _, _, config = _fetcher(tmp_path, adapter)
    assert fetcher.refresh("codex", config, manual=True).outcome == "started"
    assert _wait_done(fetcher, adapter, 1)
    assert _wait_until(
        lambda: (fetcher.cache.read("codex") or UsageCacheRecord()).last_status
        == "unexpected_response"
    )


def test_shutdown_joins_within_bound_and_terminates_app_server(tmp_path: Path) -> None:
    adapter = _FakeAdapter(_snapshot(), block=True)
    fetcher, _, _, config = _fetcher(tmp_path, adapter)
    assert fetcher.refresh("codex", config, manual=True).outcome == "started"
    assert adapter.started.wait(timeout=5)
    started = time.monotonic()
    fetcher.shutdown(timeout=5.0)
    assert time.monotonic() - started < 5.0
    assert adapter.shutdown_calls == 1  # the live app-server was terminated
    assert fetcher.is_running("codex") is False


# --- 401 counting, stop, re-arm (D7) --------------------------------------------


def test_401_first_allows_one_retry_second_consecutive_stops(tmp_path: Path) -> None:
    adapter = _FakeAdapter(
        [_unavailable(REASON_UNAUTHORIZED), _unavailable(REASON_UNAUTHORIZED)]
    )
    fetcher, clock, _, config = _fetcher(tmp_path, adapter)
    assert fetcher.refresh("codex", config, manual=True).outcome == "started"
    assert _wait_done(fetcher, adapter, 1)
    clock.advance(minutes=6)
    # Exactly one retry after the floor — a render trigger, not a manual
    # refresh (a manual refresh would re-arm and reset the count). The
    # second consecutive 401 stops.
    assert fetcher.maybe_fetch("codex", config) is True
    assert _wait_done(fetcher, adapter, 2)
    # A non-manual trigger now reports the stop (no fetch).
    assert fetcher.maybe_fetch("codex", config) is False
    assert adapter.calls == 2
    assert fetcher.refresh("codex", config).outcome == "auth_stopped"


def test_401_stop_rearmed_by_manual_refresh(tmp_path: Path) -> None:
    adapter = _FakeAdapter(
        [
            _unavailable(REASON_UNAUTHORIZED),
            _unavailable(REASON_UNAUTHORIZED),
            _snapshot(),
        ]
    )
    fetcher, clock, _, config = _fetcher(tmp_path, adapter)
    fetcher.refresh("codex", config, manual=True)
    assert _wait_done(fetcher, adapter, 1)
    clock.advance(minutes=6)
    assert fetcher.maybe_fetch("codex", config) is True
    assert _wait_done(fetcher, adapter, 2)
    assert fetcher.refresh("codex", config).outcome == "auth_stopped"
    # Manual Refresh overrides the stop (but not the floor).
    clock.advance(minutes=6)
    assert fetcher.refresh("codex", config, manual=True).outcome == "started"
    assert _wait_done(fetcher, adapter, 3)
    record = fetcher.cache.read("codex")
    assert record is not None and record.last_status == "ok"


def test_401_stop_rearmed_by_detection_change(tmp_path: Path) -> None:
    """A changed login (the `tokens` login disappears, kind `cli_login` ->
    `none`) re-arms the provider: the stop clears on the next trigger,
    and a fresh login fetches again."""
    adapter = _FakeAdapter(
        [
            _unavailable(REASON_UNAUTHORIZED),
            _unavailable(REASON_UNAUTHORIZED),
            _snapshot(),
        ]
    )
    fetcher, clock, home, config = _fetcher(tmp_path, adapter)
    fetcher.refresh("codex", config, manual=True)
    assert _wait_done(fetcher, adapter, 1)
    clock.advance(minutes=6)
    assert fetcher.maybe_fetch("codex", config) is True
    assert _wait_done(fetcher, adapter, 2)
    assert fetcher.refresh("codex", config).outcome == "auth_stopped"
    # The login disappears: the stop clears on the next trigger, even
    # though that trigger itself has nothing to fetch.
    (home / ".codex" / "auth.json").unlink()
    assert fetcher.maybe_fetch("codex", config) is False  # no_login, but re-armed
    assert adapter.calls == 2
    # A fresh login (same path, same kind) now fetches again.
    _codex_login(home)
    clock.advance(minutes=6)
    assert fetcher.maybe_fetch("codex", config) is True
    assert _wait_done(fetcher, adapter, 3)


def test_401_stop_rearmed_by_account_hash_change(tmp_path: Path) -> None:
    """A changed Codex `account_hash` re-arms the provider even when the
    login file is unchanged (a different account signed in)."""
    adapter = _FakeAdapter(
        [
            _unavailable(REASON_UNAUTHORIZED),
            _unavailable(REASON_UNAUTHORIZED),
            _snapshot(account_hash="bbb"),
        ]
    )
    fetcher, clock, home, config = _fetcher(tmp_path, adapter)
    fetcher.refresh("codex", config, manual=True)
    assert _wait_done(fetcher, adapter, 1)
    clock.advance(minutes=6)
    assert fetcher.maybe_fetch("codex", config) is True
    assert _wait_done(fetcher, adapter, 2)
    assert fetcher.refresh("codex", config).outcome == "auth_stopped"
    # The cache record now belongs to another account: the stop clears.
    record = fetcher.cache.read("codex")
    assert record is not None
    fetcher.cache.write(
        "codex",
        UsageCacheRecord(
            enabled=True,
            fetched_at=record.fetched_at,
            last_attempt_at=record.last_attempt_at,
            last_status=record.last_status,
            account_hash="bbb",
            windows=list(record.windows),
        ),
    )
    clock.advance(minutes=6)
    assert fetcher.maybe_fetch("codex", config) is True
    assert _wait_done(fetcher, adapter, 3)


def test_non_401_failure_resets_the_401_count(tmp_path: Path) -> None:
    adapter = _FakeAdapter(
        [
            _unavailable(REASON_UNAUTHORIZED),
            _unavailable("app_server_failed"),
            _unavailable(REASON_UNAUTHORIZED),
        ]
    )
    fetcher, clock, _, config = _fetcher(tmp_path, adapter)
    for expected_calls in (1, 2, 3):
        assert fetcher.refresh("codex", config, manual=True).outcome == "started"
        assert _wait_done(fetcher, adapter, expected_calls)
        clock.advance(minutes=6)
    # Two 401s separated by another failure never form a consecutive pair:
    # no stop, so the next non-manual trigger still fetches.
    assert fetcher.maybe_fetch("codex", config) is True
    assert _wait_done(fetcher, adapter, 4)
    assert _wait_until(lambda: not fetcher.is_running("codex"))


def test_new_fetcher_has_no_stop(tmp_path: Path) -> None:
    """The stop lives in process memory only: a new fetcher (a restart)
    has no stop to honour."""
    adapter = _FakeAdapter(
        [_unavailable(REASON_UNAUTHORIZED), _unavailable(REASON_UNAUTHORIZED)]
    )
    fetcher, clock, home, config = _fetcher(tmp_path, adapter)
    fetcher.refresh("codex", config, manual=True)
    assert _wait_done(fetcher, adapter, 1)
    clock.advance(minutes=6)
    assert fetcher.maybe_fetch("codex", config) is True
    assert _wait_done(fetcher, adapter, 2)
    assert fetcher.refresh("codex", config).outcome == "auth_stopped"
    restarted = UsageFetcher(home=home, env={}, adapters={"codex": adapter}, clock=clock)
    clock.advance(minutes=6)
    assert restarted.maybe_fetch("codex", config) is True


# --- display state (D20) -------------------------------------------------------


def _record_for_state(
    windows: list[UsageWindow], fetched_at: datetime, last_status: str | None = "ok"
) -> UsageCacheRecord:
    return UsageCacheRecord(
        enabled=True, fetched_at=fetched_at, last_status=last_status, windows=windows
    )


def test_derive_display_state_monthly_max_age() -> None:
    monthly = [UsageWindow(kind="monthly", used_percent=48.0, resets_at=RESETS)]
    # Monthly max-age is 24 h: 23 h old still shows, 25 h old is stale.
    assert (
        derive_display_state(
            record=_record_for_state(monthly, T0 - timedelta(hours=23)),
            fetch_running=False,
            now=T0,
            session_max_age=timedelta(minutes=60),
            weekly_max_age=timedelta(hours=24),
            monthly_max_age=timedelta(hours=24),
        )
        == "ready"
    )
    assert (
        derive_display_state(
            record=_record_for_state(monthly, T0 - timedelta(hours=25)),
            fetch_running=False,
            now=T0,
            session_max_age=timedelta(minutes=60),
            weekly_max_age=timedelta(hours=24),
            monthly_max_age=timedelta(hours=24),
        )
        == "stale"
    )


def test_derive_display_state_weekly_max_age() -> None:
    weekly = [UsageWindow(kind="weekly", used_percent=19.0, resets_at=RESETS)]
    assert (
        derive_display_state(
            record=_record_for_state(weekly, T0 - timedelta(hours=23)),
            fetch_running=False,
            now=T0,
            session_max_age=timedelta(minutes=60),
            weekly_max_age=timedelta(hours=24),
            monthly_max_age=timedelta(hours=24),
        )
        == "ready"
    )
    assert (
        derive_display_state(
            record=_record_for_state(weekly, T0 - timedelta(hours=25)),
            fetch_running=False,
            now=T0,
            session_max_age=timedelta(minutes=60),
            weekly_max_age=timedelta(hours=24),
            monthly_max_age=timedelta(hours=24),
        )
        == "stale"
    )


def test_derive_display_state_never_collecting_failed() -> None:
    kwargs: dict[str, Any] = {
        "now": T0,
        "session_max_age": timedelta(minutes=60),
        "weekly_max_age": timedelta(hours=24),
        "monthly_max_age": timedelta(hours=24),
    }
    assert derive_display_state(record=None, fetch_running=False, **kwargs) == "never"
    assert derive_display_state(record=None, fetch_running=True, **kwargs) == "collecting"
    failed = _record_for_state([], T0, last_status="app_server_failed")
    assert derive_display_state(record=failed, fetch_running=False, **kwargs) == "failed"
    assert derive_display_state(record=failed, fetch_running=True, **kwargs) == "collecting"
