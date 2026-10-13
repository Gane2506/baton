"""CLI wiring: the shared contexts live in `jev_agent.app.context` now (issue #92).

This module keeps the CLI-only pieces: the `error:` line (`fail`, `print_error`), the
`cli_errors` wrapper that turns a `JevError` into that line and exit 1, and the `cli_context` /
`repo_context` wrappers built on them. Everything else is re-exported so imports written
before the app layer existed keep working; new code (and everything under `dashboard/`)
imports `jev_agent.app.context` / `jev_agent.app.errors` directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from jev_agent.app.context import (
    API_NO_CODER,
    API_NO_MODEL,
    FAKE_WORKERS_ENV,
    NO_WORKERS_MESSAGE,
    STATE_DB_FILENAME,
    AppContext,
    RepoContext,
    WorkerReadiness,
    api_role_problem,
    build_api_worker,
    build_cli_workers,
    build_dispatcher,
    build_registry,
    fake_workers_enabled,
    free_roles,
    load_free_catalog,
    open_context,
    open_repo,
    open_store,
    pipeline_settings,
    repo_config,
    repo_root,
    state_db_path,
    worker_readiness,
)
from jev_agent.app.errors import JevError

__all__ = [
    "API_NO_CODER",
    "API_NO_MODEL",
    "FAKE_WORKERS_ENV",
    "NO_WORKERS_MESSAGE",
    "STATE_DB_FILENAME",
    "AppContext",
    "CliContext",
    "CliError",
    "JevError",
    "RepoContext",
    "WorkerReadiness",
    "api_role_problem",
    "build_api_worker",
    "build_cli_workers",
    "build_dispatcher",
    "build_registry",
    "cli_context",
    "cli_errors",
    "fail",
    "fake_workers_enabled",
    "free_roles",
    "load_free_catalog",
    "open_cli_context",
    "open_context",
    "open_repo",
    "open_store",
    "pipeline_settings",
    "print_error",
    "repo_config",
    "repo_context",
    "repo_root",
    "state_db_path",
    "worker_readiness",
]

# The old names, for imports written before the app layer existed (issue #92).
CliError = JevError
CliContext = AppContext
open_cli_context = open_context

err_console = Console(stderr=True)


def print_error(message: str) -> None:
    """`error: <message>` on stderr. Messages quote config tables (`[trust]`, `[workers.api]`)
    and git output (`! [rejected]`): printed verbatim, never as Rich markup."""
    err_console.print(f"[red]error:[/red] {escape(message)}", highlight=False)


def fail(message: str) -> typer.Exit:
    print_error(message)
    return typer.Exit(1)


@contextmanager
def cli_errors() -> Iterator[None]:
    """Turn a `JevError` raised inside the block into the CLI's `error:` line and exit 1."""
    try:
        yield
    except JevError as exc:
        raise fail(str(exc)) from exc


@contextmanager
def repo_context(repo: Path, *, create: bool = True) -> Iterator[RepoContext]:
    """`open_repo` for CLI commands: errors exit 1 with one line on stderr. Read-only commands
    pass `create=False`."""
    with cli_errors(), open_repo(repo, create=create) as ctx:
        yield ctx


@contextmanager
def cli_context(repo: Path) -> Iterator[AppContext]:
    """`open_context` for commands that run the pipeline; errors exit 1 (stderr)."""
    with cli_errors(), open_context(repo) as ctx:
        yield ctx
