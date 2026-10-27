"""`core.file_lock`: cross-platform file locks and positioned I/O (issue #333).

These tests run the POSIX path (fcntl); the Windows path (msvcrt) is exercised by
the Windows CI job, which is what proves it.
"""

import os
from pathlib import Path

import pytest

from jev_agent.core import file_lock


def _open(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o644)


def test_exclusive_lock_blocks_second_fd(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    first = _open(path)
    second = _open(path)
    try:
        assert file_lock.try_lock_exclusive(first) is True
        assert file_lock.try_lock_exclusive(second) is False
        file_lock.unlock(first)
        assert file_lock.try_lock_exclusive(second) is True
    finally:
        os.close(first)
        os.close(second)


def test_shared_lock_fails_when_exclusive_held(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    first = _open(path)
    second = _open(path)
    try:
        assert file_lock.try_lock_exclusive(first) is True
        assert file_lock.try_lock_shared(second) is False
        file_lock.unlock(first)
        assert file_lock.try_lock_shared(second) is True
    finally:
        os.close(first)
        os.close(second)


def test_lock_released_when_fd_closes(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    first = _open(path)
    assert file_lock.try_lock_exclusive(first) is True
    os.close(first)  # no unlock: the kernel releases the lock on close
    second = _open(path)
    try:
        assert file_lock.try_lock_exclusive(second) is True
    finally:
        os.close(second)


def test_read_write_at_roundtrip_and_position(tmp_path: Path) -> None:
    path = tmp_path / "data"
    fd = _open(path)
    try:
        file_lock.write_at(fd, b"hello world", 0)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0  # positioned I/O moves nothing
        assert file_lock.read_at(fd, 5, 0) == b"hello"
        assert file_lock.read_at(fd, 5, 6) == b"world"
        file_lock.write_at(fd, b"WORLD", 6)
        assert file_lock.read_at(fd, 11, 0) == b"hello WORLD"
    finally:
        os.close(fd)


@pytest.mark.skipif(
    os.name != "posix", reason="msvcrt branch needs Windows (proven by the Windows CI job)"
)
def test_fcntl_is_the_posix_backend() -> None:
    assert getattr(file_lock, "fcntl", None) is not None
