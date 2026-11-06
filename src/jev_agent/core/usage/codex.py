"""Codex usage via the local app-server JSON-RPC (issue #396, §6.3).

Spawns `codex app-server --listen stdio://`, speaks JSONL over stdio, and
parses `result.rateLimits` only. The CLI reads its own `~/.codex/auth.json`,
so this adapter never touches a token value. The success shape was observed
by the §9.0 probe on codex-cli 0.167.0 (records in
`docs/designs/cli-autoimport-usage/probe/`): primary `windowDurationMins`
43200 (= 30 days), `usedPercent` 48, `resetsAt` 1795687699 (unix seconds),
secondary null, `planType` "free" inside `rateLimits`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jev_agent import __version__ as _PACKAGE_VERSION
from jev_agent.core.executables import _platform_search_dirs, resolve_executable
from jev_agent.core.usage import (
    REASON_APP_SERVER_FAILED,
    REASON_DISABLED_BY_CONFIG,
    REASON_NO_LOGIN,
    REASON_NOT_APPLICABLE_API_KEY,
    REASON_NOT_CHECKED_YET,
    REASON_TIMEOUT,
    REASON_UNEXPECTED_RESPONSE,
    REASON_USAGE_BLOCKED,
    UsageSnapshot,
    UsageUnavailable,
    UsageWindow,
    Wording,
    _utcnow,
)
from jev_agent.core.provider_auth import cli_version

log = logging.getLogger(__name__)

PARSER_VERSION = 1

# windowDurationMins -> window kind. Derived from the duration, never from
# primary/secondary position (R1: 43200 is the monthly window).
_DURATION_TO_KIND: dict[int, str] = {300: "session", 10080: "weekly", 43200: "monthly"}

_MAX_OUTPUT_BYTES = 4 * 1024 * 1024  # 4 MiB cap on total buffered stdout
_TIMEOUT_S = 15.0  # whole spawn-to-answer
_KILL_GRACE_S = 5.0  # SIGTERM, then SIGKILL after this grace

# Preserved environment for the spawned app-server: HOME, PATH, TMPDIR, LANG,
# LC_ALL only. No `*_API_KEY` / `*_TOKEN` variable ever leaks into the child.
_PRESERVED_ENV = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL")

# Reason code -> (UI wording, next step). The §6.5 table is the
# specification of this mapping's contents; the template holds no reason
# wording of its own.
WORDING: Wording = {
    REASON_NOT_CHECKED_YET: ("Usage not checked yet", "Check usage"),
    REASON_NO_LOGIN: (
        "No CLI login found in `~/.codex/auth.json`",
        "Sign in in the CLI, or use the dashboard sign-in flow.",
    ),
    REASON_NOT_APPLICABLE_API_KEY: (
        "",
        "Billed per token — no subscription windows apply to an API key.",
    ),
    REASON_TIMEOUT: ("The usage check timed out", "Refresh usage"),
    REASON_UNEXPECTED_RESPONSE: (
        "Usage couldn't be read — the provider changed its response",
        "",
    ),
    REASON_APP_SERVER_FAILED: (
        "Couldn't ask the Codex CLI for usage",
        "Refresh usage — and check the CLI is installed",
    ),
    REASON_USAGE_BLOCKED: ("Usage is blocked for this account", "Check your ChatGPT plan."),
    REASON_DISABLED_BY_CONFIG: (
        "Usage is turned off for this provider",
        "Enable it in `jev.toml` and restart the dashboard",
    ),
}

PopenFn = Callable[..., Any]

# Logged-once-per-process diagnostics (§6.8): unknown durations, one log
# per distinct duration; unexpected-response diagnostics, one log total.
# Module-level sets (mutated in place — no `global` statements, P9).
_UNKNOWN_DURATIONS_LOGGED: set[float] = set()
_UNEXPECTED_LOGGED: set[str] = set()


class _FetchFailed(Exception):
    """Internal: the fetch failed with this reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CodexUsageSource:
    """Fetches Codex usage from the local app-server.

    Constructor seams (§8 testability): `popen` (the process spawner),
    `which` (binary resolution), `clock`, `timeout_s` / `kill_grace_s`
    (bounds, overridable for tests so no test sleeps 15 s), and
    `cli_version` (the CLI version string for `source_version`; when None
    it is resolved lazily once, via the cached `provider_auth.cli_version`).
    """

    def __init__(
        self,
        *,
        popen: PopenFn = subprocess.Popen,
        which: Callable[[str], str | None] = resolve_executable,
        clock: Callable[[], datetime] = _utcnow,
        timeout_s: float = _TIMEOUT_S,
        kill_grace_s: float = _KILL_GRACE_S,
        cli_version: str | None = None,
    ) -> None:
        self._popen = popen
        self._which = which
        self._clock = clock
        self._timeout_s = timeout_s
        self._kill_grace_s = kill_grace_s
        self._cli_version = cli_version
        self._lock = threading.Lock()
        self._current_proc: Any = None

    # -- adapter API ---------------------------------------------------------

    def fetch(self) -> UsageSnapshot | UsageUnavailable:
        """One app-server round-trip, or an unavailable outcome (never raises)."""
        binary = self._which("codex")
        if binary is None:
            return self._unavailable(REASON_APP_SERVER_FAILED)
        tmpdir = tempfile.mkdtemp(prefix="jev-codex-usage-")
        try:
            try:
                proc = self._popen(
                    [binary, "app-server", "--listen", "stdio://"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=tmpdir,
                    env=self._scrubbed_env(),
                    text=True,
                )
            except OSError:
                return self._unavailable(REASON_APP_SERVER_FAILED)
            with self._lock:
                self._current_proc = proc
            outcome: list[UsageSnapshot | UsageUnavailable] = []
            thread = threading.Thread(
                target=self._conversation, args=(proc, outcome), name="codex-usage-fetch",
                daemon=True,
            )
            thread.start()
            thread.join(timeout=self._timeout_s)
            if thread.is_alive():
                # Spawn timeout (§9.1): SIGTERM, then SIGKILL after the grace.
                self._terminate(proc)
                thread.join(timeout=self._kill_grace_s + 2.0)
                return self._unavailable(REASON_APP_SERVER_FAILED)
            if outcome:
                return outcome[0]
            return self._unavailable(REASON_APP_SERVER_FAILED)
        finally:
            with self._lock:
                self._current_proc = None
            shutil.rmtree(tmpdir, ignore_errors=True)

    def shutdown(self) -> None:
        """Terminate a live app-server (the fetcher calls this on shutdown)."""
        with self._lock:
            proc = self._current_proc
        if proc is not None:
            self._terminate(proc)

    # -- internals -------------------------------------------------------------

    def _unavailable(self, reason: str) -> UsageUnavailable:
        # The adapter never knows the last-good snapshot; the fetcher fills
        # it in from the cache when it records the failure.
        return UsageUnavailable(
            provider="codex", reason=reason, fetched_at=None, last_good=None
        )

    def _source_version(self) -> str:
        version = self._cli_version
        if version is None:
            version = cli_version("codex", which=self._which)
        if version is None:
            return "unknown"
        return f"cli/{version} parser/{PARSER_VERSION}"

    def _scrubbed_env(self) -> dict[str, str]:
        """Preserved environment only, with the launch PATH augmented by the
        platform search dirs (deduped) so the `codex` wrapper's `env node`
        resolves even under a thin launch PATH (R4, §6.3 probe note)."""
        env: dict[str, str] = {}
        for name in _PRESERVED_ENV:
            value = os.environ.get(name)
            if value:
                env[name] = value
        if "HOME" not in env:
            env["HOME"] = str(Path.home())
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        for directory in _platform_search_dirs():
            text = str(directory)
            if text not in parts:
                parts.append(text)
        env["PATH"] = os.pathsep.join(parts)
        return env

    def _terminate(self, proc: Any) -> None:
        """SIGTERM, then SIGKILL after the grace. Never raises."""
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=self._kill_grace_s)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait()
        except OSError:
            pass

    def _conversation(
        self, proc: Any, outcome: list[UsageSnapshot | UsageUnavailable]
    ) -> None:
        """The JSON-RPC exchange on a worker thread (the main thread owns
        the timeout). Terminates the app-server when done, however it ends."""
        try:
            outcome.append(self._exchange(proc))
        except _FetchFailed as exc:
            outcome.append(self._unavailable(exc.reason))
        except Exception:
            outcome.append(self._unavailable(REASON_APP_SERVER_FAILED))
        finally:
            self._terminate(proc)

    def _send(self, stdin: Any, message: dict[str, Any]) -> None:
        stdin.write(json.dumps(message) + "\n")
        stdin.flush()

    def _read_until_id(
        self, stdout: Any, wanted_id: int, byte_count: list[int]
    ) -> dict[str, Any] | None:
        """Read JSONL until the line with `"id" == wanted_id`. Notifications
        (lines with `method` and no matching id) and non-JSON lines are
        skipped. Returns None on EOF. Raises `_FetchFailed` when the 4 MiB
        output cap is exceeded."""
        while True:
            line = stdout.readline()
            if line == "":
                return None
            byte_count[0] += len(line.encode("utf-8"))
            if byte_count[0] > _MAX_OUTPUT_BYTES:
                raise _FetchFailed(REASON_APP_SERVER_FAILED)
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") == wanted_id:
                return message

    def _exchange(self, proc: Any) -> UsageSnapshot | UsageUnavailable:
        stdin, stdout = proc.stdin, proc.stdout
        if stdin is None or stdout is None:
            raise _FetchFailed(REASON_APP_SERVER_FAILED)
        byte_count = [0]
        try:
            self._send(
                stdin,
                {
                    "method": "initialize",
                    "id": 1,
                    # params carry only clientInfo, no protocolVersion; the
                    # version is the package version, fallback "unknown".
                    "params": {
                        "clientInfo": {
                            "name": "jev",
                            "title": "Baton",
                            "version": _PACKAGE_VERSION,
                        }
                    },
                },
            )
            ack = self._read_until_id(stdout, 1, byte_count)
            if ack is None or "error" in ack or "result" not in ack:
                raise _FetchFailed(REASON_APP_SERVER_FAILED)  # handshake failure
            self._send(stdin, {"method": "initialized", "params": {}})
            self._send(stdin, {"method": "account/rateLimits/read", "id": 2})
            answer = self._read_until_id(stdout, 2, byte_count)
            if answer is None:
                raise _FetchFailed(REASON_APP_SERVER_FAILED)
            if "error" in answer:
                # Includes the auth-required failure ("codex account
                # authentication required to read rate limits").
                raise _FetchFailed(REASON_APP_SERVER_FAILED)
            result = answer.get("result")
            if not isinstance(result, dict):
                self._log_unexpected("result-not-object", result)
                raise _FetchFailed(REASON_UNEXPECTED_RESPONSE)
            return self._parse(result)
        except (OSError, ValueError) as exc:
            # A broken pipe / vanished child mid-exchange is a spawn failure,
            # not a shape change.
            log.debug("codex usage exchange failed: %s", type(exc).__name__)
            raise _FetchFailed(REASON_APP_SERVER_FAILED) from exc

    def _parse(self, result: dict[str, Any]) -> UsageSnapshot:
        rate_limits = result.get("rateLimits")
        if not isinstance(rate_limits, dict):
            self._log_unexpected("rateLimits-missing", result)
            raise _FetchFailed(REASON_UNEXPECTED_RESPONSE)
        # `ordinaryUsageAllowed` false blocks usage for the account. The
        # probe record carries it on `result` (top level); a False in
        # either place blocks — a blocked account must never show usage.
        if (
            result.get("ordinaryUsageAllowed") is False
            or rate_limits.get("ordinaryUsageAllowed") is False
        ):
            raise _FetchFailed(REASON_USAGE_BLOCKED)
        primary = rate_limits.get("primary")
        if primary is None:
            self._log_unexpected("primary-null", result)
            raise _FetchFailed(REASON_UNEXPECTED_RESPONSE)
        windows: list[UsageWindow] = []
        seen_durations: set[int] = set()
        for slot in (primary, rate_limits.get("secondary")):
            if slot is None:
                continue  # secondary is often null
            if not isinstance(slot, dict):
                self._log_unexpected("window-not-object", result)
                raise _FetchFailed(REASON_UNEXPECTED_RESPONSE)
            duration = slot.get("windowDurationMins")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                self._log_unexpected("windowDurationMins-missing", result)
                raise _FetchFailed(REASON_UNEXPECTED_RESPONSE)
            if not float(duration).is_integer():
                # A non-integral duration is a duration we do not know.
                self._log_unknown_duration(duration)
                continue
            duration_int = int(duration)
            if duration_int in seen_durations:
                self._log_unexpected("duplicate-duration", result)
                raise _FetchFailed(REASON_UNEXPECTED_RESPONSE)
            seen_durations.add(duration_int)
            kind = _DURATION_TO_KIND.get(duration_int)
            if kind is None:
                # Unknown duration: drop the window, keep the known ones;
                # all-unknown (no windows left) is unexpected_response (R2).
                self._log_unknown_duration(duration_int)
                continue
            used = slot.get("usedPercent")
            if isinstance(used, bool) or not isinstance(used, (int, float)):
                self._log_unexpected("usedPercent-missing", result)
                raise _FetchFailed(REASON_UNEXPECTED_RESPONSE)
            resets = slot.get("resetsAt")
            if resets is None:
                resets_at = None  # "no reset reported"
            elif isinstance(resets, bool) or not isinstance(resets, (int, float)):
                self._log_unexpected("resetsAt-mistyped", result)
                raise _FetchFailed(REASON_UNEXPECTED_RESPONSE)
            else:
                try:
                    resets_at = datetime.fromtimestamp(resets, tz=UTC)
                except (OSError, OverflowError, ValueError):
                    self._log_unexpected("resetsAt-out-of-range", result)
                    raise _FetchFailed(REASON_UNEXPECTED_RESPONSE) from None
            windows.append(
                UsageWindow(
                    kind=kind, used_percent=float(used), resets_at=resets_at
                )
            )
        if not windows:
            self._log_unexpected("all-windows-unknown", result)
            raise _FetchFailed(REASON_UNEXPECTED_RESPONSE)
        plan = rate_limits.get("planType")
        if plan is None:
            plan_text = None
        elif isinstance(plan, str):
            plan_text = plan
        else:
            self._log_unexpected("planType-mistyped", result)
            raise _FetchFailed(REASON_UNEXPECTED_RESPONSE)
        account_hash = None
        account_id = result.get("accountId")
        if isinstance(account_id, str):
            account_hash = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
        spend_limit_reached = rate_limits.get("spendControlReached") is True
        return UsageSnapshot(
            provider="codex",
            windows=windows,
            plan=plan_text,
            fetched_at=self._clock(),
            source_version=self._source_version(),
            account_hash=account_hash,
            spend_limit_reached=spend_limit_reached,
        )

    def _log_unknown_duration(self, duration: int | float) -> None:
        """An unknown duration is logged once per dashboard process start,
        per distinct duration value (§6.3)."""
        key = float(duration)
        if key in _UNKNOWN_DURATIONS_LOGGED:
            return
        _UNKNOWN_DURATIONS_LOGGED.add(key)
        log.warning("codex usage: unknown windowDurationMins %s (window dropped)", duration)

    def _log_unexpected(self, detail: str, result: Any) -> None:
        """The redacted `unexpected_response` diagnostic (§6.8): which fields
        were present, which expected fields were missing or mistyped, and the
        response's top-level keys only — no values. Logged once per
        dashboard process start."""
        if _UNEXPECTED_LOGGED:
            return
        _UNEXPECTED_LOGGED.add(detail)
        keys = sorted(result.keys()) if isinstance(result, dict) else []
        log.warning(
            "codex usage: unexpected response (%s); top-level keys: %s",
            detail,
            ", ".join(keys) or "(none)",
        )
