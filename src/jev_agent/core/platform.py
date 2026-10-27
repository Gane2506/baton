"""Cross-platform process-group termination (deploy Phase 1, issue #333).

`os.killpg` and `signal.SIGKILL` do not exist on Windows: the worker-kill paths
called them unguarded, so a timed-out worker crashed the killer instead of the
worker. `kill_process_group` is the one place that knows the difference — POSIX
sends the signal to the process group, Windows runs `taskkill /T` (the whole
tree) with `/F` only for the force pass, matching the TERM-then-KILL escalation
the POSIX callers already do.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

__all__ = ["kill_process_group"]


def kill_process_group(pid: int, *, force: bool) -> None:
    """Terminate the process group `pid` (and its children).

    POSIX: `SIGTERM` (graceful) or `SIGKILL` (force) to the group. Windows:
    `taskkill /T` (the tree), `/F` only when `force`. Raises `ProcessLookupError`
    when the group is already gone, `PermissionError` when it cannot be signalled
    — callers suppress both, as they did for `os.killpg`.
    """
    if hasattr(os, "killpg"):
        os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
        return
    if sys.platform == "win32":
        argv = ["taskkill", "/T", "/PID", str(pid)]
        if force:
            argv.insert(1, "/F")
        result = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 128:  # taskkill: no such process
            raise ProcessLookupError(f"no process group {pid}")
        return
    raise NotImplementedError(f"no process-group kill on {sys.platform}")
