"""Codex usage adapter (issue #396, §6.3): app-server spawn, handshake,
parser, and the §9.1 fake-spawner cases.

No network, no real CLI, no wall-clock sleeps: the app-server is a fake
`popen`, the timeout/grace are overridden small, and the clock is fixed.
The response fixtures follow the probe shape (§9.0 item 1, codex-cli
0.167.0): `result.rateLimits.primary` / `secondary`, `planType` inside
`rateLimits`, `accountId` / `ordinaryUsageAllowed` / `spendControlReached`
on `result`.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jev_agent.core.executables import resolve_executable
from jev_agent.core.usage import (
    REASON_APP_SERVER_FAILED,
    REASON_UNEXPECTED_RESPONSE,
    REASON_USAGE_BLOCKED,
    UsageSnapshot,
    UsageUnavailable,
)
from jev_agent.core.usage.codex import CodexUsageSource, _PACKAGE_VERSION

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
RESETS = 1795687699  # 2026-11-26 10:08:19 UTC (the probe's monthly reset)


def _window(duration: int, used: float, resets: int | None = RESETS) -> dict[str, Any]:
    return {"windowDurationMins": duration, "usedPercent": used, "resetsAt": resets}


def _result(
    *,
    primary: dict[str, Any] | None = None,
    secondary: dict[str, Any] | None = None,
    plan: str | None = "free",
    account: str | None = "acct-1",
    ordinary: bool | None = True,
    spend: bool | None = False,
) -> dict[str, Any]:
    return {
        "accountId": account,
        "ordinaryUsageAllowed": ordinary,
        "spendControlReached": spend,
        "rateLimits": {"primary": primary, "secondary": secondary, "planType": plan},
    }


def _lines(
    result: dict[str, Any] | None = None,
    *,
    ack: dict[str, Any] | None = None,
    answer_error: dict[str, Any] | None = None,
    notification: dict[str, Any] | None = None,
) -> list[str]:
    out: list[str] = []
    if notification is not None:
        out.append(json.dumps(notification) + "\n")
    if ack is not None:
        out.append(json.dumps(ack) + "\n")
    else:
        out.append(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n")
    if result is not None:
        out.append(json.dumps({"jsonrpc": "2.0", "id": 2, "result": result}) + "\n")
    elif answer_error is not None:
        out.append(
            json.dumps({"jsonrpc": "2.0", "id": 2, "error": answer_error}) + "\n"
        )
    return out


class _FakeStdout:
    """A canned stdout: returns the given text in order; `readline(n)`
    honours the size limit like a real text stream. When `block` is set
    the read waits until `unblock()` (the proc's terminate/kill)."""

    def __init__(self, text: str, *, block: bool = False) -> None:
        self._text = text
        self._pos = 0
        self._block = block
        self._unblock = threading.Event()

    def readline(self, size: int = -1) -> str:
        if self._block:
            self._unblock.wait(timeout=30)
            return ""
        if self._pos >= len(self._text):
            return ""
        # A real text stream's readline(n) stops at the first newline
        # (inclusive) or after n characters, whichever comes first.
        newline = self._text.find("\n", self._pos)
        end = len(self._text) if newline == -1 else newline + 1
        if size is not None and size >= 0:
            end = min(end, self._pos + size)
        out = self._text[self._pos : end]
        self._pos = end
        return out

    def unblock(self) -> None:
        self._unblock.set()


class _FakeProc:
    """A spawned app-server: canned stdout, stdin captured for assertion,
    terminate/kill tracked. `refuse_to_die` makes `wait()` raise
    TimeoutExpired until the proc is killed (a child ignoring SIGTERM)."""

    def __init__(self, stdout: _FakeStdout, *, refuse_to_die: bool = False) -> None:
        self.stdin = io.StringIO()
        self.stdout = stdout
        self.stderr = None
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self._refuse_to_die = refuse_to_die

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self.stdout.unblock()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.unblock()

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self._refuse_to_die and not self.killed:
            raise subprocess.TimeoutExpired("codex", timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _FakePopen:
    """Records every spawn (argv + kwargs) and hands out `_FakeProc`s."""

    def __init__(
        self, lines: list[str], *, block: bool = False, refuse_to_die: bool = False
    ) -> None:
        self._lines = lines
        self._block = block
        self._refuse_to_die = refuse_to_die
        self.calls: list[dict[str, Any]] = []
        self.procs: list[_FakeProc] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> _FakeProc:
        self.calls.append({"argv": argv, "kwargs": kwargs})
        proc = _FakeProc(
            _FakeStdout("".join(self._lines), block=self._block),
            refuse_to_die=self._refuse_to_die,
        )
        self.procs.append(proc)
        return proc


def _source(
    lines: list[str],
    *,
    block: bool = False,
    refuse_to_die: bool = False,
    which: Any = None,
    timeout_s: float = 5.0,
    kill_grace_s: float = 0.5,
    cli_version: str | None = "0.167.0",
) -> tuple[CodexUsageSource, _FakePopen]:
    popen = _FakePopen(lines, block=block, refuse_to_die=refuse_to_die)
    source = CodexUsageSource(
        popen=popen,
        which=which or (lambda cmd: "/fake/bin/codex"),
        clock=lambda: NOW,
        timeout_s=timeout_s,
        kill_grace_s=kill_grace_s,
        cli_version=cli_version,
    )
    return source, popen


def _fetch(lines: list[str], **kwargs: Any) -> tuple[Any, _FakePopen]:
    source, popen = _source(lines, **kwargs)
    return source.fetch(), popen


def _requests(proc: _FakeProc) -> list[dict[str, Any]]:
    return [json.loads(line) for line in proc.stdin.getvalue().splitlines()]


# --- happy paths ------------------------------------------------------------


def test_happy_path_two_windows_plan_verbatim() -> None:
    result = _result(
        primary=_window(300, 38, RESETS),
        secondary=_window(10080, 19, RESETS),
        plan="plus",
    )
    outcome, popen = _fetch(_lines(result))
    assert isinstance(outcome, UsageSnapshot)
    assert outcome.provider == "codex"
    assert [(w.kind, w.used_percent) for w in outcome.windows] == [
        ("session", 38.0),
        ("weekly", 19.0),
    ]
    assert outcome.windows[0].resets_at == datetime.fromtimestamp(RESETS, tz=UTC)
    assert outcome.plan == "plus"  # rendered verbatim, never normalised
    assert outcome.fetched_at == NOW
    assert outcome.source_version == "cli/0.167.0 parser/1"
    assert outcome.account_hash == hashlib.sha256(b"acct-1").hexdigest()
    assert outcome.spend_limit_reached is False
    # The exchange: initialize (id 1, clientInfo only) then
    # account/rateLimits/read (id 2); the "initialized" notification
    # travels between them.
    requests = _requests(popen.procs[0])
    assert requests[0]["method"] == "initialize"
    assert requests[0]["id"] == 1
    assert requests[0]["params"] == {
        "clientInfo": {"name": "jev", "title": "Baton", "version": _PACKAGE_VERSION}
    }
    assert requests[1] == {"method": "initialized", "params": {}}
    assert requests[2] == {"method": "account/rateLimits/read", "id": 2}


def test_probe_fixture_monthly_only() -> None:
    """The Mac probe's shape: primary 43200 (30 days), 48% used,
    secondary null, plan `free` inside `rateLimits`."""
    result = _result(primary=_window(43200, 48, 1795687699), secondary=None, plan="free")
    outcome, _ = _fetch(_lines(result))
    assert isinstance(outcome, UsageSnapshot)
    assert [(w.kind, w.used_percent) for w in outcome.windows] == [("monthly", 48.0)]
    assert outcome.windows[0].resets_at == datetime(2026, 11, 26, 10, 8, 19, tzinfo=UTC)
    assert outcome.plan == "free"


def test_notifications_and_non_json_lines_skipped() -> None:
    notification = {"jsonrpc": "2.0", "method": "account/changed", "params": {}}
    lines = _lines(
        _result(primary=_window(300, 10), secondary=None),
        notification=notification,
    )
    # A garbage line before the ack is skipped the same way.
    lines.insert(0, "not json at all\n")
    outcome, _ = _fetch(lines)
    assert isinstance(outcome, UsageSnapshot)
    assert outcome.windows[0].kind == "session"


def test_spawn_argv_env_and_cwd() -> None:
    outcome, popen = _fetch(
        _lines(_result(primary=_window(300, 10), secondary=None))
    )
    assert isinstance(outcome, UsageSnapshot)
    call = popen.calls[0]
    assert call["argv"] == ["/fake/bin/codex", "app-server", "--listen", "stdio://"]
    env = call["kwargs"]["env"]
    # Only the preserved names travel; no *_API_KEY / *_TOKEN ever leaks.
    assert set(env) <= {"HOME", "PATH", "TMPDIR", "LANG", "LC_ALL"}
    assert not any(k.endswith(("_API_KEY", "_TOKEN")) for k in env)
    assert "jev-codex-usage-" in call["kwargs"]["cwd"]  # a temp-dir cwd (§6.3)
    assert call["kwargs"]["text"] is True


def test_env_scrubbed_of_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-decoy")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-decoy")
    monkeypatch.setenv("SOME_TOKEN", "decoy")
    monkeypatch.setenv("HOME", "/fake/home")
    monkeypatch.setenv("PATH", "/usr/bin")
    outcome, popen = _fetch(
        _lines(_result(primary=_window(300, 10), secondary=None))
    )
    assert isinstance(outcome, UsageSnapshot)
    env = popen.calls[0]["kwargs"]["env"]
    assert "OPENAI_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "SOME_TOKEN" not in env
    assert env["HOME"] == "/fake/home"
    assert env["PATH"].split(os.pathsep)[0] == "/usr/bin"


def test_thin_launch_path_resolves_binary_and_augments_child_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: under a thin launch PATH the adapter still resolves `codex`
    via the platform search dirs, and the child's PATH is augmented with
    them so the wrapper's `env node` resolves."""
    home = tmp_path / "home"
    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True)
    codex = bindir / "codex"
    codex.write_text("#!/bin/sh\nexec true\n", encoding="utf-8")
    codex.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin")  # thin: no codex, no node
    source, popen = _source(
        _lines(_result(primary=_window(43200, 48, 1795687699), secondary=None)),
        which=resolve_executable,
    )
    outcome = source.fetch()
    assert isinstance(outcome, UsageSnapshot)
    assert popen.calls[0]["argv"][0] == str(codex)
    child_path = popen.calls[0]["kwargs"]["env"]["PATH"].split(os.pathsep)
    assert str(bindir) in child_path


