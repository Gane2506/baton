"""Medium-level checks: secret scan, SAST (bandit, local Semgrep rules), dependency audits
(pip-audit, npm audit) and test coverage.
"""

from __future__ import annotations

import re
from pathlib import Path

from jev_agent.quality.base import (
    PY_SUFFIXES,
    BaseCheck,
    dependency_files_changed,
    from_level,
    int_or_none,
    is_python,
    load_json,
    mentions,
    tool_error,
)
from jev_agent.quality.checks_basic import pytest_command
from jev_agent.quality.models import CheckResult, Finding
from jev_agent.quality.tools import ToolRunner, added_lines, normalize, quote, tail
from jev_agent.roles.schemas import Severity
from jev_agent.runlog.redact import CREDENTIAL_PATTERNS, secret_env_values

# ---- medium: secrets, SAST, dependency audit, coverage ---------------------------------------

# The credential shapes every log redacts (one list, `runlog.redact`), plus a heuristic that
# only makes sense for code under review: a long literal assigned to a credential-like name.
SECRET_PATTERNS_NAMED: list[tuple[str, re.Pattern[str]]] = [
    (kind, pattern) for kind, pattern, _ in CREDENTIAL_PATTERNS
] + [
    (
        "hard-coded credential",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|passw(?:or)?d|access[_-]?token|auth[_-]?token)\b"
            r"\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"
        ),
    ),
]


def scan_added_lines(diff: str, env: dict[str, str] | None = None) -> list[tuple[str, int, str]]:
    """(file, line, kind) for each added line of `diff` that looks like it holds a secret: a
    well-known key pattern, or the current value of a secret environment variable."""
    values = secret_env_values(env if env is not None else _environ())
    hits: list[tuple[str, int, str]] = []
    for file, line, text in added_lines(diff):
        kind = next((name for name, p in SECRET_PATTERNS_NAMED if p.search(text)), None)
        if kind is None and any(value in text for value in values):
            kind = "the value of a secret environment variable"
        if kind is not None:
            hits.append((file, line, kind))
    return hits


def _environ() -> dict[str, str]:
    import os

    return dict(os.environ)


class SecretScanCheck(BaseCheck):
    """gitleaks when installed; else a built-in scan of the diff's added lines."""

    name = "secrets"
    tool = "gitleaks"
    levels = from_level("medium")
    whole_repo = True
    hint = "https://github.com/gitleaks/gitleaks (the built-in scan runs without it)"

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        gitleaks = runner.resolve("gitleaks")
        if gitleaks is None:
            if runner.required("gitleaks"):
                return runner.missing("gitleaks", self.hint)
            return self._builtin(runner)
        result = await runner.run(
            "gitleaks",
            f"{gitleaks} detect --no-git --no-banner --redact --verbose --exit-code 1 --source .",
        )
        findings: list[Finding] = []
        current: dict[str, str] = {}
        for raw in result.output.splitlines():
            key, sep, value = raw.partition(":")
            if not sep:
                continue
            key, value = key.strip(), value.strip()
            if key in ("RuleID", "File", "Line"):
                current[key] = value
            if "File" in current and "Line" in current:
                findings.append(self._secret(normalize(current["File"], workdir),
                                             int_or_none(current["Line"]),
                                             current.get("RuleID", "secret")))  # fmt: skip
                current = {}
        if result.timed_out or result.exit_code not in (0, 1):
            return tool_error(self, result)
        if result.exit_code == 0:
            return CheckResult(status="pass", detail="gitleaks: no leaks")
        if not findings:  # exit 1 means leaks were found, even when we cannot list them
            return CheckResult(
                status="fail",
                detail=f"gitleaks reported leaks (exit 1) but its output could not be parsed: "
                f"{tail(result.output, 300)}",
                findings=[
                    self.finding(
                        "critical",
                        "security",
                        "gitleaks reported leaks that could not be listed (output unparsed)",
                        suggestion="run gitleaks detect --no-git --verbose and remove them",
                    )
                ],
            )
        return CheckResult(
            status="fail", detail=f"gitleaks: {len(findings)} leak(s)", findings=findings
        )

    def _builtin(self, runner: ToolRunner) -> CheckResult:
        findings = [self._secret(f, line, kind) for f, line, kind in scan_added_lines(runner.diff)]
        how = "built-in scan of added lines (gitleaks not installed)"
        if not findings:
            return CheckResult(status="pass", detail=f"{how}: nothing found")
        return CheckResult(
            status="fail", detail=f"{how}: {len(findings)} possible secret(s)", findings=findings
        )

    def _secret(self, file: str, line: int | None, kind: str) -> Finding:
        return self.finding(
            "critical",
            "security",
            f"possible secret committed: {kind}",
            file,
            line,
            suggestion="remove it, read it from an environment variable, and rotate it",
        )


