"""Production- and scalable-level checks: type checkers (mypy, pyright, tsc), licences of new
dependencies, and container/IaC scans (trivy, hadolint, checkov).
"""

from __future__ import annotations

import re
from pathlib import Path

from jev_agent.core.commands import CommandResult
from jev_agent.quality.base import (
    JS_SUFFIXES,
    PY_SUFFIXES,
    PYRIGHT_LINE,
    TSC_LINE,
    BaseCheck,
    exit_mismatch,
    from_level,
    iac_files,
    int_or_none,
    is_python,
    load_json,
    parse_lines,
    pyproject,
    read_text,
    tool_error,
)
from jev_agent.quality.models import CheckResult, Finding
from jev_agent.quality.tools import ToolRunner, added_lines, normalize, quote
from jev_agent.roles.schemas import Severity

# ---- production: types, licences ----------------------------------------------------------


def _filtered_type_result(
    check: BaseCheck, runner: ToolRunner, found: list[tuple[str, int, str]], result: CommandResult
) -> CheckResult:
    """Type errors in the changed files fail the check; others are only counted. A timeout,
    or a non-zero exit with no errors listed, is a tool error."""
    if result.timed_out or (result.exit_code != 0 and not found):
        return tool_error(check, result, f"{check.tool} failed with no errors listed")
    mine = [(f, line, msg) for f, line, msg in found if runner.is_changed(f)]
    others = len(found) - len(mine)
    note = f"; {others} error(s) in files this work did not change" if others else ""
    if not mine:
        return CheckResult(status="pass", detail=f"no type errors in changed files{note}")
    findings = [check.finding("major", "bug", msg, f, line) for f, line, msg in mine]
    return CheckResult(status="fail", detail=f"{len(mine)} type error(s){note}", findings=findings)


def _mypy_configured(repo: Path) -> bool:
    return (
        "mypy" in pyproject(repo).get("tool", {})
        or (repo / "mypy.ini").is_file()
        or (repo / ".mypy.ini").is_file()
        or "[mypy" in read_text(repo / "setup.cfg")
    )


class MypyCheck(BaseCheck):
    name = "mypy"
    tool = "mypy"
    levels = from_level("production")
    gate_from = "production"
    needs = "a mypy config ([tool.mypy], mypy.ini or setup.cfg)"
    hint = "pip install mypy"

    def applies(self, repo: Path) -> bool:
        return is_python(repo) and _mypy_configured(repo)

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        mypy = runner.resolve("mypy")
        if mypy is None:
            return runner.missing("mypy", self.hint)
        if not runner.changed(PY_SUFFIXES):
            return CheckResult(status="pass", detail="no Python files changed")
        result = await runner.run("mypy", f"{mypy} --no-error-summary --no-pretty")
        found = [
            (f, line, msg.removeprefix("error:").strip())
            for f, line, msg in parse_lines(result.output, workdir)
            if msg.startswith("error:")
        ]
        return _filtered_type_result(self, runner, found, result)


class PyrightCheck(BaseCheck):
    name = "pyright"
    tool = "pyright"
    levels = from_level("production")
    gate_from = "production"
    needs = "a pyright config (pyrightconfig.json or [tool.pyright])"
    hint = "pip install pyright, or npm install --save-dev pyright"

    def applies(self, repo: Path) -> bool:
        return (repo / "pyrightconfig.json").is_file() or "pyright" in pyproject(repo).get(
            "tool", {}
        )

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        pyright = runner.resolve("pyright")
        if pyright is None:
            return runner.missing("pyright", self.hint)
        if not runner.changed(PY_SUFFIXES):
            return CheckResult(status="pass", detail="no Python files changed")
        result = await runner.run("pyright", pyright)
        found = []
        for raw in result.output.splitlines():
            match = PYRIGHT_LINE.match(raw.rstrip())
            if match and match.group("sev") == "error":
                file = normalize(match.group("file"), workdir)
                found.append((file, int(match.group("line")), match.group("msg")))
        return _filtered_type_result(self, runner, found, result)


class TscCheck(BaseCheck):
    name = "tsc"
    tool = "tsc"
    levels = from_level("production")
    gate_from = "production"
    needs = "a tsconfig.json"
    hint = "npm install --save-dev typescript"

    def applies(self, repo: Path) -> bool:
        return (repo / "tsconfig.json").is_file()

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        tsc = runner.resolve("tsc")
        if tsc is None:
            return runner.missing("tsc", self.hint)
        if not runner.changed(JS_SUFFIXES):
            return CheckResult(status="pass", detail="no JS/TS files changed")
        result = await runner.run("tsc", f"{tsc} --noEmit --pretty false")
        found = parse_lines(result.output, workdir, TSC_LINE)
        return _filtered_type_result(self, runner, found, result)


_REQ_LINE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)")
COPYLEFT = ("AGPL", "SSPL", "GPL")  # LGPL is reported, not flagged as major


