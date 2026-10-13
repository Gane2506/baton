"""Run a CLI as a subprocess with a timeout and a redacted log (used by the CLI worker adapters).

`run_command` never uses a shell. The child gets its own process group, so on timeout the whole
group (the CLI and anything it spawned) is killed. Output is collected in full and returned;
the log gets the argv, cwd, stdin, exit status and output, redacted. Environment values are
never logged.

Workers take a `CommandRunner` so tests can inject recorded outputs instead of real processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from jev_agent.runlog.redact import REDACTED
from jev_agent.runlog.redact import redact as runlog_redact

__all__ = ["REDACTED", "CommandResult", "CommandRunner", "redact", "run_command"]

KILL_GRACE_S = 5.0  # between SIGTERM and SIGKILL on timeout
DRAIN_TIMEOUT_S = 5.0  # how long to wait for pipes to close after the process exits
EXIT_NOT_FOUND = 127
EXIT_NOT_EXECUTABLE = 126


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None  # None only if the process could not be reaped
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_s: float = 0.0


class CommandRunner(Protocol):
    async def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        stdin_text: str | None = None,
        *,
        log_path: Path | None = None,
    ) -> CommandResult: ...


async def run_command(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: float,
    stdin_text: str | None = None,
    *,
    log_path: Path | None = None,
) -> CommandResult:
    """Run `argv` in `cwd` with `env`, feeding `stdin_text`; kill the process group on timeout.

    A missing or non-executable binary is reported as exit code 127/126 with a message on
    stderr, like a shell would, rather than raised.
    """
    started = time.monotonic()
    try:
        result = await _run(argv, cwd, env, timeout_s, stdin_text, started)
    except FileNotFoundError:
        result = _failed_start(EXIT_NOT_FOUND, f"command not found: {argv[0]}", started)
    except PermissionError:
        result = _failed_start(EXIT_NOT_EXECUTABLE, f"command not executable: {argv[0]}", started)
    if log_path is not None:
        log_command(log_path, argv, cwd, timeout_s, stdin_text, result, env)
    return result


async def _run(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: float,
    stdin_text: str | None,
    started: float,
) -> CommandResult:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=dict(env),
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # own process group, so a timeout kills grandchildren too
    )
    out: list[bytes] = []
    err: list[bytes] = []
    assert proc.stdout is not None and proc.stderr is not None
    readers = [
        asyncio.create_task(_drain(proc.stdout, out)),
        asyncio.create_task(_drain(proc.stderr, err)),
    ]
    timed_out = False
    try:
        async with asyncio.timeout(timeout_s):
            if stdin_text is not None:
                await _feed(proc, stdin_text)
            await proc.wait()
    except TimeoutError:
        timed_out = True
        await _kill_group(proc)
    try:
        async with asyncio.timeout(DRAIN_TIMEOUT_S):
            await asyncio.gather(*readers)
    except TimeoutError:
        for reader in readers:
            reader.cancel()
    return CommandResult(
        exit_code=proc.returncode,
        stdout=b"".join(out).decode("utf-8", errors="replace"),
        stderr=b"".join(err).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        duration_s=round(time.monotonic() - started, 3),
    )


async def _drain(stream: asyncio.StreamReader, chunks: list[bytes]) -> None:
    while chunk := await stream.read(65536):
        chunks.append(chunk)


async def _feed(proc: asyncio.subprocess.Process, text: str) -> None:
    assert proc.stdin is not None
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        proc.stdin.write(text.encode("utf-8"))
        await proc.stdin.drain()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        proc.stdin.close()


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    for sig, wait_s in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, KILL_GRACE_S)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            async with asyncio.timeout(wait_s):
                await proc.wait()
            return
        except TimeoutError:
            continue


def _failed_start(code: int, message: str, started: float) -> CommandResult:
    return CommandResult(
        exit_code=code,
        stdout="",
        stderr=message,
        duration_s=round(time.monotonic() - started, 3),
    )


# --- logging -------------------------------------------------------------------------------


def log_command(
    log_path: Path,
    argv: Sequence[str],
    cwd: Path,
    timeout_s: float,
    stdin_text: str | None,
    result: CommandResult,
    env: Mapping[str, str],
) -> None:
    """Append one command's record to `log_path`. Env values are never written."""
    status = "timed out" if result.timed_out else f"exit code {result.exit_code}"
    sections = [
        ("command", " ".join(argv)),
        ("cwd", str(cwd)),
        ("timeout_s", str(timeout_s)),
        ("stdin", stdin_text if stdin_text is not None else "(none)"),
        ("status", f"{status} after {result.duration_s:.1f}s"),
        ("stdout", result.stdout or "(empty)"),
        ("stderr", result.stderr or "(empty)"),
    ]
    append_log_sections(log_path, sections, env)


def append_log_sections(
    log_path: Path, sections: Sequence[tuple[str, str]], env: Mapping[str, str] | None = None
) -> None:
    """Append titled sections to `log_path`, redacted against `env` (default `os.environ`)."""
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    text = "".join(f"===== {title} ({stamp}) =====\n{body}\n" for title, body in sections)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(redact(text, env))


# --- redaction -----------------------------------------------------------------------------


def redact(text: str, env: Mapping[str, str] | None = None) -> str:
    """`jev_agent.runlog.redact.redact` against the secret values of both `env` and `os.environ`."""
    return runlog_redact(text, {**os.environ, **(env or {})})
