"""Running git and the GitHub CLI (`gh`) for shipping.

`Gh` runs `gh` in the repo, never interactively. Tests put a fake `gh` first on
PATH (or pass `executable`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from jev_agent.core.ship.errors import GhTimeoutError, ShipError
from jev_agent.core.workspace import (
    GIT_QUICK_TIMEOUT_S,
    GIT_SLOW_TIMEOUT_S,
    run_git,
    timeout_message,
)
from jev_agent.runlog.redact import redact

__all__ = ["Gh", "GhTimeoutError", "ShipError", "_git"]


def _timeout_s(slow: bool) -> float:
    """The git timeout, read off the `jev_agent.core.ship` package namespace so the
    timeout tests can monkeypatch `ship.GIT_QUICK_TIMEOUT_S` exactly as before the
    split (tests/unit/test_ship.py::test_ship_git_hang_times_out)."""
    from jev_agent.core import ship as _pkg

    quick = getattr(_pkg, "GIT_QUICK_TIMEOUT_S", GIT_QUICK_TIMEOUT_S)
    slow_s = getattr(_pkg, "GIT_SLOW_TIMEOUT_S", GIT_SLOW_TIMEOUT_S)
    return slow_s if slow else quick


def _git(root: Path, *args: str, slow: bool = False) -> subprocess.CompletedProcess[str]:
    """Run git without ever prompting (no terminal, no askpass program; credential helpers
    still work), bounded by a timeout: `slow` (the push) gets GIT_SLOW_TIMEOUT_S."""
    timeout_s = _timeout_s(slow)
    env = os.environ | {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
    try:
        return run_git(args, root, timeout_s, env=env)
    except subprocess.TimeoutExpired as exc:
        raise ShipError(timeout_message(args, timeout_s)) from exc


class Gh:
    """Runs the GitHub CLI (`gh`) in the repo, never interactively. Tests put a fake `gh`
    first on PATH (or pass `executable`)."""

    def __init__(self, cwd: Path, executable: str = "gh", timeout_s: float = 120) -> None:
        self.cwd = cwd
        self.executable = executable
        self.timeout_s = timeout_s

    def path(self) -> str | None:
        return shutil.which(self.executable)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ | {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
        try:
            return subprocess.run(
                [self.executable, *args],
                cwd=self.cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GhTimeoutError(
                f"gh {' '.join(args[:2])} timed out after {self.timeout_s:g} s"
            ) from exc
        except OSError as exc:
            raise ShipError(f"gh {' '.join(args[:2])} could not run: {exc}") from exc

    def out(self, *args: str) -> str:
        proc = self.run(*args)
        if proc.returncode != 0:
            detail = redact((proc.stderr or proc.stdout).strip())
            raise ShipError(f"gh {' '.join(args[:2])} failed: {detail}")
        return proc.stdout

    def json(self, *args: str) -> Any:
        text = self.out(*args)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ShipError(f"gh {' '.join(args[:2])} did not print JSON") from exc
