"""The quality gate's pieces: levels, checks (with a fake command runner), policy, reports."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from jev_agent.core.commands import FakeCommandRunner
from jev_agent.quality.checks import (
    CHECKS,
    BanditCheck,
    CheckovCheck,
    CoverageCheck,
    EslintCheck,
    HadolintCheck,
    LicenseCheck,
    MypyCheck,
    NpmAuditCheck,
    PipAuditCheck,
    PrettierCheck,
    PyrightCheck,
    RuffCheck,
    SecretScanCheck,
    SemgrepCheck,
    TestSuiteCheck,
    TrivyCheck,
    TscCheck,
    checks_for,
    detect_test_command,
    new_python_dependencies,
    scan_added_lines,
)
from jev_agent.quality.levels import LEVELS, QualityLevel, at_least, parse_level
from jev_agent.quality.models import CheckResult, Finding, GateResult
from jev_agent.quality.policy import blocking_reasons
from jev_agent.quality.report import render_gate, render_run_review
from jev_agent.quality.tools import QualitySettings, ToolRunner, added_lines, changed_files
from jev_agent.roles.schemas import ChecklistItem, FileNote, QualityReview, ReviewFinding

DIFF = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 import os
+API_KEY = "sk-abcdefghijklmnop1234"
 print(os.name)
diff --git a/old.py b/old.py
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-gone
"""


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    repo.mkdir(exist_ok=True)
    return repo


def _runner(
    repo: Path,
    *,
    installed: tuple[str, ...] = (),
    exit_codes: dict[str, list[int]] | None = None,
    outputs: dict[str, list[str]] | None = None,
    diff: str = "",
    level: str = "production",
    scope: str = "task",
    required: tuple[str, ...] = (),
    test_command: str | None = None,
    min_coverage: float | None = None,
    passed: tuple[str, ...] = (),
) -> tuple[ToolRunner, FakeCommandRunner]:
    commands = FakeCommandRunner(exit_codes, outputs)
    settings = QualitySettings(
        level=parse_level(level),
        test_command=test_command,
        min_coverage=min_coverage,
        required_tools=required,
    )
    runner = ToolRunner(
        commands=commands,
        repo=repo,
        workdir=repo,
        log_dir=repo.parent / "logs",
        log_prefix="1.quality",
        settings=settings,
        scope=scope,
        diff=diff,
        passed_commands=passed,
        which=lambda tool: f"/bin/{tool}" if tool in installed else None,
    )
    return runner, commands


def _diff_for(*files: str) -> str:
    return "".join(
        f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n@@ -0,0 +1 @@\n+x = 1\n" for f in files
    )


# ---- levels ----------------------------------------------------------------------------------


def test_levels_are_cumulative() -> None:
    assert LEVELS == ("none", "basic", "medium", "production", "scalable")
    assert at_least("production", "medium") and not at_least("basic", "medium")
    assert parse_level(" Production ") == "production"
    with pytest.raises(ValueError, match="one of: none, basic"):
        parse_level("ultra")
    assert checks_for("none") == []
    names = {level: [c.name for c in checks_for(level)] for level in LEVELS}
    for lower, higher in itertools.pairwise(LEVELS):
        assert set(names[lower]) <= set(names[higher])
    assert {"ruff", "eslint", "prettier", "tests"} == set(names["basic"])
    assert {"secrets", "bandit", "semgrep", "pip-audit", "npm-audit", "coverage"} == set(
        names["medium"]
    ) - set(names["basic"])
    assert {"mypy", "pyright", "tsc", "licenses"} == set(names["production"]) - set(names["medium"])
    assert {"trivy", "hadolint", "checkov"} == set(names["scalable"]) - set(names["production"])
    assert len({c.name for c in CHECKS}) == len(CHECKS)


# ---- diff helpers ----------------------------------------------------------------------------


def test_changed_files_and_added_lines() -> None:
    assert changed_files(DIFF) == ["src/app.py"]
    assert added_lines(DIFF) == [("src/app.py", 2, 'API_KEY = "sk-abcdefghijklmnop1234"')]


# ---- basic -----------------------------------------------------------------------------------


async def test_ruff_pass_fail_missing(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"pyproject.toml": "", "src/a.py": "x=1\n", "README.md": ""})
    check = RuffCheck()
    assert check.applies(repo)
    diff = _diff_for("src/a.py", "README.md")

    runner, commands = _runner(repo, installed=("ruff",), diff=diff)
    result = await check.run(repo, runner)
    assert result.status == "pass"
    assert commands.calls[0][0] == "ruff check --output-format=concise src/a.py"

    lint = "ruff check --output-format=concise src/a.py"
    fmt = "ruff format --check src/a.py"
    runner, _ = _runner(
        repo,
        installed=("ruff",),
        diff=diff,
        exit_codes={lint: [1], fmt: [1]},
        outputs={
            lint: [f"{repo}/src/a.py:1:2: E225 Missing whitespace\nFound 1 error."],
            fmt: ["Would reformat: src/a.py\n1 file would be reformatted"],
        },
    )
    result = await check.run(repo, runner)
    assert result.status == "fail" and len(result.findings) == 2
    assert result.findings[0].file == "src/a.py" and result.findings[0].line_start == 1
    assert result.findings[0].severity == "minor"

    runner, _ = _runner(repo, diff=diff)
    assert (await check.run(repo, runner)).status == "skipped"
    runner, _ = _runner(repo, diff=diff, required=("ruff",))
    missing = await check.run(repo, runner)
    assert missing.status == "fail" and "required_tools" in missing.detail

    runner, _ = _runner(repo, installed=("ruff",), diff=_diff_for("README.md"))
    assert (await check.run(repo, runner)).detail == "no Python files changed"


