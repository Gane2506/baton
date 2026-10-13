"""Run shell commands such as a task's `test_command`, behind an interface tests can fake."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from jev_agent.runlog import write_log


@dataclass(frozen=True)
class CommandResult:
    exit_code: int  # -1 when the command timed out
    output: str  # combined stdout and stderr

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class CommandRunner(Protocol):
    async def run(self, command: str, cwd: Path, log_path: Path, timeout_s: int) -> CommandResult:
        """Run `command` in a shell in `cwd`; write its (redacted) output to `log_path`."""
        ...


class ShellCommandRunner:
    async def run(self, command: str, cwd: Path, log_path: Path, timeout_s: int) -> CommandResult:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            result = CommandResult(-1, f"timed out after {timeout_s}s")
        else:
            assert proc.returncode is not None
            result = CommandResult(proc.returncode, stdout.decode(errors="replace"))
        write_log(log_path, f"$ {command}\n{result.output}\n[exit code {result.exit_code}]\n")
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
