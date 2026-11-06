"""The one background-fetch owner shared by both providers (issue #396, §6.4).

Starts a fetch when the page asks for one, enforces the cadence floor, owns
single-flight and the daemon threads. Config is never stored on the fetcher:
every fetch method takes the repo config as a parameter, and the kill switch
is read from `config.usage.<provider>` on every call. Errors in a fetch
thread never raise into the dashboard process — a failed fetch records a
failed state, nothing more.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jev_agent.core.provider_auth import LoginSource, resolve_credentials
from jev_agent.core.usage import (
    CADENCE_FLOOR,
    REASON_UNAUTHORIZED,
    REASON_UNEXPECTED_RESPONSE,
    ProviderUsageSource,
    RefreshResult,
    UsageSnapshot,
    UsageUnavailable,
    _utcnow,
)
from jev_agent.core.usage.cache import UsageCache, UsageCacheRecord

if TYPE_CHECKING:
    from jev_agent.config.schema import JevConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AuthStop:
    """A 401/403 stop, in process memory only: the login in effect when the
    stop was set, plus the Codex account hash at that time. A difference in
    either re-arms the provider."""

    login: LoginSource
    account_hash: str | None


class UsageFetcher:
    """One background-fetch owner shared by both providers.

    `adapters` maps provider -> source; a provider with no registered
    adapter is never fetched (in Phase A only `codex` is registered, so
    `claude_code` is never fetched until Phase B). `home` is the host home
    the cache lives under; tests pass a temp directory. `env` defaults to
    `os.environ`; tests pass a fake mapping.
    """

    def __init__(
        self,
        *,
        home: Path,
        env: Mapping[str, str] | None = None,
        adapters: Mapping[str, ProviderUsageSource],
        cache: UsageCache | None = None,
        clock: Any = _utcnow,
    ) -> None:
        self._home = home
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._adapters = dict(adapters)
        self._cache = cache or UsageCache(home=home, clock=clock)
        self._clock = clock
        self._lock = threading.Lock()
        self._running: dict[str, threading.Thread] = {}
        self._auth_401_count: dict[str, int] = {}
        self._auth_stopped: dict[str, _AuthStop] = {}

    @property
    def cache(self) -> UsageCache:
        """The host-level cache (the routes read it; tests pass a temp one)."""
        return self._cache

    @property
    def adapters(self) -> Mapping[str, ProviderUsageSource]:
        """The registered adapters, by provider."""
        return self._adapters

    # -- triggers --------------------------------------------------------------

    def maybe_fetch(self, provider: str, config: JevConfig) -> bool:
        """The render trigger: start a fetch only when enabled + kill switch
        on + a usage-measurable login is present + floor passed + not
        auth-stopped + not already running. Never waits; returns whether a
        fetch was started. A provider with no registered adapter is never
        fetched."""
        if provider not in self._adapters:
            return False
        outcome = self._start_if_allowed(provider, config, manual=False)
        return outcome.outcome == "started"

    def refresh(self, provider: str, config: JevConfig, *, manual: bool = False) -> RefreshResult:
        """The route entry point. `manual=True` overrides the auth stop but
        not the floor. Raises ValueError for an unregistered provider (the
        route 404s before calling this)."""
        if provider not in self._adapters:
            raise ValueError(f"no usage adapter registered for {provider!r}")
        return self._start_if_allowed(provider, config, manual=manual)

    def is_running(self, provider: str) -> bool:
        """Whether a fetch thread is alive for `provider`."""
        with self._lock:
            thread = self._running.get(provider)
        return thread is not None and thread.is_alive()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Join fetch threads within the bound; terminate any live
        app-server first so a stuck fetch unblocks promptly. Daemon
        threads are abandoned after the bound."""
        for adapter in self._adapters.values():
            shutdown = getattr(adapter, "shutdown", None)
            if callable(shutdown):
                with contextlib.suppress(Exception):
                    shutdown()
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = [t for t in self._running.values() if t.is_alive()]
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    # -- internals ---------------------------------------------------------------

    def _bare(self, provider: str, config: JevConfig) -> bool:
        return bool(provider == "claude_code" and config.workers.claude_code.bare)

    def _rearm_check(self, provider: str, config: JevConfig) -> None:
        """Clear a 401/403 stop when the detected login changed (kind or
        source path) or, for Codex, the cache record's account hash changed
        (D7). Runs on every `maybe_fetch` / `refresh` call."""
        stop = self._auth_stopped.get(provider)
        if stop is None:
            return
        resolution = resolve_credentials(
            provider, env=self._env, home=self._home, bare=self._bare(provider, config)
        )
        current = (resolution.effective.kind, resolution.effective.source_path)
        record = self._cache.read(provider)
        current_hash = record.account_hash if record else None
        if current != (stop.login.kind, stop.login.source_path) or (
            current_hash != stop.account_hash
        ):
            del self._auth_stopped[provider]
            self._auth_401_count.pop(provider, None)

    def _wait_seconds(
        self, provider: str, record: UsageCacheRecord | None, now: datetime
    ) -> float | None:
        """Seconds until the next attempt is allowed, or None when the floor
        and `next_retry_at` both pass. No attempt happens earlier than
        max(`last_attempt_at` + floor, `next_retry_at`)."""
        if record is None:
            return None
        candidates: list[float] = []
        if record.last_attempt_at is not None:
            candidates.append(
                (record.last_attempt_at + CADENCE_FLOOR[provider] - now).total_seconds()
            )
        if record.next_retry_at is not None:
            candidates.append((record.next_retry_at - now).total_seconds())
        if not candidates:
            return None
        wait = max(candidates)
        return wait if wait > 0 else None

    def _start_if_allowed(self, provider: str, config: JevConfig, *, manual: bool) -> RefreshResult:
        if not getattr(config.usage, provider):
            return RefreshResult("disabled")
        self._rearm_check(provider, config)
        if not manual and provider in self._auth_stopped:
            return RefreshResult("auth_stopped")
        if manual:
            # Manual Refresh clears the stop (D7); it does not clear the floor.
            self._auth_stopped.pop(provider, None)
            self._auth_401_count.pop(provider, None)
        record = self._cache.read(provider)
        if not manual and not (record and record.enabled):
            return RefreshResult("not_enabled")
        resolution = resolve_credentials(
            provider, env=self._env, home=self._home, bare=self._bare(provider, config)
        )
        if resolution.usage_source is None:
            return RefreshResult("no_login")
        if manual and not (record and record.enabled):
            # First click: opt in AND start a fetch immediately (D3).
            self._cache.set_enabled(provider, True)
            record = self._cache.read(provider)
        with self._lock:
            running = self._running.get(provider)
            if running is not None and running.is_alive():
                return RefreshResult("already_running")
        now = self._clock()
        wait = self._wait_seconds(provider, record, now)
        if wait is not None:
            return RefreshResult("too_soon", wait_seconds=wait)
        thread = threading.Thread(
            target=self._run_fetch,
            args=(provider, config),
            name=f"usage-fetch-{provider}",
            daemon=True,
        )
        with self._lock:
            self._running[provider] = thread
        thread.start()
        return RefreshResult("started")

    def _run_fetch(self, provider: str, config: JevConfig) -> None:
        """One fetch on a daemon thread: call the adapter, record the
        outcome in the cache, maintain the 401 count/stop. Never raises."""
        try:
            adapter = self._adapters[provider]
            try:
                result = adapter.fetch()
            except Exception as exc:
                # A raising adapter must not kill this thread or the
                # dashboard: record a failed state, nothing more. Log the
                # type only — never a token value or verbatim exception text.
                log.warning("usage fetch for %s raised %s", provider, type(exc).__name__)
                result = UsageUnavailable(
                    provider=provider,
                    reason=REASON_UNEXPECTED_RESPONSE,
                    fetched_at=None,
                    last_good=None,
                )
            now = self._clock()
            existing = self._cache.read(provider)
            enabled = True if existing is None else existing.enabled
            if isinstance(result, UsageSnapshot):
                self._auth_401_count.pop(provider, None)
                self._auth_stopped.pop(provider, None)
                record = UsageCacheRecord(
                    enabled=enabled,
                    fetched_at=result.fetched_at,
                    last_attempt_at=now,
                    last_status="ok",
                    last_reason=None,
                    next_retry_at=None,  # cleared on a successful fetch
                    source_version=result.source_version,
                    account_hash=result.account_hash,
                    plan=result.plan,
                    spend_limit_reached=result.spend_limit_reached,
                    windows=list(result.windows),
                )
            else:
                if result.reason == REASON_UNAUTHORIZED:
                    count = self._auth_401_count.get(provider, 0) + 1
                    if count >= 2:
                        # Second consecutive 401/403: stop until re-arm.
                        resolution = resolve_credentials(
                            provider,
                            env=self._env,
                            home=self._home,
                            bare=self._bare(provider, config),
                        )
                        self._auth_stopped[provider] = _AuthStop(
                            login=resolution.effective,
                            account_hash=existing.account_hash if existing else None,
                        )
                        self._auth_401_count.pop(provider, None)
                    else:
                        self._auth_401_count[provider] = count
                else:
                    self._auth_401_count.pop(provider, None)
                # A failed fetch never overwrites a good cache entry: the
                # last-good windows stay, attempt metadata updates.
                record = UsageCacheRecord(
                    enabled=enabled,
                    fetched_at=existing.fetched_at if existing else None,
                    last_attempt_at=now,
                    last_status=result.reason,
                    last_reason=result.reason,
                    next_retry_at=result.next_retry_at,
                    source_version=existing.source_version if existing else None,
                    account_hash=existing.account_hash if existing else None,
                    plan=existing.plan if existing else None,
                    spend_limit_reached=(existing.spend_limit_reached if existing else False),
                    windows=list(existing.windows) if existing else [],
                )
            self._cache.write(provider, record)
        finally:
            with self._lock:
                self._running.pop(provider, None)
