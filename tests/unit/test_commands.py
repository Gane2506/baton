"""Tests for `ShellCommandRunner` with real `sh` processes (no agent CLIs, no network)."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from jev_agent.core.commands import ShellCommandRunner

PY = shlex.quote(sys.executable)


def _alive(pid: int) -> bool:
    """True while `pid` runs (a zombie waiting for its parent to reap it counts as dead)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if not Path("/proc").is_dir():  # macOS: kill(0) succeeded, so it runs
        return True
    try:  # a zombie still answers kill(0); it may also be reaped right now
        state = Path(f"/proc/{pid}/status").read_text()
    except FileNotFoundError:
        return False
    return "\nState:\tZ" not in state


async def test_output_keeps_stream_order_and_exit_code(tmp_path: Path) -> None:
    command = "echo out1; echo err1 >&2; echo out2; exit 3"
    result = await ShellCommandRunner().run(command, tmp_path, tmp_path / "c.log", 30)
    assert result.exit_code == 3 and not result.ok and not result.timed_out
    assert result.output.split() == ["out1", "err1", "out2"]
    log = (tmp_path / "c.log").read_text()
    assert "$ echo out1" in log and "[exit code 3]" in log


async def test_timeout_kills_children_holding_the_pipe(tmp_path: Path) -> None:
    # A test command that starts a child (like `uv run pytest`) used to ignore the timeout
    # until the child exited by itself, and the child was never killed (issue #12).
    pid_file = tmp_path / "child.pid"
    child = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    command = f"echo started; {PY} -c {shlex.quote(child)}; echo never"
    started = time.monotonic()
    result = await ShellCommandRunner().run(command, tmp_path, tmp_path / "t.log", 1)
    assert time.monotonic() - started < 15
    assert result.timed_out and not result.ok
    assert "started" in result.output and "never" not in result.output
    assert "timed out after 1s" in result.output
    assert "[timed out]" in (tmp_path / "t.log").read_text()
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _alive(pid)


async def test_cancellation_kills_the_command(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    child = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    command = f"{PY} -c {shlex.quote(child)}"
    task = asyncio.create_task(ShellCommandRunner().run(command, tmp_path, tmp_path / "x.log", 60))
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    deadline = time.monotonic() + 15
    while _alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _alive(pid)


async def test_missing_command_is_a_failed_result(tmp_path: Path) -> None:
    result = await ShellCommandRunner().run("jev-no-such-cmd-xyz", tmp_path, tmp_path / "m.log", 10)
    assert result.exit_code == 127 and not result.timed_out
    assert "not found" in result.output