def test_missing_cli_is_app_server_failed() -> None:
    outcome, popen = _fetch(
        _lines(_result(primary=_window(300, 10), secondary=None)),
        which=lambda cmd: None,
    )
    assert isinstance(outcome, UsageUnavailable)
    assert outcome.reason == REASON_APP_SERVER_FAILED
    assert popen.calls == []  # nothing spawned


# --- spawn failures -----------------------------------------------------------


def test_handshake_failure_no_ack() -> None:
    outcome, _ = _fetch([])  # EOF before any line
    assert isinstance(outcome, UsageUnavailable)
    assert outcome.reason == REASON_APP_SERVER_FAILED


def test_handshake_failure_error_ack() -> None:
    ack = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "bad"}}
    outcome, _ = _fetch(_lines(None, ack=ack))
    assert isinstance(outcome, UsageUnavailable)
    assert outcome.reason == REASON_APP_SERVER_FAILED


def test_auth_required_id2_error_is_app_server_failed() -> None:
    error = {
        "code": -32000,
        "message": "codex account authentication required to read rate limits",
    }
    outcome, _ = _fetch(_lines(None, answer_error=error))
    assert isinstance(outcome, UsageUnavailable)
    assert outcome.reason == REASON_APP_SERVER_FAILED


def test_spawn_timeout_terminates() -> None:
    source, popen = _source([], block=True, timeout_s=0.2, kill_grace_s=0.1)
    outcome = source.fetch()
    assert isinstance(outcome, UsageUnavailable)
    assert outcome.reason == REASON_APP_SERVER_FAILED
    proc = popen.procs[0]
    assert proc.terminated  # SIGTERM sent
    assert not proc.killed  # the child exited inside the grace


