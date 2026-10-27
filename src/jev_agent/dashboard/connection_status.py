"""Connection liveness: cached server reachability and the decision-model route.

Read from the audit log and the config, never live SSH — except the explicit
refresh POST, which runs one `ssh HOST true` per server through the runner it is
given. `ssh_runner` is the default runner (`ssh HOST true`); tests fake it on the
dashboard app module and the app passes it in explicitly.
"""

from __future__ import annotations

import json
import os
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jev_agent.config.schema import JevConfig, JevSettings
from jev_agent.config.servers_schema import ServerConfig
from jev_agent.core.executables import resolve_executable
from jev_agent.servers.audit import AuditEntry, AuditLog, audit_path
from jev_agent.servers.ssh import (
    CONNECT_TIMEOUT_S,
    SshRunner,
    SshTransport,
    control_dir,
    run_process,
)

# ---- connection status ---------------------------------------------------------------------


ssh_runner: SshRunner = run_process  # faked in tests; `ssh HOST true` otherwise


@dataclass(frozen=True)
class _ConnServer:
    """One configured server and its last-known reachability. Built from the config plus
    the audit log's `connect` entries; building it never opens an SSH connection."""

    name: str
    host: str
    user: str | None
    access: str  # the configured access tier: basic, medium or unrestricted
    state: str  # "reachable" | "unreachable" | "unknown"
    last_check: datetime | None


@dataclass(frozen=True)
class _ConnKey:
    """An API key env var the dispatcher route needs: its name and whether it is set,
    never the value."""

    name: str
    is_set: bool


@dataclass(frozen=True)
class _ConnLeg:
    title: str
    keys: tuple[_ConnKey, ...]
    note: str


@dataclass(frozen=True)
class _ConnRoute:
    """The Jev decision-model route the repo's config builds (mirrors
    `app.context.build_dispatcher`): native TypeSafe, then Jev through OpenRouter,
    then the rules fallback."""

    primary: str
    legs: tuple[_ConnLeg, ...]
    final: str
    warning: str | None


_CONN_AUDIT_TAIL_BYTES = 64 * 1024  # one backward read of the audit tail per snapshot


def _parse_connect_line(raw: bytes) -> tuple[str | None, int | None, datetime | None]:
    """(server, exit_code, checked_at) from one audit line, or Nones when the line is not
    a `connect` entry. `connect` entries are written by `jev doctor --connect` and by the
    dashboard's explicit refresh action."""
    try:
        entry = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, None, None
    if not isinstance(entry, dict) or entry.get("action") != "connect":
        return None, None, None
    server = entry.get("server")
    if not isinstance(server, str) or not server:
        return None, None, None
    exit_code = entry.get("exit_code")
    checked_at: datetime | None = None
    when = entry.get("time")
    if isinstance(when, str):
        try:
            checked_at = datetime.fromisoformat(when)
        except ValueError:
            checked_at = None
    return server, exit_code if isinstance(exit_code, int) else None, checked_at


def _last_connect_checks(
    state_dir: Path, names: Collection[str]
) -> dict[str, tuple[int | None, datetime | None]]:
    """The last `connect` audit entry per server: exit code and check time.

    Reads the audit file backwards in 64KB chunks and stops once every configured
    server has an entry (or the file is exhausted), so a big audit log never slows the
    panel down. A GET never opens an SSH connection; this is the cached state.
    """
    want = set(names)
    if not want:
        return {}
    path = audit_path(state_dir)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return {}
    found: dict[str, tuple[int | None, datetime | None]] = {}
    with open(path, "rb") as handle:
        offset = size
        head = b""
        while offset > 0 and len(found) < len(want):
            start = max(0, offset - _CONN_AUDIT_TAIL_BYTES)
            handle.seek(start)
            chunk = handle.read(offset - start) + head
            offset = start
            lines = chunk.split(b"\n")
            head = lines[0]  # a possibly partial first line, completed by the next chunk
            for raw in reversed(lines[1:]):
                server, exit_code, checked_at = _parse_connect_line(raw)
                if server is None or server not in want or server in found:
                    continue
                found[server] = (exit_code, checked_at)
        if head.strip() and len(found) < len(want):  # the file's very first line
            server, exit_code, checked_at = _parse_connect_line(head)
            if server is not None and server in want and server not in found:
                found[server] = (exit_code, checked_at)
    return found


def _connection_servers(config: JevConfig, state_dir: Path) -> list[_ConnServer]:
    """Every configured server with its cached reachability (audit log, never live SSH)."""
    last = _last_connect_checks(state_dir, list(config.servers))
    servers = []
    for name, server in config.servers.items():
        exit_code, checked_at = last.get(name, (None, None))
        state = (
            "unknown" if name not in last else ("reachable" if exit_code == 0 else "unreachable")
        )
        servers.append(
            _ConnServer(
                name=name,
                host=server.host,
                user=server.user,
                access=server.access,
                state=state,
                last_check=checked_at,
            )
        )
    return servers


