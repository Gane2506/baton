"""`ToolRunner`: what a quality check gets to run its tool, plus helpers to read a diff.

A check never starts a process itself. It asks the runner whether its tool is installed
(`resolve`), and runs commands through `run`, which goes through the harness's injectable
`CommandRunner` (so tests fake it) with the gate's timeout, in the gate's working directory
(the task worktree or the result branch). Output is redacted and clipped before a check sees it;
the full (redacted) output is in the log file.
"""

from __future__ import annotations

import re
import shlex
import shutil
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path

from jev_agent.core.commands import CommandResult, CommandRunner
from jev_agent.quality.levels import QualityLevel
from jev_agent.quality.models import CheckResult
from jev_agent.runlog import redact

Which = Callable[[str], str | None]

# Indirection so tests can fake PATH lookups (like `doctor_cmd.which`).
which: Which = shutil.which

MAX_OUTPUT_CHARS = 200_000  # what a check may parse
DETAIL_CHARS = 1_500  # output tail kept in a check's detail

DEFAULT_MIN_COVERAGE: dict[QualityLevel, float] = {"medium": 60.0, "production": 80.0}


@dataclass(frozen=True)
class QualitySettings:
    """The `[quality]` settings a gate runs with (level resolved for the run)."""

    level: QualityLevel
    test_command: str | None = None  # `[quality]`, else `[integration]` test_command
    min_coverage: float | None = None  # unset: DEFAULT_MIN_COVERAGE for the level
    required_tools: tuple[str, ...] = ()
    timeout_s: int = 600

    def coverage_threshold(self) -> float:
        if self.min_coverage is not None:
            return self.min_coverage
        if self.level in ("production", "scalable"):
            return DEFAULT_MIN_COVERAGE["production"]
        return DEFAULT_MIN_COVERAGE["medium"]


@dataclass
class ToolRunner:
    commands: CommandRunner
    repo: Path  # the main repository (its .venv/node_modules may hold tools)
    workdir: Path  # where commands run: the task worktree or the result branch
    log_dir: Path
    log_prefix: str  # log files are `<log_dir>/<log_prefix>.<name>.log`
    settings: QualitySettings
    scope: str = "task"  # "task" | "integration"
    diff: str = ""
    passed_commands: Collection[str] = ()  # commands that already passed in this step
    which: Which = field(default=shutil.which)

    def __post_init__(self) -> None:
        self.changed_files = changed_files(self.diff)

    # ---- tools ------------------------------------------------------------------------------

    def resolve(self, tool: str) -> str | None:
        """How to call `tool` (see `resolve_tool`); None when it is not installed."""
        return resolve_tool(tool, (self.workdir, self.repo), self.which)

    def available(self, tool: str) -> bool:
        return self.resolve(tool) is not None

    def required(self, tool: str) -> bool:
        return tool in self.settings.required_tools

    def missing(self, tool: str, hint: str = "") -> CheckResult:
        """The result for a tool that is not installed: skipped, or failed when required."""
        detail = f"{tool} is not installed" + (f" ({hint})" if hint else "")
        if self.required(tool):
            return CheckResult(status="fail", detail=f"{detail}; it is in [quality] required_tools")
        return CheckResult(status="skipped", detail=detail)

    async def run(self, name: str, command: str) -> CommandResult:
        """Run `command` in the working directory; output redacted and clipped."""
        path = self.log_dir / f"{self.log_prefix}.{name}.log"
        result = await self.commands.run(command, self.workdir, path, self.settings.timeout_s)
        output = redact(result.output)
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS]
        return CommandResult(result.exit_code, output, result.timed_out)

    # ---- the diff ---------------------------------------------------------------------------

    def changed(self, suffixes: Collection[str] = ()) -> list[str]:
        """Changed files that still exist, optionally only those ending in one of `suffixes`."""
        files = [f for f in self.changed_files if (self.workdir / f).is_file()]
        if suffixes:
            files = [f for f in files if f.endswith(tuple(suffixes))]
        return files

    def is_changed(self, path: str) -> bool:
        return normalize(path, self.workdir) in self.changed_files


def resolve_tool(tool: str, bases: Collection[Path], which: Which) -> str | None:
    """How to call `tool`: its name when it is on PATH, else the path of a copy in a base
    directory's `node_modules/.bin` or `.venv/bin`; None when it is not installed."""
    if which(tool):
        return tool
    for base in bases:
        for sub in ("node_modules/.bin", ".venv/bin"):
            candidate = base / sub / tool
            if candidate.is_file():
                return str(candidate)
    return None


def tail(text: str, limit: int = DETAIL_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-(limit - 3) :]


def quote(paths: Collection[str]) -> str:
    """Paths for a shell command line."""
    return " ".join(shlex.quote(p) for p in paths)


def normalize(path: str, workdir: Path) -> str:
    """A tool's path as a repo-relative POSIX path (tools print absolute or `./` paths)."""
    text = path.strip()
    if text.startswith("./"):
        text = text[2:]
    candidate = Path(text)
    if candidate.is_absolute():
        try:
            text = candidate.resolve().relative_to(workdir.resolve()).as_posix()
        except ValueError:
            try:
                text = candidate.relative_to(workdir).as_posix()
            except ValueError:
                # Some tools (checkov) print repo paths with a leading `/`.
                relative = text.lstrip("/")
                return relative if (workdir / relative).exists() else text
    return text


_DIFF_FILE = re.compile(r"^\+\+\+ (?:b/)?(?P<path>.+?)\s*$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")


def changed_files(diff: str) -> list[str]:
    """Files a unified diff adds or modifies (deleted files are left out), in diff order."""
    files: list[str] = []
    for line in diff.splitlines():
        match = _DIFF_FILE.match(line)
        if match and match.group("path") != "/dev/null" and match.group("path") not in files:
            files.append(match.group("path"))
    return files


def added_lines(diff: str) -> list[tuple[str, int, str]]:
    """(file, line number in the new file, text) for every line the diff adds."""
    added: list[tuple[str, int, str]] = []
    current: str | None = None
    line_no = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            match = _DIFF_FILE.match(line)
            path = match.group("path") if match else None
            current = None if path in (None, "/dev/null") else path
            continue
        hunk = _HUNK.match(line)
        if hunk:
            line_no = int(hunk.group("start"))
            continue
        if current is None or line.startswith("--- "):
            continue
        if line.startswith("+"):
            added.append((current, line_no, line[1:]))
            line_no += 1
        elif line.startswith(" "):
            line_no += 1
    return added
