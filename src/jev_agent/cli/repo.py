"""Small helpers for locating the target git repository."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from jev_agent.config import CONFIG_FILENAME, STATE_DIRNAME

RepoOption = Annotated[Path, typer.Option("--repo", help="Path inside the target git repo.")]

# Names used by v0.1.0, when the tool was called jao. Only `jev init --migrate` touches them.
LEGACY_CONFIG_FILENAME = "jao.toml"
LEGACY_STATE_DIRNAME = ".jao"

LEGACY_MESSAGE = (
    f"This repo was set up with jao (v0.1). Run `jev init --migrate` (renames "
    f"{LEGACY_CONFIG_FILENAME} → {CONFIG_FILENAME} and {LEGACY_STATE_DIRNAME}/ → "
    f"{STATE_DIRNAME}/, updates .gitignore) or rename them yourself."
)


def legacy_layout(root: Path) -> bool:
    """True when `root` has only the v0.1 (jao) files: no jev.toml/.jev/, but jao.toml or .jao/."""
    if (root / CONFIG_FILENAME).exists() or (root / STATE_DIRNAME).exists():
        return False
    return (root / LEGACY_CONFIG_FILENAME).exists() or (root / LEGACY_STATE_DIRNAME).exists()


def find_git_root(start: Path, *, allow_legacy: bool = False) -> Path | None:
    """Return the closest directory at or above `start` containing `.git`, or None.

    A repo still set up with jao v0.1 stops the command (exit 1) with a hint to migrate, unless
    `allow_legacy` is set (only `jev init --migrate` does that).
    """
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():  # a directory, or a file in worktrees/submodules
            if not allow_legacy and legacy_layout(candidate):
                Console(stderr=True).print(f"[red]error:[/red] {escape(LEGACY_MESSAGE)}")
                raise typer.Exit(1)
            return candidate
    return None