async def test_tools_found_in_the_repo_venv_and_node_modules(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {"pyproject.toml": "", ".venv/bin/ruff": "", "node_modules/.bin/eslint": "", "a.py": ""},
    )
    runner, _ = _runner(repo)
    assert runner.resolve("ruff") == str(repo / ".venv/bin/ruff")
    assert runner.resolve("eslint") == str(repo / "node_modules/.bin/eslint")
    assert runner.resolve("mypy") is None


async def test_eslint_and_prettier(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {"package.json": "{}", "eslint.config.js": "", ".prettierrc": "{}", "src/a.ts": ""},
    )
    diff = _diff_for("src/a.ts")
    eslint, prettier = EslintCheck(), PrettierCheck()
    assert eslint.applies(repo) and prettier.applies(repo)
    assert not EslintCheck().applies(_repo(tmp_path / "x", {"package.json": "{}"}))
    cmd = "eslint --format unix src/a.ts"
    runner, _ = _runner(
        repo,
        installed=("eslint", "prettier"),
        diff=diff,
        exit_codes={cmd: [1], "prettier --check src/a.ts": [1]},
        outputs={
            cmd: [f"{repo}/src/a.ts:3:7: 'x' is unused [Error/no-unused-vars]\n\n1 problem"],
            "prettier --check src/a.ts": [
                "Checking formatting...\n[warn] src/a.ts\n[warn] Code style issues found."
            ],
        },
    )
    result = await eslint.run(repo, runner)
    assert result.status == "fail" and result.findings[0].location() == "src/a.ts:3"
    result = await prettier.run(repo, runner)
    assert result.status == "fail" and [f.file for f in result.findings] == ["src/a.ts"]


async def test_test_suite_detection_and_results(tmp_path: Path) -> None:
    py = _repo(tmp_path / "py", {"pyproject.toml": "", "uv.lock": "", "tests/test_a.py": ""})
    runner, _ = _runner(py, installed=("uv",))
    assert detect_test_command(py, runner) == ("uv run pytest -q", None)
    runner, _ = _runner(py)
    assert detect_test_command(py, runner) == (None, "pytest")
    runner, _ = _runner(py, test_command="make test")
    assert detect_test_command(py, runner) == ("make test", None)
    node = _repo(tmp_path / "node", {"package.json": '{"scripts": {"test": "vitest"}}'})
    runner, _ = _runner(node, installed=("npm",))
    assert detect_test_command(node, runner) == ("npm test --silent", None)
    go = _repo(tmp_path / "go", {"go.mod": "module x"})
    runner, _ = _runner(go, installed=("go",))
    assert detect_test_command(go, runner) == ("go test ./...", None)
    rust = _repo(tmp_path / "rust", {"Cargo.toml": ""})
    runner, _ = _runner(rust)
    assert detect_test_command(rust, runner) == (None, "cargo")
    empty = _repo(tmp_path / "empty", {"README.md": ""})
    runner, _ = _runner(empty)
    assert detect_test_command(empty, runner) == (None, None)

    check = TestSuiteCheck()
    runner, _ = _runner(empty)
    assert (await check.run(empty, runner)).status == "skipped"
    runner, _ = _runner(py, installed=("uv",), passed=("uv run pytest -q",))
    assert "already passed" in (await check.run(py, runner)).detail
    runner, commands = _runner(py, installed=("uv",), exit_codes={"uv run pytest -q": [1]})
    failed = await check.run(py, runner)
    assert failed.status == "fail" and failed.findings[0].category == "tests"
    assert commands.calls == [("uv run pytest -q", py)]
    runner, _ = _runner(py)  # pytest not installed
    assert (await check.run(py, runner)).status == "skipped"


# ---- medium ----------------------------------------------------------------------------------


async def test_secret_scan_builtin_and_gitleaks(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/app.py": ""})
    check = SecretScanCheck()
    runner, _ = _runner(repo, diff=DIFF)
    result = await check.run(repo, runner)
    assert result.status == "fail" and "built-in" in result.detail
    assert result.findings[0].severity == "critical" and result.findings[0].line_start == 2
    assert "sk-" not in result.findings[0].title  # never echo the secret

    runner, _ = _runner(repo, diff=_diff_for("src/app.py"))
    assert (await check.run(repo, runner)).status == "pass"
    runner, _ = _runner(repo, diff=DIFF, required=("gitleaks",))
    assert (await check.run(repo, runner)).status == "fail"

    cmd = "gitleaks detect --no-git --no-banner --redact --verbose --exit-code 1 --source ."
    out = "Finding: REDACTED\nRuleID: generic-api-key\nFile: src/app.py\nLine: 2\n\nleaks found: 1"
    runner, _ = _runner(
        repo, installed=("gitleaks",), diff=DIFF, exit_codes={cmd: [1]}, outputs={cmd: [out]}
    )
    result = await check.run(repo, runner)
    assert result.status == "fail" and result.findings[0].location() == "src/app.py:2"
    assert "generic-api-key" in result.findings[0].title


def test_scan_added_lines_finds_env_secret_values() -> None:
    diff = _diff_for("a.py").replace("+x = 1", "+token = 'supersecretvalue99'")
    assert scan_added_lines(diff, {"MY_TOKEN": "supersecretvalue99"})[0][2].startswith("the value")
    assert scan_added_lines(_diff_for("a.py"), {}) == []


async def test_bandit(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"pyproject.toml": "", "a.py": ""})
    cmd = "bandit -q -f json a.py"
    report = {
        "results": [
            {
                "filename": "./a.py",
                "line_number": 4,
                "issue_severity": "HIGH",
                "issue_text": "subprocess call with shell=True",
                "test_id": "B602",
            },
            {"filename": "a.py", "line_number": 9, "issue_severity": "LOW", "issue_text": "x"},
        ]
    }
    runner, _ = _runner(
        repo,
        installed=("bandit",),
        diff=_diff_for("a.py"),
        exit_codes={cmd: [1]},
        outputs={cmd: [json.dumps(report)]},
    )
    result = await BanditCheck().run(repo, runner)
    assert result.status == "fail"
    assert [(f.severity, f.file, f.line_start) for f in result.findings] == [
        ("major", "a.py", 4),
        ("info", "a.py", 9),
    ]
    assert all(f.category == "security" for f in result.findings)


