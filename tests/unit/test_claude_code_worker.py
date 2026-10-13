"""Tests for `ClaudeCodeWorker` against recorded-style outputs (tests/fixtures/claude_code).

No real `claude` binary is run: a fake `CommandRunner` returns the fixture as stdout.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jev_agent.core.models import Plan, ValidationResult
from jev_agent.roles.schemas import CoderSummary
from jev_agent.workers.base import Worker, WorkerJob
from jev_agent.workers.claude_code import (
    ClaudeCodeWorker,
    classify_claude_code,
    parse_result,
    usage_from,
)
from jev_agent.workers.subprocess_runner import CommandResult, log_command

FIXTURES = Path(__file__).parent.parent / "fixtures" / "claude_code"
SECRET = "sk-ant-api03-TOPSECRETKEY0123456789"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@dataclass
class Call:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    timeout_s: float
    stdin_text: str | None


@dataclass
class FakeRunner:
    """Returns `main` for the agent command and `git` for git commands; logs like the real one."""

    main: CommandResult
    git: CommandResult = field(default_factory=lambda: CommandResult(0, "", ""))
    calls: list[Call] = field(default_factory=list)

    async def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        stdin_text: str | None = None,
        *,
        log_path: Path | None = None,
    ) -> CommandResult:
        self.calls.append(Call(list(argv), cwd, dict(env), timeout_s, stdin_text))
        result = self.git if argv[0] == "git" else self.main
        if log_path is not None:
            log_command(log_path, argv, cwd, timeout_s, stdin_text, result, env)
        return result


def ok(stdout: str, stderr: str = "") -> CommandResult:
    return CommandResult(0, stdout, stderr, False, 1.0)


def job(tmp_path: Path, role: str = "coder", **kw: Any) -> WorkerJob:
    return WorkerJob(
        role=role,  # type: ignore[arg-type]
        prompt=kw.pop("prompt", "PROMPT TEXT"),
        workdir=tmp_path,
        log_path=tmp_path / "logs" / "claude.log",
        **kw,
    )


def worker(runner: FakeRunner, **kw: Any) -> ClaudeCodeWorker:
    return ClaudeCodeWorker(runner=runner, env={"PATH": "/usr/bin", "HOME": "/h"}, **kw)


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


# --- argv ----------------------------------------------------------------------------------


def test_is_a_worker() -> None:
    w = ClaudeCodeWorker()
    assert isinstance(w, Worker)
    assert w.name == "claude_code"


def test_coder_argv_defaults(tmp_path: Path) -> None:
    argv = ClaudeCodeWorker().build_argv(job(tmp_path, model="sonnet", effort="high"))
    assert argv[:5] == ["claude", "-p", "--output-format", "json", "--no-session-persistence"]
    assert flag(argv, "--model") == "sonnet"
    assert flag(argv, "--effort") == "high"
    assert flag(argv, "--permission-mode") == "acceptEdits"
    assert "Edit" in flag(argv, "--allowedTools").split()
    assert "--dangerously-skip-permissions" not in argv
    assert "--json-schema" not in argv
    assert "--max-budget-usd" not in argv
    assert "--bare" not in argv
    assert "PROMPT TEXT" not in argv  # prompt goes on stdin


@pytest.mark.parametrize("role", ["planner", "validator", "reviewer"])
def test_read_only_roles_argv(tmp_path: Path, role: str) -> None:
    w = ClaudeCodeWorker(skip_permissions=True)  # never applies to read-only roles
    argv = w.build_argv(job(tmp_path, role=role, output_schema=ValidationResult))
    assert flag(argv, "--permission-mode") == "dontAsk"
    assert flag(argv, "--allowedTools") == "Read Glob Grep"
    assert set(flag(argv, "--disallowedTools").split()) >= {"Edit", "Write"}
    assert "--dangerously-skip-permissions" not in argv
    schema = json.loads(flag(argv, "--json-schema"))
    assert schema == ValidationResult.model_json_schema()


def test_options_effort_budget_bare_extra_args(tmp_path: Path) -> None:
    w = ClaudeCodeWorker(
        command="/opt/claude",
        allowed_tools=["Read", "Edit", "Bash(pytest *)"],
        max_budget_usd=1.5,
        bare=True,
        extra_args=["--max-turns", "40"],
    )
    argv = w.build_argv(job(tmp_path, effort="turbo"))
    assert argv[0] == "/opt/claude"
    assert "--effort" not in argv  # unknown effort is dropped
    assert flag(argv, "--allowedTools") == "Read Edit Bash(pytest *)"
    assert flag(argv, "--max-budget-usd") == "1.5"
    assert "--bare" in argv
    assert argv[-2:] == ["--max-turns", "40"]


def test_skip_permissions_is_opt_in(tmp_path: Path) -> None:
    argv = ClaudeCodeWorker(skip_permissions=True).build_argv(job(tmp_path))
    assert "--dangerously-skip-permissions" in argv
    assert "--permission-mode" not in argv


def test_custom_permission_modes(tmp_path: Path) -> None:
    w = ClaudeCodeWorker(permission_mode="default", read_only_permission_mode="plan")
    assert flag(w.build_argv(job(tmp_path)), "--permission-mode") == "default"
    assert flag(w.build_argv(job(tmp_path, role="planner")), "--permission-mode") == "plan"


# --- run: success paths --------------------------------------------------------------------


async def test_coder_success_summary_files_cost_tokens(tmp_path: Path) -> None:
    git = ok(" M app/search.py\n?? app/limits.py\nR  old.py -> new.py\n")
    runner = FakeRunner(ok(fixture("success_coder.json")), git=git)
    out = await worker(runner).run(job(tmp_path, timeout_s=77))
    r = out.result
    assert r.status == "success"
    assert r.summary.startswith("Added a token-bucket rate limiter")
    assert r.files_changed == ["app/search.py", "app/limits.py", "new.py"]  # from git
    assert r.cost_usd == pytest.approx(0.4123)
    assert r.tokens == {
        "input": 1234,
        "output": 2345,
        "cache_read_input": 45678,
        "cache_creation_input": 3456,
    }
    assert r.raw_log_path == tmp_path / "logs" / "claude.log"
    assert isinstance(out.parsed, CoderSummary)
    main, git_call = runner.calls
    assert main.cwd == tmp_path and main.stdin_text == "PROMPT TEXT" and main.timeout_s == 77
    assert git_call.argv == [
        "git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=all",
    ]  # fmt: skip


async def test_coder_files_fall_back_to_summary_block_when_git_fails(tmp_path: Path) -> None:
    runner = FakeRunner(ok(fixture("success_coder.json")), git=CommandResult(128, "", "fatal"))
    out = await worker(runner).run(job(tmp_path))
    assert out.result.files_changed == ["app/limits.py", "app/search.py", "tests/test_limits.py"]


async def test_coder_without_summary_block_is_error(tmp_path: Path) -> None:
    runner = FakeRunner(ok(fixture("coder_no_summary.json")))
    out = await worker(runner).run(job(tmp_path))
    assert out.result.status == "error"
    assert "jev-summary" in out.result.summary
    assert out.result.cost_usd == pytest.approx(0.4123)  # cost is still recorded
    assert len(runner.calls) == 1  # no git call


async def test_structured_output_is_validated(tmp_path: Path) -> None:
    runner = FakeRunner(ok(fixture("success_structured_validation.json")))
    out = await worker(runner).run(job(tmp_path, role="validator", output_schema=ValidationResult))
    assert out.result.status == "success"
    assert isinstance(out.parsed, ValidationResult)
    assert out.parsed.verdict == "revise" and len(out.parsed.issues) == 2
    assert out.result.summary == "verdict: revise (2 issues)"


async def test_structured_falls_back_to_json_in_result_text(tmp_path: Path) -> None:
    runner = FakeRunner(ok(fixture("success_plan_in_result.json")))
    out = await worker(runner).run(job(tmp_path, role="planner", output_schema=Plan))
    assert out.result.status == "success"
    assert isinstance(out.parsed, Plan) and out.parsed.tasks[0].id == "T1"


async def test_structured_output_failing_schema_is_error(tmp_path: Path) -> None:
    runner = FakeRunner(ok(fixture("structured_invalid.json")))
    out = await worker(runner).run(job(tmp_path, role="reviewer", output_schema=ValidationResult))
    assert out.result.status == "error"
    assert out.parsed is None
    assert "does not match ValidationResult" in out.result.summary


async def test_plain_text_role_without_schema(tmp_path: Path) -> None:
    runner = FakeRunner(ok(fixture("success_text.json")))
    out = await worker(runner).run(job(tmp_path, role="planner"))
    assert out.result.status == "success"
    assert out.result.summary.startswith("The repository is a small Flask app")


# --- run: failures -------------------------------------------------------------------------


async def test_timeout(tmp_path: Path) -> None:
    runner = FakeRunner(CommandResult(-15, "", "", True, 1200.0))
    out = await worker(runner).run(job(tmp_path))
    assert out.result.status == "timeout"
    assert "timed out" in out.result.summary


async def test_quota_result(tmp_path: Path) -> None:
    runner = FakeRunner(CommandResult(1, fixture("quota_session_limit.json"), "", False, 2.0))
    out = await worker(runner).run(job(tmp_path))
    assert out.result.status == "quota_exhausted"
    assert "session limit" in out.result.summary


async def test_secrets_never_reach_log_or_summary(tmp_path: Path) -> None:
    stdout = fixture("is_error_api_500.json").replace("Internal server error", f"bad {SECRET}")
    runner = FakeRunner(CommandResult(1, stdout, f"debug: key={SECRET}", False, 1.0))
    w = ClaudeCodeWorker(runner=runner, env={"ANTHROPIC_API_KEY": SECRET, "PATH": "/usr/bin"})
    out = await w.run(job(tmp_path))
    assert runner.calls[0].env["ANTHROPIC_API_KEY"] == SECRET  # passed through to the CLI
    assert all(SECRET not in a for a in runner.calls[0].argv)  # never in argv
    assert SECRET not in (runner.calls[0].stdin_text or "")
    log = (tmp_path / "logs" / "claude.log").read_text()
    assert SECRET not in log
    assert SECRET not in out.result.summary
    assert "ANTHROPIC_API_KEY" not in log


async def test_runner_oserror_is_error(tmp_path: Path) -> None:
    class Boom:
        async def __call__(self, *args: Any, **kwargs: Any) -> CommandResult:
            raise OSError("no such file")

    out = await ClaudeCodeWorker(runner=Boom()).run(job(tmp_path))
    assert out.result.status == "error"


# --- classification ------------------------------------------------------------------------

CASES = [
    ("success_text.json", 0, "", "success"),
    ("success_coder.json", 0, "", "success"),
    ("error_max_turns.json", 1, "", "failure"),
    ("error_max_budget.json", 1, "", "failure"),
    ("error_during_execution.json", 1, "", "failure"),
    ("is_error_api_500.json", 1, "", "failure"),
    ("error_structured_retries.json", 1, "", "error"),
    ("quota_session_limit.json", 1, "", "quota_exhausted"),
    ("quota_weekly_limit_curly.json", 1, "", "quota_exhausted"),
    ("quota_credit_balance.json", 1, "", "quota_exhausted"),
    ("stream_rate_limit_rejected.jsonl", 1, "", "quota_exhausted"),
    ("auth_invalid_key.json", 1, "", "error"),
    ("stream_auth_failed.jsonl", 1, "", "error"),
    ("malformed.txt", 0, "", "error"),
    ("malformed.txt", 1, "", "failure"),
    (None, 1, "Error: Request rejected (429) · usage limit", "quota_exhausted"),
    (None, 1, "You\u2019ve hit your weekly limit · resets Mon", "quota_exhausted"),
    (None, 1, "Not logged in · Please run /login", "error"),
    (None, 1, "Error: something crashed", "failure"),
    (None, 0, "", "error"),
]


@pytest.mark.parametrize(("name", "exit_code", "stderr", "expected"), CASES)
def test_classify(name: str | None, exit_code: int, stderr: str, expected: str) -> None:
    stdout = fixture(name) if name else ""
    result = CommandResult(exit_code, stdout, stderr, False, 1.0)
    assert classify_claude_code(result, parse_result(stdout)) == expected


def test_classify_timeout_wins() -> None:
    stdout = fixture("success_text.json")
    result = CommandResult(None, stdout, "", True, 5.0)
    assert classify_claude_code(result, parse_result(stdout)) == "timeout"


def test_success_payload_with_nonzero_exit_is_not_success() -> None:
    stdout = fixture("success_text.json")
    result = CommandResult(1, stdout, "", False, 1.0)
    assert classify_claude_code(result, parse_result(stdout)) == "failure"


def test_parse_result_and_usage() -> None:
    assert parse_result("") is None
    assert parse_result("not json") is None
    assert parse_result('{"type":"system","subtype":"init"}') is None
    payload = parse_result(fixture("stream_rate_limit_rejected.jsonl"))
    assert payload is not None and payload["subtype"] == "error_during_execution"
    assert usage_from(None) == (None, None)
    assert usage_from({"total_cost_usd": 1, "usage": {"input_tokens": 3, "x": 1}}) == (
        1.0,
        {"input": 3},
    )
