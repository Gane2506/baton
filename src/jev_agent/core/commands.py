"""Run shell commands such as a task's `test_command`, behind an interface tests can fake."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from jev_agent.runlog import write_log
from jev_agent.workers.subprocess_runner import run_command


@dataclass(frozen=True)
class CommandResult:
    exit_code: int  # -1 when the process could not be reaped
    output: str  # combined stdout and stderr, in order
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class CommandRunner(Protocol):
    async def run(self, command: str, cwd: Path, log_path: Path, timeout_s: int) -> CommandResult:
        """Run `command` in a shell in `cwd`; write its (redacted) output to `log_path`."""
        ...


class ShellCommandRunner:
    """Runs the command with `sh -c` through `run_command`, which owns the process lifecycle:
    its own process group, killed on timeout or cancellation, so a test command that starts
    children (`uv run pytest`, `npm test`) cannot outlive its timeout."""

    async def run(self, command: str, cwd: Path, log_path: Path, timeout_s: int) -> CommandResult:
        # `exec 2>&1` makes the shell send stderr to stdout for everything it runs, so the
        # output keeps the order in which the two streams were written.
        script = f"exec 2>&1\n{command}"
        proc = await run_command(["/bin/sh", "-c", script], cwd, dict(os.environ), timeout_s)
        output = proc.stdout + proc.stderr  # stderr only holds the shell's own early errors
        if proc.timed_out:
            output += f"\n[timed out after {timeout_s}s]"
        exit_code = -1 if proc.exit_code is None else proc.exit_code
        result = CommandResult(exit_code, output, timed_out=proc.timed_out)
        status = "timed out" if result.timed_out else f"exit code {result.exit_code}"
        write_log(log_path, f"$ {command}\n{result.output}\n[{status}]\n")
        return result


class FakeCommandRunner:
    """Scripted `CommandRunner` for tests and dry runs: never starts a process.

    `exit_codes` maps a command to the exit codes of its successive runs (the last repeats);
    unknown commands exit 0. `outputs` likewise maps a command to the output of its successive
    runs (default: a line naming the command and its exit code). Every call is recorded in
    `calls` as (command, cwd).
    """

    def __init__(
        self,
        exit_codes: dict[str, list[int]] | None = None,
        outputs: dict[str, list[str]] | None = None,
    ) -> None:
        self.exit_codes = exit_codes or {}
        self.outputs = outputs or {}
        self.calls: list[tuple[str, Path]] = []

    async def run(self, command: str, cwd: Path, log_path: Path, timeout_s: int) -> CommandResult:
        runs = sum(1 for called, _ in self.calls if called == command)
        self.calls.append((command, cwd))
        codes = self.exit_codes.get(command, [0])
        code = codes[min(runs, len(codes) - 1)]
        texts = self.outputs.get(command)
        output = (
            texts[min(runs, len(texts) - 1)]
            if texts
            else f"fake output of {command!r}: exit {code}"
        )
        result = CommandResult(code, output)
        write_log(log_path, f"$ {command}\n{result.output}\n")
        return result