async def test_semgrep_needs_local_rules(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"a.py": ""})
    check = SemgrepCheck()
    assert not check.applies(repo) and "network" in check.needs
    (repo / ".semgrep.yml").write_text("rules: []")
    assert check.applies(repo)
    cmd = (
        "semgrep scan --config .semgrep.yml --json --metrics=off --disable-version-check "
        "--quiet a.py"
    )
    out: Any = {
        "results": [
            {
                "check_id": "no-eval",
                "path": "a.py",
                "start": {"line": 2},
                "end": {"line": 3},
                "extra": {"severity": "ERROR", "message": "eval on input"},
            }
        ]
    }
    runner, _ = _runner(
        repo, installed=("semgrep",), diff=_diff_for("a.py"), outputs={cmd: [json.dumps(out)]}
    )
    result = await check.run(repo, runner)
    assert result.status == "fail" and result.findings[0].location() == "a.py:2-3"


async def test_dependency_audits(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"pyproject.toml": "", "package-lock.json": "{}", "a.py": ""})
    pip = PipAuditCheck()
    runner, commands = _runner(repo, installed=("pip-audit",), diff=_diff_for("a.py"))
    assert (await pip.run(repo, runner)).detail == "no dependency files changed"
    assert commands.calls == []
    cmd = "pip-audit . -f json --progress-spinner off"
    out: Any = {
        "dependencies": [
            {
                "name": "jinja2",
                "version": "2.0",
                "vulns": [{"id": "PYSEC-1", "fix_versions": ["3.1"]}],
            },
            {"name": "ok", "version": "1", "vulns": []},
        ]
    }
    runner, _ = _runner(
        repo,
        installed=("pip-audit",),
        scope="integration",
        exit_codes={cmd: [1]},
        outputs={cmd: [json.dumps(out)]},
    )
    result = await pip.run(repo, runner)
    assert result.status == "fail" and "PYSEC-1" in result.findings[0].title
    assert "3.1" in result.findings[0].suggestion

    npm = NpmAuditCheck()
    assert npm.applies(repo)
    cmd = "npm audit --omit=dev --json"
    out = {"vulnerabilities": {"lodash": {"severity": "critical", "fixAvailable": True}}}
    runner, _ = _runner(
        repo,
        installed=("npm",),
        diff=_diff_for("package-lock.json"),
        exit_codes={cmd: [1]},
        outputs={cmd: [json.dumps(out)]},
    )
    result = await npm.run(repo, runner)
    assert result.findings[0].severity == "critical"
    runner, _ = _runner(repo, diff=_diff_for("package.json"))
    assert (await npm.run(repo, runner)).status == "skipped"


async def test_coverage(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"pyproject.toml": "[dependency-groups]\ndev = ['pytest-cov']\n"})
    check = CoverageCheck()
    assert check.scopes == ("integration",)
    cmd = "pytest -q --cov --cov-report=term"
    total = "Name  Stmts  Miss  Cover\nTOTAL  100  30  70%\n"
    runner, _ = _runner(repo, installed=("pytest",), level="medium", outputs={cmd: [total]})
    assert (await check.run(repo, runner)).status == "pass"  # 70 >= 60 at medium
    runner, _ = _runner(repo, installed=("pytest",), level="production", outputs={cmd: [total]})
    result = await check.run(repo, runner)
    assert result.status == "fail" and "70% < 80%" in result.detail
    runner, _ = _runner(
        repo, installed=("pytest",), min_coverage=65, level="production", outputs={cmd: [total]}
    )
    assert (await check.run(repo, runner)).status == "pass"
    runner, _ = _runner(repo, installed=("pytest",), outputs={cmd: ["no total here"]})
    assert (await check.run(repo, runner)).status == "error"
    plain = _repo(tmp_path / "plain", {"pyproject.toml": ""})
    runner, _ = _runner(plain, installed=("pytest",))
    assert "pytest-cov" in (await check.run(plain, runner)).detail


# ---- production --------------------------------------------------------------------------------


async def test_type_checkers_only_judge_changed_files(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "pyproject.toml": "[tool.mypy]\nstrict = true\n[tool.pyright]\n",
            "a.py": "",
            "b.py": "",
            "tsconfig.json": "{}",
            "c.ts": "",
        },
    )
    diff = _diff_for("a.py", "c.ts")
    cmd = "mypy --no-error-summary --no-pretty"
    out = (
        "a.py:3: error: Incompatible types [assignment]\nb.py:1: error: old [misc]\na.py:3: note: x"
    )
    runner, _ = _runner(
        repo, installed=("mypy",), diff=diff, exit_codes={cmd: [1]}, outputs={cmd: [out]}
    )
    result = await MypyCheck().run(repo, runner)
    assert result.status == "fail" and len(result.findings) == 1
    assert "1 error(s) in files this work did not change" in result.detail
    out = "b.py:1: error: old [misc]"
    runner, _ = _runner(
        repo, installed=("mypy",), diff=diff, exit_codes={cmd: [1]}, outputs={cmd: [out]}
    )
    assert (await MypyCheck().run(repo, runner)).status == "pass"

    out = f"  {repo}/a.py:2:5 - error: bad\n  {repo}/a.py:4:1 - warning: meh\n1 error"
    runner, _ = _runner(
        repo,
        installed=("pyright",),
        diff=diff,
        exit_codes={"pyright": [1]},
        outputs={"pyright": [out]},
    )
    result = await PyrightCheck().run(repo, runner)
    assert result.status == "fail" and [f.line_start for f in result.findings] == [2]

    cmd = "tsc --noEmit --pretty false"
    out = "c.ts(5,3): error TS2322: Type 'string' is not assignable."
    runner, _ = _runner(
        repo, installed=("tsc",), diff=diff, exit_codes={cmd: [2]}, outputs={cmd: [out]}
    )
    result = await TscCheck().run(repo, runner)
    assert result.status == "fail" and result.findings[0].location() == "c.ts:5"


