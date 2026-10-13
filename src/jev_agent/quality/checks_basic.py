"""Basic-level checks: lint and format (ruff, ESLint, Prettier) and the full test suite."""

from __future__ import annotations

from pathlib import Path

from jev_agent.quality.base import (
    JS_SUFFIXES,
    PY_SUFFIXES,
    BaseCheck,
    failed_without_findings,
    has_any,
    is_python,
    package_json,
    parse_lines,
    pyproject,
)
from jev_agent.quality.models import CheckResult
from jev_agent.quality.tools import ToolRunner, normalize, quote, tail

# ---- basic: lint, format, tests --------------------------------------------------------------


class RuffCheck(BaseCheck):
    name = "ruff"
    tool = "ruff"
    gate_from = "basic"
    needs = "a Python project"
    hint = "pip install ruff, or add it to the project's dev dependencies"

    def applies(self, repo: Path) -> bool:
        return is_python(repo)

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        ruff = runner.resolve("ruff")
        if ruff is None:
            return runner.missing("ruff", self.hint)
        files = runner.changed(PY_SUFFIXES)
        if not files:
            return CheckResult(status="pass", detail="no Python files changed")
        lint = await runner.run(
            "ruff-check", f"{ruff} check --output-format=concise {quote(files)}"
        )
        fmt = await runner.run("ruff-format", f"{ruff} format --check {quote(files)}")
        findings = [
            self.finding("minor", "maintainability", msg, file, line)
            for file, line, msg in parse_lines(lint.output, workdir)
        ]
        for raw in fmt.output.splitlines():
            if raw.startswith("Would reformat:"):
                path = normalize(raw.split(":", 1)[1], workdir)
                findings.append(
                    self.finding(
                        "minor", "maintainability", "not formatted", path, suggestion="ruff format"
                    )
                )
        if lint.exit_code == 0 and fmt.exit_code == 0:
            return CheckResult(status="pass", detail=f"{len(files)} file(s) clean")
        if not findings:
            return failed_without_findings(self, lint.exit_code or fmt.exit_code, lint.output)
        return CheckResult(status="fail", detail=f"{len(findings)} problem(s)", findings=findings)


def _eslint_configured(repo: Path) -> bool:
    data = package_json(repo) or {}
    return "eslintConfig" in data or has_any(repo, "eslint.config.*", ".eslintrc*")


def _prettier_configured(repo: Path) -> bool:
    data = package_json(repo) or {}
    return "prettier" in data or has_any(repo, ".prettierrc*", "prettier.config.*")


class EslintCheck(BaseCheck):
    name = "eslint"
    tool = "eslint"
    gate_from = "basic"
    needs = "a JS/TS project with an ESLint config"
    hint = "npm install --save-dev eslint"

    def applies(self, repo: Path) -> bool:
        return package_json(repo) is not None and _eslint_configured(repo)

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        eslint = runner.resolve("eslint")
        if eslint is None:
            return runner.missing("eslint", self.hint)
        files = runner.changed(JS_SUFFIXES)
        if not files:
            return CheckResult(status="pass", detail="no JS/TS files changed")
        result = await runner.run("eslint", f"{eslint} --format unix {quote(files)}")
        if result.ok:
            return CheckResult(status="pass", detail=f"{len(files)} file(s) clean")
        findings = [
            self.finding("minor", "maintainability", msg, file, line)
            for file, line, msg in parse_lines(result.output, workdir)
        ]
        if not findings:
            return failed_without_findings(self, result.exit_code, result.output)
        return CheckResult(status="fail", detail=f"{len(findings)} problem(s)", findings=findings)