def scanner_errors(check: BaseCheck, errors: object, findings: list[Finding]) -> CheckResult:
    """A scanner's JSON report lists errors (files or rules it could not handle): the scan is
    incomplete, so status `error`; what it did find is kept and reported."""
    count = len(errors) if isinstance(errors, list) else 1
    first = errors[0] if isinstance(errors, list) and errors else errors
    return CheckResult(
        status="error",
        detail=f"{check.tool} reported {count} error(s), the scan is incomplete "
        f"({len(findings)} finding(s) so far): {str(first)[:300]}",
        findings=findings,
    )


class BanditCheck(BaseCheck):
    name = "bandit"
    tool = "bandit"
    levels = from_level("medium")
    needs = "a Python project"
    hint = "pip install bandit"

    def applies(self, repo: Path) -> bool:
        return is_python(repo)

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        bandit = runner.resolve("bandit")
        if bandit is None:
            return runner.missing("bandit", self.hint)
        files = runner.changed(PY_SUFFIXES)
        if not files:
            return CheckResult(status="pass", detail="no Python files changed")
        result = await runner.run("bandit", f"{bandit} -q -f json {quote(files)}")
        if result.timed_out:
            return tool_error(self, result)
        data = load_json(result.output)
        if not isinstance(data, dict):
            if result.ok:
                return CheckResult(status="pass", detail="no issues")
            return tool_error(self, result)
        severity: dict[str, Severity] = {"HIGH": "major", "MEDIUM": "minor", "LOW": "info"}
        findings = [
            self.finding(
                severity.get(str(issue.get("issue_severity")).upper(), "minor"),
                "security",
                f"{issue.get('test_id', '')} {issue.get('issue_text', '')}",
                normalize(str(issue.get("filename", "")), workdir),
                int_or_none(issue.get("line_number")),
                suggestion=str(issue.get("more_info", "")),
            )
            for issue in data.get("results", [])
            if isinstance(issue, dict)
        ]
        if data.get("errors"):  # files bandit could not scan
            return scanner_errors(self, data["errors"], findings)
        if not findings:
            return CheckResult(status="pass", detail="no issues")
        return CheckResult(status="fail", detail=f"{len(findings)} issue(s)", findings=findings)


SEMGREP_CONFIGS = (".semgrep.yml", ".semgrep.yaml", ".semgrep")


class SemgrepCheck(BaseCheck):
    """Only with a local rules file: `--config auto` downloads rules from the Semgrep registry
    (network, metrics), which jev never does on its own."""

    name = "semgrep"
    tool = "semgrep"
    levels = from_level("medium")
    needs = "local Semgrep rules (.semgrep.yml or .semgrep/); `--config auto` needs the network"
    hint = "pip install semgrep"

    def applies(self, repo: Path) -> bool:
        return any((repo / name).exists() for name in SEMGREP_CONFIGS)

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        semgrep = runner.resolve("semgrep")
        if semgrep is None:
            return runner.missing("semgrep", self.hint)
        config = next(name for name in SEMGREP_CONFIGS if (workdir / name).exists())
        files = runner.changed()
        if not files:
            return CheckResult(status="pass", detail="no files changed")
        result = await runner.run(
            "semgrep",
            f"{semgrep} scan --config {config} --json --metrics=off --disable-version-check "
            f"--quiet {quote(files)}",
        )
        data = load_json(result.output)
        if result.timed_out or not isinstance(data, dict):
            return tool_error(self, result)
        severity: dict[str, Severity] = {"ERROR": "major", "WARNING": "minor", "INFO": "info"}
        findings = []
        for item in data.get("results", []):
            if not isinstance(item, dict):
                continue
            extra = item.get("extra", {}) if isinstance(item.get("extra"), dict) else {}
            findings.append(
                self.finding(
                    severity.get(str(extra.get("severity")).upper(), "minor"),
                    "security",
                    f"{item.get('check_id', '')}: {extra.get('message', '')}",
                    normalize(str(item.get("path", "")), workdir),
                    int_or_none((item.get("start") or {}).get("line")),
                    int_or_none((item.get("end") or {}).get("line")),
                )
            )
        if data.get("errors"):  # rules or files Semgrep could not handle
            return scanner_errors(self, data["errors"], findings)
        if not findings:
            return CheckResult(status="pass", detail="no matches")
        return CheckResult(status="fail", detail=f"{len(findings)} match(es)", findings=findings)


