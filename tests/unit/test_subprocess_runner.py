"""Tests for `run_command` using the Python interpreter as the child (never a real agent CLI)."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from jev_agent.workers.subprocess_runner import REDACTED, append_log_sections, redact, run_command

PY = sys.executable
SECRET = "sk-ant-api03-SECRETVALUE1234567890"


def _env(**extra: str) -> dict[str, str]:
    return {**os.environ, **extra}


async def test_collects_stdout_stderr_exit_code_and_stdin(tmp_path: Path) -> None:
    code = (
        "import sys; data = sys.stdin.read(); print('got:' + data.upper()); "
        "print('warn', file=sys.stderr); sys.exit(3)"
    )
    result = await run_command([PY, "-c", code], tmp_path, _env(), 30, stdin_text="hello")
    assert result.exit_code == 3
    assert result.stdout.strip() == "got:HELLO"
    assert result.stderr.strip() == "warn"
    assert not result.timed_out
    assert result.duration_s >= 0


async def test_runs_in_cwd_and_without_stdin(tmp_path: Path) -> None:
    code = "import os, sys; print(os.getcwd()); print(repr(sys.stdin.read()))"
    result = await run_command([PY, "-c", code], tmp_path, _env(), 30)
    lines = result.stdout.splitlines()
    assert Path(lines[0]).resolve() == tmp_path.resolve()
    assert lines[1] == "''"


async def test_large_stdin_and_output_do_not_deadlock(tmp_path: Path) -> None:
    big = "x" * 2_000_000
    code = "import sys; d = sys.stdin.read(); sys.stdout.write(d); sys.stderr.write(d)"
    result = await run_command([PY, "-c", code], tmp_path, _env(), 60, stdin_text=big)
    assert result.exit_code == 0
    assert len(result.stdout) == len(big) and len(result.stderr) == len(big)


async def test_timeout_kills_process_group(tmp_path: Path) -> None:
    # The child spawns a grandchild that holds the pipes open; both must die promptly.
    code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([{PY!r}, '-c', 'import time; time.sleep(60)']); "
        "print('started', flush=True); time.sleep(60)"
    )
    started = time.monotonic()
    result = await run_command([PY, "-c", code], tmp_path, _env(), 1.0, log_path=tmp_path / "t.log")
    assert result.timed_out
    assert result.exit_code is not None and result.exit_code != 0
    assert "started" in result.stdout
    assert time.monotonic() - started < 15
    assert "timed out" in (tmp_path / "t.log").read_text()


def _alive(pid: int) -> bool:
    """True while `pid` runs (a zombie waiting for its parent to reap it counts as dead)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/status")
    return not (status.exists() and "\nState:\tZ" in status.read_text())


async def test_cancellation_kills_process_group(tmp_path: Path) -> None:
    # Ctrl-C cancels the awaiting task; the child runs in its own session, so nothing else
    # would stop it or its grandchild (issue #11).
    pids = tmp_path / "pids"
    code = (
        "import os, subprocess, sys, time; "
        f"g = subprocess.Popen([{PY!r}, '-c', 'import time; time.sleep(60)']); "
        f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{g.pid}}'); time.sleep(60)"
    )
    task = asyncio.create_task(run_command([PY, "-c", code], tmp_path, _env(), 60))
    for _ in range(100):
        if pids.exists() and len(pids.read_text().split()) == 2:
            break
        await asyncio.sleep(0.05)
    child, grandchild = (int(p) for p in pids.read_text().split())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    deadline = time.monotonic() + 15
    while (_alive(child) or _alive(grandchild)) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _alive(child) and not _alive(grandchild)


async def test_missing_binary_is_reported_not_raised(tmp_path: Path) -> None:
    result = await run_command(["jev-no-such-binary-xyz"], tmp_path, _env(), 5)
    assert result.exit_code == 127
    assert "command not found" in result.stderr


async def test_log_has_command_and_output_but_never_env_values(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "run.log"
    env = _env(MY_SERVICE_API_KEY=SECRET, ANTHROPIC_API_KEY="anthropic-secret-value-42")
    code = "import os; print('key=' + os.environ['MY_SERVICE_API_KEY']); print('ok')"
    result = await run_command([PY, "-c", code], tmp_path, env, 30, "the prompt", log_path=log)
    assert SECRET in result.stdout  # the caller gets the raw output...
    text = log.read_text()
    assert SECRET not in text  # ...the log does not
    assert "anthropic-secret-value-42" not in text
    assert REDACTED in text
    assert "the prompt" in text and "exit code 0" in text and "ok" in text
    assert "MY_SERVICE_API_KEY=" not in text  # env is not dumped


async def test_log_is_appended_across_commands(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    await run_command([PY, "-c", "print('first')"], tmp_path, _env(), 30, log_path=log)
    await run_command([PY, "-c", "print('second')"], tmp_path, _env(), 30, log_path=log)
    text = log.read_text()
    assert text.index("first") < text.index("second")


def test_redact_patterns_and_env_values() -> None:
    env = {"SOME_TOKEN": "tok-abcdefghijkl", "HOME": "/Users/someone-long-path"}
    text = (
        "Bearer abcdefghijklmnop sk-or-v1-0123456789abcdef tok-abcdefghijkl "
        "/Users/someone-long-path"
    )
    out = redact(text, env)
    assert "abcdefghijklmnop" not in out
    assert "sk-or-v1-0123456789abcdef" not in out
    assert "tok-abcdefghijkl" not in out
    assert "/Users/someone-long-path" in out  # HOME is not a secret


def test_append_log_sections_redacts(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    append_log_sections(log, [("result", f"summary {SECRET}")], {})
    assert SECRET not in log.read_text()
    assert "===== result" in log.read_text()