_DEP_ARRAY_START = re.compile(r"^\s*dependencies\s*=\s*\[")
_QUOTED = re.compile(r""""([^"\\]*)"|'([^'\\]*)""")


def _quoted_values(line: str) -> list[str]:
    """The inner text of every single- or double-quoted string on the line."""
    return [
        match.group(1) if match.group(1) is not None else match.group(2)
        for match in _QUOTED.finditer(line)
    ]


def _toml_dependency_names(added: list[str]) -> list[str]:
    """Names from `dependencies = [...]` arrays in added pyproject.toml lines, whether the
    array sits on a single line or is split across lines. A bare quoted line is treated as
    an element of a multi-line array whose opening bracket is outside the diff."""
    values: list[str] = []
    in_array = False
    for text in added:
        line = text
        if not in_array:
            start = _DEP_ARRAY_START.match(line)
            if start is None:
                if line.strip()[:1] in ("'", '"'):
                    values.extend(_quoted_values(line))
                continue
            in_array = True
            line = line[start.end() :]
        values.extend(_quoted_values(line))
        if "]" in line:
            in_array = False
    names: list[str] = []
    for value in values:
        match = _REQ_LINE.match(value)
        if match and match.group("name").lower() != "python":
            names.append(match.group("name"))
    return names


def new_python_dependencies(runner: ToolRunner) -> list[str]:
    """Package names on lines the diff adds to `requirements*.txt`, or entries it adds to
    `pyproject.toml` dependency arrays (single-line or multi-line). A heuristic: pins and
    extras are stripped."""
    names: list[str] = []
    by_file: dict[str, list[str]] = {}
    for file, _, text in added_lines(runner.diff):
        by_file.setdefault(file, []).append(text)
    for file, texts in by_file.items():
        base = Path(file).name
        if base.startswith("requirements") and base.endswith(".txt"):
            for text in texts:
                line = text.strip()
                if not line or line.startswith(("#", "-")):
                    continue
                match = _REQ_LINE.match(line)
                if match and match.group("name").lower() != "python":
                    names.append(match.group("name"))
        elif base == "pyproject.toml":
            names.extend(_toml_dependency_names(texts))
    return list(dict.fromkeys(names))


class LicenseCheck(BaseCheck):
    name = "licenses"
    tool = "pip-licenses"
    levels = from_level("production")
    gate_from = "production"
    needs = "a Python project"
    hint = "pip install pip-licenses (in the project's environment)"

    def applies(self, repo: Path) -> bool:
        return is_python(repo)

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        new = new_python_dependencies(runner)
        if not new:
            return CheckResult(status="pass", detail="no new dependencies")
        tool = runner.resolve("pip-licenses")
        if tool is None:
            return runner.missing("pip-licenses", self.hint)
        result = await runner.run("licenses", f"{tool} --format=json --packages {quote(new)}")
        data = load_json(result.output)
        if result.timed_out or not isinstance(data, list):
            return tool_error(self, result)
        findings: list[Finding] = []
        seen = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            name, licence = str(item.get("Name", "")), str(item.get("License", "UNKNOWN"))
            seen.add(name.lower())
            upper = licence.upper()
            if any(word in upper for word in COPYLEFT) and "LGPL" not in upper:
                findings.append(
                    self.finding(
                        "major",
                        "maintainability",
                        f"new dependency {name} is {licence} (copyleft)",
                        suggestion="check the licence is acceptable, or pick another package",
                    )
                )
            elif "LGPL" in upper or upper in ("UNKNOWN", ""):
                findings.append(
                    self.finding("minor", "maintainability", f"new dependency {name}: {licence}")
                )
        missing = [n for n in new if n.lower() not in seen]
        note = f"; not installed, so not checked: {', '.join(missing)}" if missing else ""
        if any(f.severity == "major" for f in findings):
            return CheckResult(
                status="fail", detail=f"{len(findings)} licence concern(s){note}", findings=findings
            )
        return CheckResult(
            status="pass", detail=f"{len(new)} new dependency(ies) checked{note}", findings=findings
        )


# ---- scalable: containers and infrastructure as code ----------------------------------------

_IAC_SEVERITY: dict[str, Severity] = {
    "CRITICAL": "critical",
    "HIGH": "major",
    "MEDIUM": "minor",
    "LOW": "info",
}


