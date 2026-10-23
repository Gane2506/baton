"""`jev browser install` behind a button (issue #88).

A dashboard-owned background job: at most one install at a time (single-flight), its
output streams to `.jev/browser-install.log` (the Connections page shows the tail), and
it can be stopped (the installer process is terminated). Local installs only — the
`--server` variant stays in the terminal (`jev browser install --server NAME`).

Tier 2: the button asks for confirmation first, naming the exact command. The command
itself is fixed (`servers.browser.install_argv()`); no request can change it.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jev_agent.servers.browser import install_argv

__all__ = [
    "BrowserInstallBusy",
    "BrowserInstallError",
    "BrowserInstallRunner",
    "BrowserInstallStatus",
    "resolve_executable",
]


def _platform_search_dirs() -> list[Path]:
    """Well-known install locations per platform, for when PATH is minimal.

    The dashboard is often launched from a bare environment (no shell profile),
    so Homebrew/nvm/Windows-npm locations that a terminal would have on PATH
    are missing. These are checked as a fallback after `shutil.which`.
    """
    home = Path.home()
    if sys.platform == "darwin":
        return [
            Path("/opt/homebrew/bin"),  # Apple Silicon Homebrew
            Path("/usr/local/bin"),  # Intel Homebrew
            home / ".nvm" / "versions" / "node",
        ]
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", ""))
        localappdata = Path(os.environ.get("LOCALAPPDATA", ""))
        program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        return [
            appdata / "npm",
            program_files / "nodejs",
            localappdata / "nvm",
        ]
    # Linux and other Unix-likes
    candidates = [
        Path("/usr/local/bin"),
        Path("/opt/nodejs/bin"),
        home / ".nvm" / "versions" / "node",
        home / ".local" / "bin",
    ]
    # nvm version dirs: ~/.nvm/versions/node/vXX.YY.ZZ/bin
    nvm_root = home / ".nvm" / "versions" / "node"
    if nvm_root.is_dir():
        with contextlib.suppress(OSError):
            candidates.extend(
                sorted((nvm_root / d / "bin" for d in os.listdir(nvm_root)), reverse=True)
            )
    return candidates


def resolve_executable(name: str) -> str | None:
    """Full path to `name`, or None when it cannot be found.

    Checks PATH first via `shutil.which`, then falls back to well-known
    per-platform install locations (Homebrew on macOS, npm/nodejs dirs on
    Windows, /usr/local + nvm on Linux).
    """
    found = shutil.which(name)
    if found:
        return found
    suffix = ".exe" if sys.platform == "win32" else ""
    for directory in _platform_search_dirs():
        candidate = directory / f"{name}{suffix}"
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


_LOG_FILENAME = "browser-install.log"
_LOG_TAIL_LINES = 30


class BrowserInstallBusy(RuntimeError):
    """An install is already running."""


class BrowserInstallError(RuntimeError):
    """The install could not be started (e.g. `npx` not on PATH)."""


@dataclass(frozen=True)
class BrowserInstallStatus:
    """What the Connections page's browser panel shows."""

    state: str  # "idle" | "running" | "done" | "failed" | "stopped"
    command: str  # the exact command that runs (or ran)
    message: str  # one-line outcome, when finished
    started_at: datetime | None
    finished_at: datetime | None
    log_tail: tuple[str, ...]


def _spawn(argv: list[str], log: Path) -> subprocess.Popen[bytes]:
    """Start the installer with its output appended to `log`. Indirection for tests."""
    handle = log.open("ab")
    try:
        return subprocess.Popen(
            argv, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL
        )
    except BaseException:
        handle.close()
        raise


class BrowserInstallRunner:
    """One browser install at a time, owned by the dashboard process."""

    def __init__(
        self,
        root: Path,
        spawn: Callable[[list[str], Path], subprocess.Popen[bytes]] = _spawn,
    ) -> None:
        self.root = root
        self._spawn = spawn
        self._guard = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._state = "idle"
        self._message = ""
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None

    # ---- state -------------------------------------------------------------------

    def _log_path(self) -> Path:
        return self.root / ".jev" / _LOG_FILENAME

    def status(self) -> BrowserInstallStatus:
        with self._guard:
            state = self._state
            message = self._message
            started_at = self._started_at
            finished_at = self._finished_at
        return BrowserInstallStatus(
            state=state,
            command=" ".join(install_argv()),
            message=message,
            started_at=started_at,
            finished_at=finished_at,
            log_tail=self._tail(),
        )

    def _tail(self) -> tuple[str, ...]:
        try:
            data = self._log_path().read_bytes()
        except OSError:
            return ()
        lines = data.decode("utf-8", errors="replace").splitlines()
        return tuple(lines[-_LOG_TAIL_LINES:])

    def busy(self) -> bool:
        with self._guard:
            return self._state == "running"

    # ---- control -----------------------------------------------------------------

    def start(self) -> None:
        """Start the install in a background thread; `BrowserInstallBusy` when one runs,
        `BrowserInstallError` when the installer cannot be started."""
        with self._guard:
            if self._state == "running":
                raise BrowserInstallBusy("a browser install is already running")
            argv = install_argv()
            resolved = resolve_executable(argv[0])
            if resolved is None:
                raise BrowserInstallError(
                    f"`{argv[0]}` not found; install Node.js (which provides npx). "
                    f"Searched PATH plus the usual {sys.platform} install locations."
                )
            argv = [resolved, *argv[1:]]
            log = self._log_path()
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_bytes(b"")  # a fresh log per install
            proc = self._spawn(argv, log)
            self._proc = proc
            self._state = "running"
            self._message = ""
            self._started_at = datetime.now(UTC)
            self._finished_at = None
            self._thread = threading.Thread(
                target=self._watch, args=(proc,), name="jev-browser-install", daemon=True
            )
            try:
                self._thread.start()
            except BaseException:
                self._state = "idle"
                self._proc = None
                raise

    def stop(self) -> bool:
        """Terminate the installer; False when no install is running."""
        with self._guard:
            proc = self._proc
            if self._state != "running" or proc is None:
                return False
            self._state = "stopped"
            self._message = "stopped"
            self._finished_at = datetime.now(UTC)
        proc.terminate()
        return True

    def _watch(self, proc: subprocess.Popen[bytes]) -> None:
        try:
            # P2: the runner owns the child's lifetime. `communicate()` reaps
            # the process; its output already streams to the log file, so this
            # is equivalent to `wait()` here. Injected test doubles only
            # expose `wait()`.
            if isinstance(proc, subprocess.Popen):
                proc.communicate()
                code: int | None = proc.returncode
            else:  # test double
                code = proc.wait()
        except Exception as exc:  # never leave the runner stuck in "running"
            code = None
            outcome = f"the installer could not be watched: {exc}"
        else:
            outcome = "browser installed" if code == 0 else f"the install exited with code {code}"
        with self._guard:
            if self._state == "running":  # a stop() meanwhile wins
                self._state = "done" if code == 0 else "failed"
                self._message = outcome
                self._finished_at = datetime.now(UTC)
            self._proc = None
            self._thread = None

    def shutdown(self, timeout: float = 30.0) -> None:
        """Stop a running install and wait for its thread (the lifespan calls this)."""
        self.stop()
        with self._guard:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
