"""Small helpers for locating the target git repository (CLI adapter).

The neutral pieces live in `jev_agent.app.repo`; this module keeps the CLI-only parts:
`RepoOption` (the `--repo` flag), and the legacy (jao v0.1) layout error, which prints the
`error:` line and exits 1. Everything else is re-exported so earlier imports keep working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from jev_agent.app.repo import (
    LEGACY_CONFIG_FILENAME,
    LEGACY_MESSAGE,
    LEGACY_STATE_DIRNAME,
    legacy_layout,
)
from jev_agent.app.repo import (
    find_git_root as _find_git_root,
)

__all__ = [
    "LEGACY_CONFIG_FILENAME",
    "LEGACY_MESSAGE",
    "LEGACY_STATE_DIRNAME",
    "RepoOption",
    "find_git_root",
    "legacy_layout",
]

RepoOption = Annotated[Path, typer.Option("--repo", help="Path inside the target git repo.")]


def find_git_root(start: Path, *, allow_legacy: bool = False) -> Path | None:
    """Return the closest directory at or above `start` containing `.git`, or None.

    A repo still set up with jao v0.1 stops the command (exit 1) with a hint to migrate,
    unless `allow_legacy` is set (only `jev init --migrate` does that).
    """
    root = _find_git_root(start)
    if root is not None and not allow_legacy and legacy_layout(root):
        Console(stderr=True).print(f"[red]error:[/red] {escape(LEGACY_MESSAGE)}")
        raise typer.Exit(1)
    return root
