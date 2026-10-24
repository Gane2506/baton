"""The `QualityCheck` protocol, `BaseCheck` defaults, and helpers every check uses: project
detection (cheap file tests) and tool output parsing. See `checks.py` for how to add a check.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any, Protocol

from jev_agent.core.commands import CommandResult
from jev_agent.quality.levels import LEVELS, QualityLevel
from jev_agent.quality.models import CheckResult, Finding
from jev_agent.quality.tools import ToolRunner, normalize, tail
from jev_agent.roles.schemas import Category, Severity

Scope = str  # "task" | "integration"
BOTH: tuple[Scope, ...] = ("task", "integration")


def from_level(level: QualityLevel) -> tuple[QualityLevel, ...]:
    """`level` and every level above it."""
    return LEVELS[LEVELS.index(level) :]


class QualityCheck(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def tool(self) -> str | None: ...
    @property
    def levels(self) -> tuple[QualityLevel, ...]: ...
    @property
    def gate_from(self) -> QualityLevel | None: ...
    @property
    def scopes(self) -> tuple[Scope, ...]: ...
    @property
    def needs(self) -> str: ...
    @property
    def hint(self) -> str: ...
    @property
    def whole_repo(self) -> bool: ...

    def applies(self, repo: Path) -> bool: ...

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult: ...


class BaseCheck:
    """Defaults for the attributes of `QualityCheck`."""

    name: str = ""
    tool: str | None = None
    levels: tuple[QualityLevel, ...] = from_level("basic")
    gate_from: QualityLevel | None = None
    scopes: tuple[Scope, ...] = BOTH
    needs: str = ""
    hint: str = ""
    whole_repo: bool = False

    def applies(self, repo: Path) -> bool:
        return True

    def finding(
        self,
        severity: Severity,
        category: Category,
        title: str,
        file: str = "",
        line: int | None = None,
        line_end: int | None = None,
        suggestion: str = "",
    ) -> Finding:
        return Finding(
            source=self.name,
            severity=severity,
            category=category,
            title=" ".join(title.split())[:300],
            file=file,
            line_start=line,
            line_end=line_end,
            suggestion=suggestion[:500],
        )


# ---- project detection (cheap file tests) -------------------------------------------------

_PY_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
_DEP_FILES = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "package.json",
    "package-lock.json",
)
JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
PY_SUFFIXES = (".py",)


def is_python(repo: Path) -> bool:
    return any((repo / name).is_file() for name in _PY_MARKERS)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def pyproject(repo: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(read_text(repo / "pyproject.toml"))
    except tomllib.TOMLDecodeError:
        return {}


def package_json(repo: Path) -> dict[str, Any] | None:
    path = repo / "package.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def mentions(repo: Path, *words: str) -> bool:
    """Whether a dependency file of the project mentions any of `words`."""
    names = [*_DEP_FILES, *(p.name for p in repo.glob("requirements*.txt"))]
    text = "\n".join(read_text(repo / name) for name in dict.fromkeys(names)).lower()
    return any(word.lower() in text for word in words)


def has_any(repo: Path, *patterns: str) -> bool:
    return any(next(repo.glob(pattern), None) is not None for pattern in patterns)


def iac_files(repo: Path) -> list[str]:
    """Container and infrastructure-as-code files near the top of the repo."""
    patterns = (
        "Dockerfile*",
        "*/Dockerfile*",
        "**/Dockerfile*",
        "*.tf",
        "*/*.tf",
        "docker-compose*.y*ml",
        "compose*.y*ml",
        "k8s/**/*.y*ml",
        "kubernetes/**/*.y*ml",
        "helm/**/Chart.yaml",
        "charts/**/Chart.yaml",
    )
    found: list[str] = []
    for pattern in patterns:
        for path in repo.glob(pattern):
            if path.is_file() and ".jev" not in path.parts and "node_modules" not in path.parts:
                found.append(path.relative_to(repo).as_posix())
    return sorted(set(found))


def dependency_files_changed(runner: ToolRunner) -> bool:
    return any(
        Path(f).name in _DEP_FILES or Path(f).name.startswith("requirements")
        for f in runner.changed_files
    )


# ---- output parsing ------------------------------------------------------------------------

_LINE = re.compile(r"^(?P<file>[^\s:][^:]*):(?P<line>\d+)(?::(?P<col>\d+))?:\s*(?P<msg>.+)$")
TSC_LINE = re.compile(r"^(?P<file>[^\s(][^(]*)\((?P<line>\d+),(?P<col>\d+)\): error (?P<msg>.+)$")
PYRIGHT_LINE = re.compile(
    r"^\s*(?P<file>[^\s:][^:]*):(?P<line>\d+):(?P<col>\d+) - (?P<sev>error|warning): (?P<msg>.+)$"
)


def parse_lines(
    output: str, workdir: Path, pattern: re.Pattern[str] = _LINE
) -> list[tuple[str, int, str]]:
    """(file, line, message) for each output line matching `pattern`."""
    found: list[tuple[str, int, str]] = []
    for raw in output.splitlines():
        match = pattern.match(raw.rstrip())
        if match:
            found.append(
                (normalize(match.group("file"), workdir), int(match.group("line")), match["msg"])
            )
    return found


def load_json(output: str) -> Any:
    """The JSON document in a tool's output (tools may print a line or two before it)."""
    text = output.strip()
    for start in (0, *(i for i, ch in enumerate(text) if ch in "[{")):
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            continue
    return None


def tool_error(check: BaseCheck, result: CommandResult, problem: str = "") -> CheckResult:
    """The tool is installed but gave no usable answer: it timed out, crashed, or printed
    output we could not parse. Status `error`, never findings (the coder cannot fix a broken
    tool); the policy blocks on it when the check is required."""
    tool = check.tool or check.name
    if result.timed_out:
        problem = f"{tool} timed out"
    elif not problem:
        problem = f"{tool} failed or printed output that could not be parsed"
    return CheckResult(
        status="error", detail=f"{problem} (exit {result.exit_code}): {tail(result.output)}"
    )


def exit_mismatch(check: BaseCheck, result: CommandResult, *, found: bool) -> CheckResult | None:
    """A JSON scanner whose exit code contradicts its parsed report, or None when they
    agree (issue #204 I-1). Scanners exit 0 for a clean scan and 1 when they found
    something; any other code is a crash, and exit 1 with nothing listed means the scan
    errored while emitting valid empty JSON — both silently passed before, because the
    checks only looked at the parsed report. A mismatch is a tool error, never a pass:
    the scan cannot be trusted to have covered anything."""
    if result.timed_out:
        return None  # callers already turned a timeout into a tool error
    tool = check.tool or check.name
    if result.exit_code not in (0, 1):
        return tool_error(
            check, result, f"{tool} exited {result.exit_code}, not a findings exit code"
        )
    if result.exit_code == 1 and not found:
        return tool_error(check, result, f"{tool} exited 1 but listed no findings")
    return None


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