async def test_licences_of_new_dependencies(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"pyproject.toml": "", "requirements.txt": ""})
    diff = (
        "diff --git a/pyproject.toml b/pyproject.toml\n--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n@@ -1,2 +1,4 @@\n dependencies = [\n"
        '+    "gplthing>=1.0",\n+    "nice[extra]~=2",\n ]\n'
        "diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n"
        "+++ b/requirements.txt\n@@ -0,0 +1,2 @@\n+# comment\n+other==1\n"
    )
    runner, _ = _runner(repo, diff=diff)
    assert new_python_dependencies(runner) == ["gplthing", "nice", "other"]
    cmd = "pip-licenses --format=json --packages gplthing nice other"
    out: Any = [
        {"Name": "gplthing", "License": "GNU General Public License v3 (GPLv3)"},
        {"Name": "nice", "License": "MIT"},
    ]
    runner, _ = _runner(
        repo, installed=("pip-licenses",), diff=diff, outputs={cmd: [json.dumps(out)]}
    )
    result = await LicenseCheck().run(repo, runner)
    assert result.status == "fail" and "copyleft" in result.findings[0].title
    assert "not checked: other" in result.detail
    runner, _ = _runner(repo, diff=_diff_for("a.py"))
    assert (await LicenseCheck().run(repo, runner)).detail == "no new dependencies"


def test_new_python_dependencies_single_and_multi_line_arrays(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"pyproject.toml": "", "requirements.txt": ""})
    diff = (
        "diff --git a/pyproject.toml b/pyproject.toml\n--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n@@ -1,5 +1,7 @@\n dependencies = [\n"
        '+    "gplthing>=1.0",\n     "kept",\n+    "nice[extra]~=2",\n ]\n'
        '+single = ["one", "two==2"]\n'
        "diff --git a/pyproject.toml b/other.toml\n--- a/other.toml\n+++ b/other.toml\n"
        '@@ -0,0 +1 @@\n+dependencies = ["ignored-file"]\n'
    )
    runner, _ = _runner(repo, diff=diff)
    assert new_python_dependencies(runner) == ["gplthing", "nice"]


async def test_new_python_dependencies_replaced_single_line_array(tmp_path: Path) -> None:
    # A copyleft dependency added on a single-line array must not pass as "no new
    # dependencies" (issue #50).
    repo = _repo(tmp_path, {"pyproject.toml": "", "requirements.txt": ""})
    diff = (
        "diff --git a/pyproject.toml b/pyproject.toml\n--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n@@ -1 +1 @@\n-dependencies = []\n"
        '+dependencies = ["gplthing>=1.0"]\n'
    )
    runner, _ = _runner(repo, installed=("pip-licenses",), diff=diff)
    out = [{"Name": "gplthing", "License": "GNU General Public License v3 (GPLv3)"}]
    runner, _ = _runner(
        repo,
        installed=("pip-licenses",),
        diff=diff,
        outputs={"pip-licenses --format=json --packages gplthing": [json.dumps(out)]},
    )
    result = await LicenseCheck().run(repo, runner)
    assert result.status == "fail" and "copyleft" in result.findings[0].title


# ---- scalable --------------------------------------------------------------------------------


async def test_container_and_iac_scans(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"Dockerfile": "FROM x", "infra/main.tf": ""})
    assert TrivyCheck().applies(repo) and HadolintCheck().applies(repo)
    assert CheckovCheck().applies(repo)
    assert not TrivyCheck().applies(_repo(tmp_path / "plain", {"a.py": ""}))
    diff = _diff_for("Dockerfile")

    cmd = "trivy fs --scanners misconfig,secret --format json --quiet ."
    out: Any = {
        "Results": [
            {
                "Target": "Dockerfile",
                "Misconfigurations": [
                    {
                        "ID": "DS002",
                        "Title": "root user",
                        "Severity": "HIGH",
                        "CauseMetadata": {"StartLine": 1},
                    }
                ],
            }
        ]
    }
    runner, _ = _runner(repo, installed=("trivy",), diff=diff, outputs={cmd: [json.dumps(out)]})
    result = await TrivyCheck().run(repo, runner)
    assert (
        result.findings[0].severity == "major" and result.findings[0].location() == "Dockerfile:1"
    )

    cmd = "hadolint -f json Dockerfile"
    out = [
        {"file": "Dockerfile", "line": 1, "code": "DL3007", "message": "latest", "level": "warning"}
    ]
    runner, _ = _runner(
        repo,
        installed=("hadolint",),
        diff=diff,
        exit_codes={cmd: [1]},
        outputs={cmd: [json.dumps(out)]},
    )
    result = await HadolintCheck().run(repo, runner)
    assert result.status == "fail" and result.findings[0].severity == "minor"

    cmd = "checkov -d . --quiet --compact -o json"
    out = {
        "results": {
            "failed_checks": [
                {
                    "check_id": "CKV_AWS_1",
                    "check_name": "open bucket",
                    "file_path": "/infra/main.tf",
                    "file_line_range": [3, 9],
                    "severity": "CRITICAL",
                }
            ]
        }
    }
    runner, _ = _runner(repo, installed=("checkov",), diff=diff, outputs={cmd: [json.dumps(out)]})
    result = await CheckovCheck().run(repo, runner)
    assert result.findings[0].location() == "infra/main.tf:3-9"
    assert result.findings[0].severity == "critical"


def test_iac_files_finds_nested_dockerfiles(tmp_path: Path) -> None:
    """Dockerfiles deeper than one level count for the container scans (issue #204)."""
    from jev_agent.quality.base import iac_files

    repo = _repo(tmp_path, {"services/api/Dockerfile": "FROM x", "Dockerfile": "FROM x"})
    assert "services/api/Dockerfile" in iac_files(repo)


