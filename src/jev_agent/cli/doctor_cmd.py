"""`jev doctor`: check git state, config validity, the dispatcher, worker CLIs, API keys and the
agent tools (MCP servers, skills, plugins) each role is given.

The checks live in `jev_agent.app.doctor` (issue #92); this module renders the table and
keeps the test indirections (`which`, `ssh_runner`, `resolve_host`) that earlier tests
patch on this module.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jev_agent.app.doctor import Check, Status, _resolve_host
from jev_agent.app.doctor import run_checks as _run_checks
from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import cli_errors
from jev_agent.core.executables import resolve_executable
from jev_agent.servers.ssh import SshRunner, run_process

__all__ = ["Check", "Status", "doctor", "resolve_host", "run_checks", "ssh_runner", "which"]

console = Console()

# "note" is informational, like "ok", but points at something worth knowing.
_STYLE: dict[Status, str] = {"ok": "green", "note": "cyan", "warn": "yellow", "fail": "red"}

# Indirections so tests can fake PATH lookups without real CLIs installed, and never run ssh
# or touch DNS. `run_checks` passes them into `app.doctor.run_checks`.
which: Callable[[str], str | None] = resolve_executable
ssh_runner: SshRunner = run_process


def resolve_host(host: str) -> bool:
    """Whether `host` resolves. The DNS indirection tests patch on this module; the bounded
    implementation lives in `app.doctor._resolve_host` (one place, not two)."""
    return _resolve_host(host)


def run_checks(repo: Path, *, connect: bool = False) -> list[Check]:
    """`app.doctor.run_checks`, with this module's test indirections."""
    return _run_checks(repo, connect=connect, find=which, ssh=ssh_runner, resolve=resolve_host)


def doctor(
    repo: RepoOption = Path("."),
    connect: Annotated[
        bool,
        typer.Option("--connect", help="Also log in to each server (`ssh HOST true`)."),
    ] = False,
) -> None:
    """Check git state, config validity, worker CLIs, API keys, servers and the browser."""
    with cli_errors():
        checks = run_checks(repo, connect=connect)
    table = Table(title="jev doctor")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    for check in checks:
        style = _STYLE[check.status]
        table.add_row(
            escape(check.name), f"[{style}]{check.status}[/{style}]", escape(check.detail)
        )
    console.print(table)
    if any(check.status == "fail" for check in checks):
        raise typer.Exit(1)