class TrivyCheck(BaseCheck):
    """trivy downloads its vulnerability and policy databases on first use (network)."""

    name = "trivy"
    tool = "trivy"
    levels = from_level("scalable")
    whole_repo = True
    needs = "Dockerfiles or infrastructure-as-code files"
    hint = "https://trivy.dev (brew install trivy)"

    def applies(self, repo: Path) -> bool:
        return bool(iac_files(repo))

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        trivy = runner.resolve("trivy")
        if trivy is None:
            return runner.missing("trivy", self.hint)
        result = await runner.run(
            "trivy", f"{trivy} fs --scanners misconfig,secret --format json --quiet ."
        )
        data = load_json(result.output)
        if result.timed_out or not isinstance(data, dict):
            return tool_error(self, result)
        findings: list[Finding] = []
        for target in data.get("Results", []) or []:
            if not isinstance(target, dict):
                continue
            file = normalize(str(target.get("Target", "")), workdir)
            for item in [*(target.get("Misconfigurations") or []), *(target.get("Secrets") or [])]:
                if not isinstance(item, dict):
                    continue
                cause = item.get("CauseMetadata")
                cause = cause if isinstance(cause, dict) else {}
                findings.append(
                    self.finding(
                        _IAC_SEVERITY.get(str(item.get("Severity")).upper(), "minor"),
                        "security",
                        f"{item.get('ID') or item.get('RuleID', '')}: {item.get('Title', '')}",
                        file,
                        int_or_none(cause.get("StartLine") or item.get("StartLine")),
                        int_or_none(cause.get("EndLine") or item.get("EndLine")),
                        suggestion=str(item.get("Resolution", "")),
                    )
                )
        if (bad := exit_mismatch(self, result, found=bool(findings))) is not None:
            return bad
        if not findings:
            return CheckResult(status="pass", detail="no misconfigurations")
        return CheckResult(status="fail", detail=f"{len(findings)} issue(s)", findings=findings)


class HadolintCheck(BaseCheck):
    name = "hadolint"
    tool = "hadolint"
    levels = from_level("scalable")
    needs = "a Dockerfile"
    hint = "https://github.com/hadolint/hadolint (brew install hadolint)"

    def applies(self, repo: Path) -> bool:
        return any(Path(f).name.startswith("Dockerfile") for f in iac_files(repo))

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        hadolint = runner.resolve("hadolint")
        if hadolint is None:
            return runner.missing("hadolint", self.hint)
        files = [f for f in runner.changed() if Path(f).name.startswith("Dockerfile")]
        if not files:
            return CheckResult(status="pass", detail="no Dockerfiles changed")
        result = await runner.run("hadolint", f"{hadolint} -f json {quote(files)}")
        if result.timed_out:
            return tool_error(self, result)
        data = load_json(result.output)
        if not isinstance(data, list):
            if result.ok:
                return CheckResult(status="pass", detail="no issues")
            return tool_error(self, result)
        level: dict[str, Severity] = {"error": "major", "warning": "minor"}
        findings = [
            self.finding(
                level.get(str(item.get("level")), "info"),
                "maintainability",
                f"{item.get('code', '')}: {item.get('message', '')}",
                normalize(str(item.get("file", "")), workdir),
                int_or_none(item.get("line")),
            )
            for item in data
            if isinstance(item, dict)
        ]
        if (bad := exit_mismatch(self, result, found=bool(findings))) is not None:
            return bad
        if not findings:
            return CheckResult(status="pass", detail="no issues")
        return CheckResult(status="fail", detail=f"{len(findings)} issue(s)", findings=findings)


class CheckovCheck(BaseCheck):
    name = "checkov"
    tool = "checkov"
    levels = from_level("scalable")
    whole_repo = True
    needs = "Dockerfiles or infrastructure-as-code files"
    hint = "pip install checkov"

    def applies(self, repo: Path) -> bool:
        return bool(iac_files(repo))

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        checkov = runner.resolve("checkov")
        if checkov is None:
            return runner.missing("checkov", self.hint)
        result = await runner.run("checkov", f"{checkov} -d . --quiet --compact -o json")
        if result.timed_out:
            return tool_error(self, result)
        data = load_json(result.output)
        reports = data if isinstance(data, list) else [data]
        findings: list[Finding] = []
        for report in reports:
            if not isinstance(report, dict):
                continue
            results = report.get("results") or {}
            for item in results.get("failed_checks", []) if isinstance(results, dict) else []:
                if not isinstance(item, dict):
                    continue
                lines = item.get("file_line_range") or [None, None]
                findings.append(
                    self.finding(
                        _IAC_SEVERITY.get(str(item.get("severity")).upper(), "minor"),
                        "security",
                        f"{item.get('check_id', '')}: {item.get('check_name', '')}",
                        normalize(str(item.get("file_path", "")), workdir),
                        int_or_none(lines[0]),
                        int_or_none(lines[1] if len(lines) > 1 else None),
                        suggestion=str(item.get("guideline") or ""),
                    )
                )
        if (bad := exit_mismatch(self, result, found=bool(findings))) is not None:
            return bad
        if data is None:
            return tool_error(self, result)
        if not findings:
            return CheckResult(status="pass", detail="no failed checks")
        return CheckResult(
            status="fail", detail=f"{len(findings)} failed check(s)", findings=findings
        )