async def test_scanner_nonzero_exit_with_valid_empty_json_is_an_error(tmp_path: Path) -> None:
    """A scanner that errors while emitting valid empty JSON must not silently pass
    (issue #204 I-1): exit 1 with nothing listed, and any non-findings exit code,
    are tool errors, not clean scans."""
    cases: list[tuple[Any, dict[str, str], str, str, str, int, str]] = [
        (
            BanditCheck(),
            _PY,
            "bandit",
            "bandit -q -f json a.py",
            _diff_for("a.py"),
            1,
            '{"results": []}',
        ),
        (
            BanditCheck(),
            _PY,
            "bandit",
            "bandit -q -f json a.py",
            _diff_for("a.py"),
            2,
            '{"results": []}',
        ),
        (
            SemgrepCheck(),
            {"a.py": "", ".semgrep.yml": "rules: []"},
            "semgrep",
            _SEMGREP_CMD,
            _diff_for("a.py"),
            2,
            '{"results": []}',
        ),
        (
            TrivyCheck(),
            _IAC,
            "trivy",
            "trivy fs --scanners misconfig,secret --format json --quiet .",
            _diff_for("Dockerfile"),
            1,
            '{"Results": []}',
        ),
        (
            CheckovCheck(),
            _IAC,
            "checkov",
            "checkov -d . --quiet --compact -o json",
            _diff_for("Dockerfile"),
            1,
            '{"results": {"failed_checks": []}}',
        ),
        (
            HadolintCheck(),
            _IAC,
            "hadolint",
            "hadolint -f json Dockerfile",
            _diff_for("Dockerfile"),
            1,
            "[]",
        ),
        (
            NpmAuditCheck(),
            {"package-lock.json": "{}"},
            "npm",
            "npm audit --omit=dev --json",
            _diff_for("package-lock.json"),
            1,
            '{"vulnerabilities": {}}',
        ),
    ]
    for check, files, tool, cmd, diff, code, output in cases:
        repo = _repo(tmp_path, files)
        runner, _ = _runner(
            repo,
            installed=(tool,),
            exit_codes={cmd: [code]},
            outputs={cmd: [output]},
            diff=diff,
        )
        result = await check.run(repo, runner)
        assert result.status == "error", (check.name, code, result)
        assert result.findings == [] and f"exit {code}" in result.detail


async def test_scanner_clean_and_findings_exits_unchanged(tmp_path: Path) -> None:
    """Exit 0 with empty JSON still passes, and exit 1 with findings still fails."""
    repo = _repo(tmp_path, _PY)
    cmd = "bandit -q -f json a.py"
    runner, _ = _runner(
        repo,
        installed=("bandit",),
        exit_codes={cmd: [0]},
        outputs={cmd: ['{"results": []}']},
        diff=_diff_for("a.py"),
    )
    assert (await BanditCheck().run(repo, runner)).status == "pass"
    repo = _repo(tmp_path, _IAC)
    cmd = "trivy fs --scanners misconfig,secret --format json --quiet ."
    runner, _ = _runner(
        repo,
        installed=("trivy",),
        exit_codes={cmd: [0]},
        outputs={cmd: ['{"Results": []}']},
        diff=_diff_for("Dockerfile"),
    )
    assert (await TrivyCheck().run(repo, runner)).status == "pass"


# ---- policy ----------------------------------------------------------------------------------


def _finding(severity: str, category: str) -> Finding:
    return Finding.model_validate(
        {"source": "x", "severity": severity, "category": category, "title": "t"}
    )


@pytest.mark.parametrize(
    ("level", "finding", "blocks"),
    [
        ("basic", ("critical", "bug"), True),
        ("medium", ("major", "security"), False),
        ("production", ("major", "security"), True),
        ("production", ("major", "performance"), False),
        ("scalable", ("major", "performance"), True),
        ("scalable", ("major", "maintainability"), False),
        ("basic", ("minor", "security"), False),
    ],
)
def test_policy_by_finding(level: str, finding: tuple[str, str], blocks: bool) -> None:
    reasons = blocking_reasons(parse_level(level), [], [_finding(*finding)])
    assert bool(reasons) is blocks


def test_policy_by_check_status() -> None:
    failed = CheckResult(status="fail", detail="2 problems", name="ruff", required=True)
    advisory = CheckResult(status="fail", detail="cov low", name="coverage", required=False)
    assert blocking_reasons("basic", [failed], []) == ["ruff failed: 2 problems"]
    assert blocking_reasons("medium", [advisory], []) == []
    skipped = CheckResult(status="skipped", name="mypy", required=True)  # not installed
    assert blocking_reasons("production", [skipped], []) == []
    errored = CheckResult(status="error", detail="crashed", name="mypy", required=True)
    assert blocking_reasons("production", [errored], []) == ["mypy could not run: crashed"]


# ---- reports ---------------------------------------------------------------------------------


def _gate(level: str = "scalable", blocked: bool = True, **extra: object) -> GateResult:
    review = QualityReview(
        summary="Adds search | with a bug.",
        walkthrough=[FileNote(file="src/search.py", change="new search()")],
        findings=[
            ReviewFinding(
                severity="major",
                category="security",
                title="SQL built from input",
                file="src/search.py",
                line_start=10,
                line_end=12,
                suggestion="use parameters",
            )
        ],
        security_checklist=[ChecklistItem(item="Injection", status="concern", note="line 10")],
        scalability_checklist=[ChecklistItem(item="N+1", status="ok")],
        threat_model=["SQL injection -> parameterised queries (missing)"],
    )
    data: dict[str, object] = {
        "level": level,
        "scope": "task",
        "task_id": "T1",
        "number": 2,
        "checks": [
            CheckResult(
                status="fail",
                detail="1 problem",
                name="ruff",
                findings=[_finding("minor", "maintainability")],
            ),
            CheckResult(status="skipped", detail="bandit is not installed", name="bandit"),
        ],
        "review": review,
        "review_status": "done",
        "blocked": blocked,
        "reasons": ["1 major security finding(s)"] if blocked else [],
        **extra,
    }
    return GateResult.model_validate(data)