def test_spawn_timeout_kills_after_grace() -> None:
    source, popen = _source(
        [], block=True, refuse_to_die=True, timeout_s=0.2, kill_grace_s=0.1
    )
    outcome = source.fetch()
    assert isinstance(outcome, UsageUnavailable)
    assert outcome.reason == REASON_APP_SERVER_FAILED
    proc = popen.procs[0]
    assert proc.terminated
    assert proc.killed  # SIGKILL after the grace, the child ignored SIGTERM


def test_output_over_4mib_killed() -> None:
    big = "x" * (5 * 1024 * 1024) + "\n"
    outcome, popen = _fetch([big])
    assert isinstance(outcome, UsageUnavailable)
    assert outcome.reason == REASON_APP_SERVER_FAILED
    assert popen.procs[0].terminated


# --- parser: durations, windows, resets ---------------------------------------


def test_unknown_duration_dropped_known_shown() -> None:
    result = _result(
        primary=_window(300, 38, RESETS),
        secondary={"windowDurationMins": 99999, "usedPercent": 90, "resetsAt": RESETS},
    )
    outcome, _ = _fetch(_lines(result))
    assert isinstance(outcome, UsageSnapshot)
    assert [(w.kind, w.used_percent) for w in outcome.windows] == [("session", 38.0)]


def test_all_unknown_durations_is_unexpected_response() -> None:
    result = _result(
        primary={"windowDurationMins": 99999, "usedPercent": 90, "resetsAt": RESETS},
        secondary=None,
    )
    outcome, _ = _fetch(_lines(result))
    assert isinstance(outcome, UsageUnavailable)
    assert outcome.reason == REASON_UNEXPECTED_RESPONSE


def test_non_integral_duration_dropped() -> None:
    result = _result(
        primary=_window(300, 38, RESETS),
        secondary={"windowDurationMins": 10080.5, "usedPercent": 90, "resetsAt": RESETS},
    )
    outcome, _ = _fetch(_lines(result))
    assert isinstance(outcome, UsageSnapshot)
    assert [w.kind for w in outcome.windows] == ["session"]


