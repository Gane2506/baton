"""Locating the target git repository (the shared seam, issue #92).

Front-end agnostic: no printing, no prompts, no exits. `cli.repo` keeps the CLI adapter
(`RepoOption`, and the legacy-layout error that prints and exits 1) on top of this.
"""

from __future__ import annotations

from pathlib import Path

from jev_agent.config import CONFIG_FILENAME, STATE_DIRNAME

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


def find_git_root(start: Path) -> Path | None:
    """The closest directory at or above `start` containing `.git`, or None."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():  # a directory, or a file in worktrees/submodules
            return candidate
    return None