def test_gate_result_helpers() -> None:
    gate = _gate()
    assert gate.label == "T1-2"
    assert gate.counts()["major"] == 1 and gate.counts()["minor"] == 1
    top = gate.top_findings()
    assert top[0] == "1 major security finding(s)"
    assert top[1].startswith("major security src/search.py:10-12: SQL built from input")
    assert "blocked (1 major, 1 minor)" in gate.summary_line()
    integration = _gate(scope="integration", task_id=None, number=1)
    assert integration.label == "integration-1"


def test_finding_compact_never_exceeds_tiny_limits() -> None:
    """`compact(limit)` with `limit < 4` clamps instead of mis-slicing (`line[:limit-3]`
    with a negative index used to return nearly the whole line plus "...")."""
    finding = Finding(
        source="ruff", severity="major", category="security", title="a very long finding title"
    )
    assert finding.compact(limit=0) == ""
    assert finding.compact(limit=1) == "m"
    assert finding.compact(limit=2) == "ma"
    assert finding.compact(limit=3) == "maj"
    assert finding.compact(limit=4) == "m..."
    for limit in range(0, 10):
        assert len(finding.compact(limit=limit)) <= limit


def test_short_never_exceeds_tiny_limits() -> None:
    """`policy._short` shares the same clamp (its `limit` parameter accepts any value)."""
    from jev_agent.quality.policy import _short

    assert _short("a very long detail", limit=0) == ""
    assert _short("a very long detail", limit=2) == "a "
    assert _short("a very long detail", limit=4) == "a..."
    for limit in range(0, 10):
        assert len(_short("a very long detail", limit=limit)) <= limit


def test_render_gate_sections_follow_the_level() -> None:
    text = render_gate(_gate())
    for heading in ("## Summary", "## Walkthrough", "## Findings", "### Major (1)", "## Checks"):
        assert heading in text
    assert "## Security checklist" in text and "## Scalability" in text
    assert "## Threat model" in text and "SQL injection -> parameterised" in text
    assert "Adds search \\| with a bug." not in text  # summary is prose, not a table cell
    assert "| `src/search.py` | new search() |" in text
    assert "`src/search.py:10-12`" in text and "Suggestion: use parameters" in text
    assert "| ruff | **fail** | 1 problem |" in text
    assert "not a substitute" not in text and "human security review" in text
    basic = render_gate(_gate(level="basic", blocked=False))
    assert "## Security checklist" not in basic and "**passed**" in basic
    production = render_gate(_gate(level="production"))
    assert "## Security checklist" in production and "## Threat model" not in production


def test_render_run_review_uses_the_latest_gate_per_task() -> None:
    old = _gate(level="production", number=1)
    new = _gate(level="production", number=2, blocked=False, review=None, review_status="error")
    integration = _gate(
        level="production", scope="integration", task_id=None, number=1, blocked=False
    )
    text = render_run_review("r1", "production", [old, new, integration])
    assert "| T1-2 | passed |" in text and "T1-1" not in text
    assert "| integration-1 | passed |" in text and "every gate passed" in text
    assert "[integration-1] **SQL built from input**" in text
    assert "## Security checklist" in text
    assert "No quality gate has run yet" in render_run_review("r1", "basic", [])


def test_render_run_review_shows_code_grade_routing() -> None:
    """The run review surfaces the grade, its confidence, and the
    human_review routing (issue #267): the dashboard's Quality review
    section and `jev quality show` used to show a blocked gate with no
    trace of the grade behind it."""
    graded = _gate(
        level="production",
        blocked=True,
        code_grade={
            "status": "graded",
            "model": "S4MPL3BI4S/Coding_Decision_Agent",
            "code_quality": 2,
            "instruction_followed": False,
            "likely_correct": False,
            "merge_action": "request_changes",
            "answer_confidence": {"merge_action": 0.44},
            "gate": {
                "decision": "human_review",
                "reason": (
                    "coding decision agent is not confident enough to merge "
                    "(merge_action=request_changes at 0.44 < 0.70); "
                    "a human must decide"
                ),
                "merge_action": "request_changes",
                "confidence": 0.44,
            },
        },
    )
    unavailable = _gate(
        level="production",
        scope="integration",
        task_id=None,
        number=1,
        blocked=False,
        code_grade={
            "status": "unavailable",
            "detail": "the Coding Decision Agent is not installed",
        },
    )
    text = render_run_review("r1", "production", [graded, unavailable])
    assert "## Code orchestrator" in text
    assert "human_review" in text and "0.44" in text and "request_changes" in text
    assert "code_quality 2/4" in text
    assert "not graded (unavailable:" in text
    # No grade at all: the section stays out of the review.
    assert "## Code orchestrator" not in render_run_review("r1", "production", [_gate()])


# ---- store -----------------------------------------------------------------------------------


def test_migration_6_and_quality_reports(tmp_path: Path) -> None:
    import sqlite3

    from jev_agent.core.migrations import MIGRATIONS, SCHEMA_VERSION
    from jev_agent.core.models import Idea
    from jev_agent.core.store import Store

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("".join(MIGRATIONS[:5]) + "\nPRAGMA user_version = 5;")
        conn.execute(
            "INSERT INTO runs (id, idea_json, state, created_at, updated_at)"
            " VALUES ('old', ?, 'DONE', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (Idea(text="x", repo_path=tmp_path).model_dump_json(),),
        )
    with Store.open(db_path) as store:
        assert SCHEMA_VERSION >= 6
        assert store.get_run("old").quality_level == "none"
        run = store.create_run(Idea(text="y", repo_path=tmp_path), quality_level="medium")
        assert store.get_run(run.id).quality_level == "medium"
        store.set_run_quality(run.id, "scalable")
        assert store.get_run(run.id).quality_level == "scalable"
        with pytest.raises(ValueError):
            store.set_run_quality(run.id, "ultra")  # type: ignore[arg-type]
        gate = _gate()
        store.add_quality_report(run.id, gate, tmp_path / "T1-2.md")
        (record,) = store.list_quality_reports(run.id)
        assert record.result == gate and record.report_path == tmp_path / "T1-2.md"
        item = store.add_queue_item("z", quality="basic")
        assert store.get_queue_item(item.id).quality == "basic"
        assert store.add_queue_item("w").quality is None