def _conn_aggregate(servers: list[_ConnServer]) -> tuple[str, str]:
    """(slot state, label) for the sidebar: All connected / N unreachable / Not checked."""
    if not servers:
        return "ok", "Live"  # nothing to report; the slot keeps workstream D's default
    down = sum(1 for s in servers if s.state == "unreachable")
    if down:
        return "degraded", "1 unreachable" if down == 1 else f"{down} unreachable"
    unknown = sum(1 for s in servers if s.state == "unknown")
    if unknown:
        return "degraded", "1 not checked" if unknown == 1 else f"{unknown} not checked"
    return "ok", "All connected"


def _key_set(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def _dispatcher_route(config: JevConfig) -> _ConnRoute:
    """The effective decision-model route, mirroring `app.context.build_dispatcher`.

    `jev` is Jev (native TypeSafe, then OpenRouter) with the rules dispatcher as
    fallback; `rules` is rules alone. Key env var names are shown with set/unset;
    values never leave the process.
    """
    primary = config.dispatcher.primary
    native_env = JevSettings.for_native().api_key_env  # "TYPESAFE_API_KEY"
    openrouter_env = config.dispatcher.jev.api_key_env  # default "OPENROUTER_API_KEY"
    native = _ConnKey(native_env, _key_set(native_env))
    openrouter = _ConnKey(openrouter_env, _key_set(openrouter_env))
    if primary == "rules":
        return _ConnRoute(
            primary="rules",
            legs=(
                _ConnLeg(
                    "Rules dispatcher",
                    (),
                    "Deterministic answers from simple rules: no network calls, no keys.",
                ),
            ),
            final="Rules decide every decision.",
            warning=None,
        )
    jev_legs = (
        _ConnLeg("Jev \u00b7 native TypeSafe", (native,), "TypeSafe's Jev API answers first."),
        _ConnLeg(
            "Jev \u00b7 OpenRouter",
            (openrouter,),
            "When the native leg cannot serve a request, the same Jev model answers "
            "through OpenRouter.",
        ),
    )
    if not native.is_set and not openrouter.is_set:
        outcome = "every decision uses the rules dispatcher."
        warning: str | None = (
            f"{native_env} and {openrouter_env} are not set, so Jev cannot be reached; {outcome}"
        )
    elif not native.is_set:
        warning = (
            f"{native_env} is not set, so every Jev decision goes through OpenRouter "
            f"({openrouter_env}) without the native TypeSafe leg."
        )
    elif not openrouter.is_set:
        warning = (
            f"{openrouter_env} is not set, so there is no failover if the native TypeSafe "
            f"leg ({native_env}) cannot serve a decision."
        )
    else:
        warning = None
    return _ConnRoute(
        primary="jev",
        legs=jev_legs,
        final="Rules dispatcher: the deterministic fallback when Jev cannot answer.",
        warning=warning,
    )


def _check_one_server(name: str, server: ServerConfig, state_dir: Path, runner: SshRunner) -> str:
    """One `ssh HOST true`, like `jev doctor --connect`. The audit line it writes is the
    cached state the panel and `/connections/live` read back.

    `runner` is passed in (not read from a module global) so the caller — the
    dashboard app, whose `ssh_runner` tests fake — controls the seam.
    """
    transport = SshTransport(server, sockets=control_dir(state_dir), runner=runner)
    result = transport.check(timeout_s=CONNECT_TIMEOUT_S + 5)
    entry = AuditEntry(
        run_id=None,
        role="dashboard",
        server=name,
        access=server.access,
        action="connect",
        command="true",
        allowed=True,
        exit_code=result.exit_code,
        duration_s=result.duration_s,
    )
    try:
        AuditLog(audit_path(state_dir)).write(entry)
    except OSError:
        return "unknown"  # the check ran, but its result could not be recorded
    return "reachable" if result.ok else "unreachable"


def _refresh_servers(config: JevConfig, state_dir: Path, runner: SshRunner) -> str:
    """Check every server. Only called from the explicit refresh POST: a GET never runs
    this, so reloading the page cannot repeat the SSH connections."""
    names = list(config.servers)
    if not names:
        return "no servers configured"
    if resolve_executable("ssh") is None:
        return "`ssh` not found on PATH; servers cannot be checked"
    states: dict[str, list[str]] = {"reachable": [], "unreachable": [], "unknown": []}
    for name in names:
        states[_check_one_server(name, config.servers[name], state_dir, runner)].append(name)
    parts = []
    if states["reachable"]:
        parts.append("reachable: " + ", ".join(states["reachable"]))
    if states["unreachable"]:
        parts.append("unreachable: " + ", ".join(states["unreachable"]))
    if states["unknown"]:
        parts.append("not checked: " + ", ".join(states["unknown"]))
    return "; ".join(parts) + " (recorded in the audit log)"