class PrettierCheck(BaseCheck):
    name = "prettier"
    tool = "prettier"
    gate_from = "basic"
    needs = "a JS/TS project with a Prettier config"
    hint = "npm install --save-dev prettier"

    def applies(self, repo: Path) -> bool:
        return package_json(repo) is not None and _prettier_configured(repo)

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        prettier = runner.resolve("prettier")
        if prettier is None:
            return runner.missing("prettier", self.hint)
        files = runner.changed((*JS_SUFFIXES, ".json", ".css", ".scss", ".md", ".html"))
        if not files:
            return CheckResult(status="pass", detail="no files Prettier formats changed")
        result = await runner.run("prettier", f"{prettier} --check {quote(files)}")
        if result.ok:
            return CheckResult(status="pass", detail=f"{len(files)} file(s) formatted")
        findings = [
            self.finding(
                "minor",
                "maintainability",
                "not formatted",
                normalize(raw.split("]", 1)[1], workdir),
                suggestion="prettier --write",
            )
            for raw in result.output.splitlines()
            if raw.startswith("[warn] ") and not raw.startswith("[warn] Code style issues")
        ]
        if not findings:
            return failed_without_findings(self, result.exit_code, result.output)
        return CheckResult(status="fail", detail=f"{len(findings)} file(s)", findings=findings)


def _has_pytest(repo: Path) -> bool:
    return (
        (repo / "tests").is_dir()
        or (repo / "test").is_dir()
        or (repo / "pytest.ini").is_file()
        or (repo / "conftest.py").is_file()
        or "pytest" in pyproject(repo).get("tool", {})
    )


def pytest_command(repo: Path, runner: ToolRunner) -> tuple[str | None, str | None]:
    """(command prefix that runs pytest, missing tool)."""
    if (repo / "uv.lock").is_file() and runner.available("uv"):
        return "uv run pytest", None
    pytest = runner.resolve("pytest")
    if pytest is not None:
        return pytest, None
    return None, "pytest"


def detect_test_command(repo: Path, runner: ToolRunner) -> tuple[str | None, str | None]:
    """(full test suite command, missing tool): `[quality]`/`[integration] test_command`,
    else pytest, `npm test`, `go test ./...` or `cargo test` when the project has them."""
    if runner.settings.test_command:
        return runner.settings.test_command, None
    if is_python(repo) and _has_pytest(repo):
        prefix, missing = pytest_command(repo, runner)
        return (None, missing) if prefix is None else (f"{prefix} -q", None)
    scripts = (package_json(repo) or {}).get("scripts", {})
    test_script = scripts.get("test") if isinstance(scripts, dict) else None
    if isinstance(test_script, str) and "no test specified" not in test_script:
        return ("npm test --silent", None) if runner.available("npm") else (None, "npm")
    if (repo / "go.mod").is_file():
        return ("go test ./...", None) if runner.available("go") else (None, "go")
    if (repo / "Cargo.toml").is_file():
        return ("cargo test", None) if runner.available("cargo") else (None, "cargo")
    return None, None


class TestSuiteCheck(BaseCheck):
    name = "tests"
    tool = None  # pytest, npm, go or cargo, depending on the project
    gate_from = "basic"
    needs = "a test suite"
    __test__ = False  # not a pytest test class

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        command, missing = detect_test_command(workdir, runner)
        if command is None:
            if missing is not None:
                return runner.missing(missing, "needed to run the test suite")
            return CheckResult(
                status="skipped",
                detail="no test suite detected; set [quality] test_command",
            )
        if command in runner.passed_commands:
            return CheckResult(status="pass", detail=f"`{command}` already passed in this step")
        result = await runner.run("tests", command)
        if result.ok:
            return CheckResult(status="pass", detail=f"`{command}` passed")
        timed_out = " (timed out)" if result.exit_code == -1 else ""
        return CheckResult(
            status="fail",
            detail=f"`{command}` failed with exit code {result.exit_code}{timed_out}: "
            f"{tail(result.output)}",
            findings=[
                self.finding(
                    "major",
                    "tests",
                    f"the test suite fails (`{command}`, exit {result.exit_code})",
                    suggestion="make the failing tests pass",
                )
            ],
        )
