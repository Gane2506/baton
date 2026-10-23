"""`jev browser install` behind a button (issue #88).

A dashboard-owned background job: at most one install at a time (single-flight), its
output streams to `.jev/browser-install.log` (the Connections page shows the tail), and
it can be stopped (the installer process is terminated). Local installs only — the
`--server` variant stays in the terminal (`jev browser install --server NAME`).

Tier 2: the button asks for confirmation first, naming the exact command. The command
itself is fixed (`servers.browser.install_argv()`); no request can change it.
"""

from __future__ import annotations

import subprocess
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
]

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
            if shutil.which(argv[0]) is None:
                raise BrowserInstallError(
                    f"`{argv[0]}` not found on PATH; install Node.js (which provides npx) "
                    "and restart the dashboard so it picks up the new PATH"
                )
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