# ---- the gate's handling of one check -----------------------------------------------------------


class _Broken:
    name: str = "broken"
    tool: str | None = None
    levels: tuple[QualityLevel, ...] = LEVELS[1:]
    gate_from: QualityLevel | None = "basic"
    scopes: tuple[str, ...] = ("task", "integration")
    needs = ""
    hint = ""
    whole_repo = False

    def applies(self, repo: Path) -> bool:
        return True

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        raise RuntimeError("bug in the check")


class _WholeRepo(_Broken):
    name = "scanner"
    tool = "scanner"
    gate_from = None
    whole_repo = True

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        old = _finding("critical", "security").model_copy(update={"file": "old.py"})
        return CheckResult(status="fail", findings=[old])


async def test_gate_runs_checks_safely(tmp_path: Path) -> None:
    from jev_agent.quality.gate import _run_check

    repo = _repo(tmp_path, {"a.py": ""})
    runner, _ = _runner(repo, diff=_diff_for("a.py"), required=("scanner",))
    broken = await _run_check(_Broken(), "task", "basic", runner)
    assert broken.status == "error" and "RuntimeError: bug in the check" in broken.detail
    assert blocking_reasons("basic", [broken], [])  # a required check that errors blocks
    assert broken.name == "broken" and broken.required
    filtered = await _run_check(_WholeRepo(), "task", "basic", runner)
    assert filtered.status == "pass" and filtered.findings == []
    assert "1 finding(s) in unchanged files ignored" in filtered.detail
    assert filtered.required  # its tool is in required_tools
    only_integration = await _run_check(CoverageCheck(), "task", "medium", runner)
    assert only_integration.detail == "runs on the result branch only"
    not_here = await _run_check(MypyCheck(), "task", "production", runner)
    assert not_here.detail.startswith("not applicable: needs a mypy config")


# ---- tool errors: crashes, timeouts, unparseable output (#17, #18) -----------------------------

_PY_REQS_DIFF = (
    "diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n"
    "+++ b/requirements.txt\n@@ -0,0 +1 @@\n+other==1\n"
)
_SEMGREP_CMD = (
    "semgrep scan --config .semgrep.yml --json --metrics=off --disable-version-check --quiet a.py"
)
_GITLEAKS_CMD = "gitleaks detect --no-git --no-banner --redact --verbose --exit-code 1 --source ."
_PY = {"pyproject.toml": "[tool.mypy]\n", "a.py": ""}
_IAC = {"Dockerfile": "FROM x"}

# (check, repo files, tool, command, exit code, output, scope, diff)
_BROKEN_TOOLS: list[tuple[Any, dict[str, str], str, str, int, str, str, str]] = [
    (RuffCheck(), _PY, "ruff", "ruff check --output-format=concise a.py", 2,
     "error: Failed to parse pyproject.toml", "task", _diff_for("a.py")),
    (EslintCheck(), {"package.json": "{}", "eslint.config.js": "", "a.ts": ""}, "eslint",
     "eslint --format unix a.ts", 2, "Oops! Something went wrong!", "task", _diff_for("a.ts")),
    (PrettierCheck(), {"package.json": "{}", ".prettierrc": "{}", "a.ts": ""}, "prettier",
     "prettier --check a.ts", 2, "[error] Invalid configuration", "task", _diff_for("a.ts")),
    (SecretScanCheck(), {"a.py": ""}, "gitleaks", _GITLEAKS_CMD, 126, "permission denied",
     "task", _diff_for("a.py")),
    (BanditCheck(), _PY, "bandit", "bandit -q -f json a.py", 2, "Traceback: boom", "task",
     _diff_for("a.py")),
    (SemgrepCheck(), {"a.py": "", ".semgrep.yml": "rules: []"}, "semgrep", _SEMGREP_CMD, 2,
     "Fatal error: invalid rules", "task", _diff_for("a.py")),
    (PipAuditCheck(), _PY, "pip-audit", "pip-audit . -f json --progress-spinner off", 1,
     "ERROR: connection refused", "integration", ""),
    (NpmAuditCheck(), {"package-lock.json": "{}"}, "npm", "npm audit --omit=dev --json", 1,
     "npm ERR! network", "integration", ""),
    (CoverageCheck(), {"pyproject.toml": "dev = ['pytest-cov']"}, "pytest",
     "pytest -q --cov --cov-report=term", 4, "error: unrecognized arguments: --cov",
     "integration", ""),
    (MypyCheck(), _PY, "mypy", "mypy --no-error-summary --no-pretty", 2,
     "mypy: can't read file 'x'", "task", _diff_for("a.py")),
    (LicenseCheck(), {"pyproject.toml": "", "requirements.txt": ""}, "pip-licenses",
     "pip-licenses --format=json --packages other", 1, "ModuleNotFoundError", "task",
     _PY_REQS_DIFF),
    (TrivyCheck(), _IAC, "trivy", "trivy fs --scanners misconfig,secret --format json --quiet .",
     1, "FATAL db download failed", "task", _diff_for("Dockerfile")),
    (HadolintCheck(), _IAC, "hadolint", "hadolint -f json Dockerfile", 1, "parse error",
     "task", _diff_for("Dockerfile")),
    (CheckovCheck(), _IAC, "checkov", "checkov -d . --quiet --compact -o json", 1,
     "Traceback: crash", "task", _diff_for("Dockerfile")),
]  # fmt: skip