def test_duplicate_duration_is_unexpected_response() -> None:
    result = _result(
        primary=_window(300, 38, RESETS),
        secondary=_window(300, 19, RESETS),
    )
    outcome, _ = _fetch(_lines(result))
    assert isinstance(outcome, UsageUnavailable)
    assert outcome.reason == REASON_UNEXPECTED_RESPONSE


def test_primary_null_is_unexpected_response() -> None:
    outcome, _ = _fetch(_lines(_result(primary=None, secondary=_window(10080, 19))))
    assert isinstance(outcome, UsageUnavailable)
    assert outcome.reason == REASON_UNEXPECTED_RESPONSE


def test_missing_or_mistyped_used_percent_is_unexpected_response() -> None:
    for bad in (None, "48", True):
        window = {"windowDurationMins": 43200, "resetsAt": RESETS}
        if bad is not None:
            window["usedPercent"] = bad
        outcome, _ = _fetch(_lines(_result(primary=window, secondary=None)))
        assert isinstance(outcome, UsageUnavailable)
        assert outcome.reason == REASON_UNEXPECTED_RESPONSE


def test_null_resets_at_is_no_reset_reported() -> None:
    outcome, _ = _fetch(
        _lines(_result(primary=_window(43200, 48, None), secondary=None))
    )
    assert isinstance(outcome, UsageSnapshot)
    assert outcome.windows[0].resets_at is None


def test_missing_resets_at_is_no_reset_reported() -> None:
    window = {"windowDurationMins": 43200, "usedPercent": 48}
    outcome, _ = _fetch(_lines(_result(primary=window, secondary=None)))
    assert isinstance(outcome, UsageSnapshot)
    assert outcome.windows[0].resets_at is None


def test_mistyped_resets_at_is_unexpected_response() -> None:
    window = {"windowDurationMins": 43200, "usedPercent": 48, "resetsAt": "soon"}
    outcome, _ = _fetch(_lines(_result(primary=window, secondary=None)))
    assert isinstance(outcome, UsageUnavailable)
    assert outcome.reason == REASON_UNEXPECTED_RESPONSE


def test_past_resets_at_is_kept() -> None:
    """A reset in the past renders as "resetting…" — the parser keeps the
    timestamp; the view decides the wording."""
    past = int(NOW.timestamp()) - 60
    outcome, _ = _fetch(_lines(_result(primary=_window(300, 90, past), secondary=None)))
    assert isinstance(outcome, UsageSnapshot)
    assert outcome.windows[0].resets_at == datetime.fromtimestamp(past, tz=UTC)


def test_ordinary_usage_allowed_false_is_usage_blocked() -> None:
    result = _result(primary=_window(300, 10), secondary=None, ordinary=False)
    outcome, _ = _fetch(_lines(result))
    assert isinstance(outcome, UsageUnavailable)
    assert outcome.reason == REASON_USAGE_BLOCKED


def test_spend_control_reached_true_is_snapshot_with_flag() -> None:
    result = _result(primary=_window(300, 10), secondary=None, spend=True)
    outcome, _ = _fetch(_lines(result))
    assert isinstance(outcome, UsageSnapshot)
    assert outcome.spend_limit_reached is True


def test_rate_limit_reached_type_needs_no_special_handling() -> None:
    result = _result(primary=_window(300, 100), secondary=None)
    result["rateLimitReachedType"] = "session"
    outcome, _ = _fetch(_lines(result))
    assert isinstance(outcome, UsageSnapshot)
    assert outcome.windows[0].used_percent == 100.0
    assert outcome.spend_limit_reached is False


def test_plan_type_missing_is_none_mistyped_is_unexpected() -> None:
    outcome, _ = _fetch(
        _lines(_result(primary=_window(300, 10), secondary=None, plan=None))
    )
    assert isinstance(outcome, UsageSnapshot)
    assert outcome.plan is None
    result = _result(primary=_window(300, 10), secondary=None, plan=42)
    outcome, _ = _fetch(_lines(result))
    assert isinstance(outcome, UsageUnavailable)
    assert outcome.reason == REASON_UNEXPECTED_RESPONSE


# --- D17: never a number from local session files ------------------------------


def test_session_files_never_drive_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decoy values planted in `~/.codex/sessions` must never appear in
    the snapshot — only the app-server's answer does (D17)."""
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "decoy.json").write_text(
        json.dumps({"usage": {"windowDurationMins": 300, "usedPercent": 99}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    outcome, _ = _fetch(
        _lines(_result(primary=_window(43200, 48, 1795687699), secondary=None))
    )
    assert isinstance(outcome, UsageSnapshot)
    assert [(w.kind, w.used_percent) for w in outcome.windows] == [("monthly", 48.0)]