class PipAuditCheck(BaseCheck):
    """Runs on a task only when it changed dependency files; always at integration. pip-audit
    queries a vulnerability database over the network."""

    name = "pip-audit"
    tool = "pip-audit"
    levels = from_level("medium")
    needs = "a Python project"
    hint = "pip install pip-audit"

    def applies(self, repo: Path) -> bool:
        return is_python(repo)

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        if runner.scope == "task" and not dependency_files_changed(runner):
            return CheckResult(status="pass", detail="no dependency files changed")
        audit = runner.resolve("pip-audit")
        if audit is None:
            return runner.missing("pip-audit", self.hint)
        target = "-r requirements.txt" if (workdir / "requirements.txt").is_file() else "."
        result = await runner.run("pip-audit", f"{audit} {target} -f json --progress-spinner off")
        if result.timed_out:
            return tool_error(self, result)
        data = load_json(result.output)
        deps = data.get("dependencies", []) if isinstance(data, dict) else data
        if not isinstance(deps, list):
            if result.ok:
                return CheckResult(status="pass", detail="no known vulnerabilities")
            return tool_error(self, result)
        findings = []
        for dep in deps:
            if not isinstance(dep, dict):
                continue
            for vuln in dep.get("vulns", []) or []:
                fixes = ", ".join(vuln.get("fix_versions", []) or []) or "none yet"
                findings.append(
                    self.finding(
                        "major",
                        "security",
                        f"{dep.get('name')} {dep.get('version')}: {vuln.get('id')}",
                        suggestion=f"upgrade (fixed in: {fixes})",
                    )
                )
        if not findings:
            return CheckResult(status="pass", detail="no known vulnerabilities")
        return CheckResult(
            status="fail", detail=f"{len(findings)} vulnerability(ies)", findings=findings
        )


class NpmAuditCheck(BaseCheck):
    name = "npm-audit"
    tool = "npm"
    levels = from_level("medium")
    needs = "a package-lock.json"
    hint = "install Node.js (npm)"

    def applies(self, repo: Path) -> bool:
        return (repo / "package-lock.json").is_file()

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        if runner.scope == "task" and not dependency_files_changed(runner):
            return CheckResult(status="pass", detail="no dependency files changed")
        npm = runner.resolve("npm")
        if npm is None:
            return runner.missing("npm", self.hint)
        result = await runner.run("npm-audit", f"{npm} audit --omit=dev --json")
        if result.timed_out:
            return tool_error(self, result)
        data = load_json(result.output)
        vulns = data.get("vulnerabilities") if isinstance(data, dict) else None
        if not isinstance(vulns, dict):
            if result.ok:
                return CheckResult(status="pass", detail="no known vulnerabilities")
            return tool_error(self, result)
        severity: dict[str, Severity] = {
            "critical": "critical",
            "high": "major",
            "moderate": "minor",
            "low": "info",
            "info": "info",
        }
        findings = [
            self.finding(
                severity.get(str(info.get("severity")), "minor"),
                "security",
                f"{name}: {info.get('severity')} vulnerability",
                suggestion="npm audit fix" if info.get("fixAvailable") else "no fix available yet",
            )
            for name, info in sorted(vulns.items())
            if isinstance(info, dict)
        ]
        if not findings:
            return CheckResult(status="pass", detail="no known vulnerabilities")
        return CheckResult(
            status="fail", detail=f"{len(findings)} vulnerable package(s)", findings=findings
        )


_COVERAGE_TOTAL = re.compile(r"^TOTAL\s.*?(?P<pct>\d+(?:\.\d+)?)%\s*$", re.MULTILINE)


class CoverageCheck(BaseCheck):
    """Line coverage of the whole suite, measured once on the result branch (pytest-cov)."""

    name = "coverage"
    tool = "pytest-cov"
    levels = from_level("medium")
    gate_from = "production"
    scopes = ("integration",)
    needs = "a Python project with pytest-cov in its dependencies"
    hint = "add pytest-cov to the project's dev dependencies"

    def applies(self, repo: Path) -> bool:
        return is_python(repo)

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        if not mentions(workdir, "pytest-cov", "pytest_cov"):
            return runner.missing("pytest-cov", self.hint)
        explicit = runner.settings.test_command
        if explicit and "pytest" in explicit:
            base = explicit
        else:
            prefix, missing = pytest_command(workdir, runner)
            if prefix is None:
                return runner.missing(missing or "pytest", "needed to measure coverage")
            base = f"{prefix} -q"
        result = await runner.run("coverage", f"{base} --cov --cov-report=term")
        totals = list(_COVERAGE_TOTAL.finditer(result.output))
        match = totals[-1] if totals else None  # the last TOTAL line
        if result.timed_out or match is None:
            return tool_error(self, result, "no coverage total in the output")
        pct, minimum = float(match.group("pct")), runner.settings.coverage_threshold()
        if pct >= minimum:
            return CheckResult(status="pass", detail=f"coverage {pct:g}% >= {minimum:g}%")
        return CheckResult(
            status="fail",
            detail=f"coverage {pct:g}% < {minimum:g}%",
            findings=[
                self.finding(
                    "major",
                    "tests",
                    f"line coverage {pct:g}% is below the {minimum:g}% minimum",
                    suggestion="add tests for the new code",
                )
            ],
        )
