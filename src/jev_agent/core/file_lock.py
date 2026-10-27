"""Cross-platform file locking and positioned I/O (deploy Phase 1, issue #333).

`fcntl.flock`, `os.pread` and `os.pwrite` are Unix-only: importing them unguarded
crashes `jev` on Windows before it can do anything (the CLI entry point imports
the scheduler). This module is the one place that knows the platform difference:

- POSIX: `fcntl.flock` (shared/exclusive, non-blocking) and `os.pread`/`os.pwrite`.
- Windows: `msvcrt.locking` on the first byte of the file (`LK_NBLCK` /
  `LK_UNLCK`; there is no shared lock, so a shared attempt takes a brief
  exclusive lock and releases it — the callers only need "is it held?"), and
  `os.lseek` + `os.read` / `os.write` for positioned I/O.

Both lock flavours release when the file descriptor closes, so a crashed holder
never leaves a stale lock — the property the scheduler and chat locks rely on.
"""

from __future__ import annotations

import contextlib
import os

__all__ = [
    "read_at",
    "try_lock_exclusive",
    "try_lock_shared",
    "unlock",
    "write_at",
]

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms have no flock
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows platforms have no msvcrt
    msvcrt = None  # type: ignore[assignment]


def try_lock_exclusive(fd: int) -> bool:
    """Take a non-blocking exclusive lock on `fd`'s file; False when it is held."""
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    if msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    # No locking primitive on this platform: the lock file is advisory-only and
    # a single process is the only holder (same as `app.settings`' no-op).
    return True


def try_lock_shared(fd: int) -> bool:
    """Take a non-blocking shared lock; False when an exclusive holder has it.

    Windows has no shared lock: the attempt takes the exclusive lock and holds
    it until `unlock`, which answers the callers' only question — "is the file
    exclusively held?" — the same way.
    """
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    return try_lock_exclusive(fd)


def unlock(fd: int) -> None:
    """Release a lock taken by `try_lock_exclusive` / `try_lock_shared`."""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def read_at(fd: int, size: int, offset: int = 0) -> bytes:
    """`size` bytes from `offset`, without moving the file position."""
    if hasattr(os, "pread"):
        return os.pread(fd, size, offset)
    position = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, size)
    finally:
        os.lseek(fd, position, os.SEEK_SET)


def write_at(fd: int, data: bytes, offset: int = 0) -> None:
    """Write `data` at `offset`, without moving the file position."""
    if hasattr(os, "pwrite"):
        os.pwrite(fd, data, offset)
        return
    position = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.lseek(fd, position, os.SEEK_SET)