@pytest.mark.parametrize(
    ("check", "files", "tool", "cmd", "code", "output", "scope", "diff"),
    _BROKEN_TOOLS,
    ids=[case[0].name for case in _BROKEN_TOOLS],
)
async def test_a_broken_tool_is_an_error_not_a_finding_or_a_skip(
    tmp_path: Path,
    check: Any,
    files: dict[str, str],
    tool: str,
    cmd: str,
    code: int,
    output: str,
    scope: str,
    diff: str,
) -> None:
    repo = _repo(tmp_path, files)
    runner, commands = _runner(
        repo,
        installed=(tool,),
        exit_codes={cmd: [code]},
        outputs={cmd: [output]},
        scope=scope,
        diff=diff,
    )
    result = await check.run(repo, runner)
    assert cmd in [called for called, _ in commands.calls]
    assert result.status == "error" and result.findings == []
    assert f"exit {code}" in result.detail and output in result.detail


class _TimingOut(FakeCommandRunner):
    """A command runner whose commands all time out."""

    async def run(self, command: str, cwd: Path, log_path: Path, timeout_s: int) -> Any:
        result = await super().run(command, cwd, log_path, timeout_s)
        return type(result)(-1, result.output, timed_out=True)


@pytest.mark.parametrize("case", [0, 4, 9], ids=["ruff", "bandit", "mypy"])
async def test_a_timed_out_tool_is_an_error(tmp_path: Path, case: int) -> None:
    check, files, tool, cmd, _, _, scope, diff = _BROKEN_TOOLS[case]
    repo = _repo(tmp_path, files)
    partial = "a.py:1: error: half a line [misc]" if tool == "mypy" else "a.py:1:1: E1 x"
    runner, _ = _runner(repo, installed=(tool,), scope=scope, diff=diff)
    runner.commands = _TimingOut(outputs={cmd: [partial]})
    result = await check.run(repo, runner)
    assert result.status == "error" and result.findings == []
    assert "timed out" in result.detail


async def test_gitleaks_exit_1_without_parsed_leaks_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/app.py": ""})
    runner, _ = _runner(
        repo,
        installed=("gitleaks",),
        diff=DIFF,
        exit_codes={_GITLEAKS_CMD: [1]},
        outputs={_GITLEAKS_CMD: ["WRN leaks found: 3"]},
    )
    result = await SecretScanCheck().run(repo, runner)
    assert result.status == "fail"
    (finding,) = result.findings
    assert finding.severity == "critical" and finding.category == "security"
    assert "could not" in finding.title
    assert blocking_reasons("basic", [result], result.findings)  # blocks at every level
    runner, _ = _runner(repo, installed=("gitleaks",), diff=DIFF)  # exit 0: no leaks
    assert (await SecretScanCheck().run(repo, runner)).status == "pass"


@pytest.mark.parametrize("check_name", ["bandit", "semgrep"])
async def test_scanner_errors_in_json_are_an_error(tmp_path: Path, check_name: str) -> None:
    if check_name == "bandit":
        check: Any = BanditCheck()
        repo, cmd = _repo(tmp_path, _PY), "bandit -q -f json a.py"
        report: Any = {"errors": [{"filename": "a.py", "reason": "syntax error"}], "results": []}
    else:
        check = SemgrepCheck()
        repo, cmd = _repo(tmp_path, {"a.py": "", ".semgrep.yml": ""}), _SEMGREP_CMD
        report = {"errors": [{"level": "error", "message": "rule parse error"}], "results": []}
    runner, _ = _runner(
        repo, installed=(check_name,), diff=_diff_for("a.py"), outputs={cmd: [json.dumps(report)]}
    )
    result = await check.run(repo, runner)
    assert result.status == "error" and "1 error(s)" in result.detail


def test_policy_blocks_on_a_required_error_only() -> None:
    required = CheckResult(status="error", detail="mypy timed out", name="mypy", required=True)
    advisory = CheckResult(status="error", detail="trivy crashed", name="trivy", required=False)
    assert blocking_reasons("production", [required], []) == ["mypy could not run: mypy timed out"]
    assert blocking_reasons("scalable", [advisory], []) == []


def test_error_status_in_reports_and_stored_json() -> None:
    errored = CheckResult(status="error", detail="bandit crashed", name="bandit", required=True)
    gate = _gate(blocked=True).model_copy(update={"checks": [errored]})
    assert "| bandit | **error** | bandit crashed |" in render_gate(gate)
    assert "checks 1 error" in gate.summary_line()
    assert GateResult.model_validate_json(gate.model_dump_json()) == gate
    old = _gate().model_dump_json()  # a report stored before the error status existed
    assert GateResult.model_validate_json(old).checks[1].status == "skipped"


def test_worktree_binaries_are_never_gate_tools(tmp_path: Path) -> None:
    # Issue #196: the task worktree is where the coder writes arbitrary files,
    # so a planted `<workdir>/.venv/bin/ruff` printing "clean" must never be
    # resolved as a gate tool. Only PATH and the main repository are trusted.
    repo = _repo(tmp_path / "r", {"pyproject.toml": ""})
    workdir = _repo(tmp_path / "w", {".venv/bin/ruff": "#!/bin/sh\necho clean\n"})
    runner, _ = _runner(repo)
    # Rebind the runner to a separate worktree (the fixture uses repo == workdir).
    runner.workdir = workdir
    assert runner.resolve("ruff") is None
    # A tool in the main repository's venv is still found.
    (repo / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (repo / ".venv" / "bin" / "ruff").write_text("")
    assert runner.resolve("ruff") == str(repo / ".venv" / "bin" / "ruff")
    # PATH lookup still wins over both.
    runner2, _ = _runner(repo, installed=("ruff",))
    runner2.workdir = workdir
    assert runner2.resolve("ruff") == "ruff"
