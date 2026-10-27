"""Finding CLIs without relying on the inherited PATH (deploy Phase 1, issue #333).

The dashboard is often launched from a bare environment (a launchd agent, a systemd
unit, `nohup` from a non-login shell), so Homebrew/nvm/Windows-npm locations that a
terminal would have on PATH are missing. `resolve_executable` is the single seam
every CLI lookup in the tree goes through: `shutil.which` first, then well-known
per-platform install locations. Dashboard startup calls
`augment_path_with_known_clis` once so spawned workers inherit the discovered
directories through `os.environ["PATH"]`.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from pathlib import Path

__all__ = [
    "KNOWN_CLIS",
    "augment_path_with_known_clis",
    "resolve_executable",
]

# CLIs the dashboard (and the workers it spawns) needs to find under a bare env.
KNOWN_CLIS = ("npx", "node", "claude", "codex", "gh", "ssh", "git")


def _nvm_bin_dirs(nvm_root: Path) -> list[Path]:
    """The `bin` dir of every installed Node version under an nvm root.

    Unix nvm layouts keep the binary at `<root>/vXX.YY.ZZ/bin/<name>`; nvm-windows
    keeps it at `<root>/vXX.YY.ZZ/<name>` (no `bin` level). Both are enumerated
    the same way; the caller picks the right suffix per platform.
    """
    if not nvm_root.is_dir():
        return []
    names: list[str] = []
    with contextlib.suppress(OSError):
        names = sorted(os.listdir(nvm_root), reverse=True)
    if sys.platform == "win32":
        return [nvm_root / name for name in names if (nvm_root / name).is_dir()]
    return [nvm_root / name / "bin" for name in names if (nvm_root / name).is_dir()]


def _platform_search_dirs() -> list[Path]:
    """Well-known install locations per platform, for when PATH is minimal."""
    home = Path.home()
    if sys.platform == "darwin":
        candidates = [
            Path("/opt/homebrew/bin"),  # Apple Silicon Homebrew
            Path("/usr/local/bin"),  # Intel Homebrew
            home / ".local" / "bin",  # pip --user, the claude installer
            home / ".npm-global" / "bin",  # npm global prefix (codex)
        ]
        # macOS nvm: ~/.nvm/versions/node/vXX.YY.ZZ/bin (the version dirs were
        # missing here until issue #333: the bare root dir itself holds no binary).
        return candidates + _nvm_bin_dirs(home / ".nvm" / "versions" / "node")
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", ""))
        localappdata = Path(os.environ.get("LOCALAPPDATA", ""))
        program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        nvm_roots = [appdata / "nvm", localappdata / "nvm"]
        candidates = [
            appdata / "npm",
            program_files / "nodejs",
            *nvm_roots,
        ]
        # nvm-windows version dirs: %APPDATA%\\nvm\\vXX.YY.ZZ (node.exe lives
        # directly in the version dir, not in a `bin` subdir).
        for root in nvm_roots:
            candidates.extend(_nvm_bin_dirs(root))
        return candidates
    # Linux and other Unix-likes
    candidates = [
        Path("/usr/local/bin"),
        Path("/opt/nodejs/bin"),
        home / ".local" / "bin",
        home / ".npm-global" / "bin",  # npm global prefix
    ]
    # nvm version dirs: ~/.nvm/versions/node/vXX.YY.ZZ/bin
    return candidates + _nvm_bin_dirs(home / ".nvm" / "versions" / "node")


def _candidate_names(name: str) -> list[str]:
    """Filenames to try for `name` in a fallback dir (Windows needs PATHEXT)."""
    if sys.platform != "win32":
        return [name]
    if Path(name).suffix:
        return [name]  # already has an extension: do not append another
    return [f"{name}{suffix}" for suffix in (".exe", ".cmd", ".bat", "")]


def resolve_executable(name: str) -> str | None:
    """Full path to `name`, or None when it cannot be found.

    Checks PATH first via `shutil.which`, then falls back to well-known
    per-platform install locations (Homebrew + nvm + per-user ~/.local/bin
    and ~/.npm-global/bin on macOS, npm/nodejs/nvm on Windows, /usr/local +
    nvm + per-user ~/.local/bin and ~/.npm-global/bin on Linux).
    """
    found = shutil.which(name)
    if found:
        return found
    for directory in _platform_search_dirs():
        for candidate_name in _candidate_names(name):
            candidate = directory / candidate_name
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
            except OSError:
                continue
    return None


def augment_path_with_known_clis(names: tuple[str, ...] = KNOWN_CLIS) -> list[str]:
    """Prepend the directories of `names` (found outside PATH) to `os.environ["PATH"]`.

    Call once at dashboard startup: spawned workers inherit the process env, so a
    `claude`/`codex`/`npx` found in a Homebrew or nvm dir is usable by workers too,
    not just by the dashboard's own lookups. Returns the directories added (empty
    when everything was already on PATH); idempotent — a second call adds nothing.
    """
    current = os.environ.get("PATH", "")
    present = set(current.split(os.pathsep)) if current else set()
    added: list[str] = []
    for name in names:
        found = resolve_executable(name)
        if found is None:
            continue
        directory = str(Path(found).parent)
        if directory in present:
            continue
        present.add(directory)
        added.append(directory)
    if added:
        prefix = os.pathsep.join(added)
        os.environ["PATH"] = f"{prefix}{os.pathsep}{current}" if current else prefix
    return added
