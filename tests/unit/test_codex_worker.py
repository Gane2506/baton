"""Tests for `CodexWorker` against recorded-style JSONL outputs (tests/fixtures/codex).

No real `codex` binary is run: a fake `CommandRunner` returns the fixture as stdout and, when
asked, writes the `-o` last-message file the way the CLI would.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jev_agent.core.models import ValidationResult
from jev_agent.roles.schemas import CoderSummary
from jev_agent.workers.base import Worker, WorkerJob
from jev_agent.workers.codex import (
    CodexWorker,
    classify_codex,
    final_message,
    in_git_repo,
    parse_events,
    usage_from,
)
from jev_agent.workers.subprocess_runner import CommandResult, log_command

FIXTURES = Path(__file__).parent.parent / "fixtures" / "codex"
SECRET = "sk-proj-CODEXSECRET0123456789abcdef"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@dataclass
class Call:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    timeout_s: float
    stdin_text: str | None
    schema: Any = None  # contents of --output-schema at call time


@dataclass
class FakeRunner:
    main: CommandResult
    git: CommandResult = field(default_factory=lambda: CommandResult(0, "", ""))
    last_message: str | None = None  # written to the -o file if set
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
        argv = list(argv)
        call = Call(argv, cwd, dict(env), timeout_s, stdin_text)
        if "--output-schema" in argv:
            call.schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
        if self.last_message is not None and "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_text(self.last_message)
        self.calls.append(call)
        result = self.git if argv[0] == "git" else self.main
        if log_path is not None:
            log_command(log_path, argv, cwd, timeout_s, stdin_text, result, env)
        return result


def ok(stdout: str, stderr: str = "") -> CommandResult:
    return CommandResult(0, stdout, stderr, False, 1.0)


def job(workdir: Path, role: str = "coder", **kw: Any) -> WorkerJob:
    return WorkerJob(
        role=role,  # type: ignore[arg-type]
        prompt=kw.pop("prompt", "PROMPT TEXT"),
        workdir=workdir,
        log_path=workdir.parent / "logs" / "codex.log",
        **kw,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    (path / ".git").mkdir()  # enough for in_git_repo; no git command needed
    return path


def worker(runner: FakeRunner, **kw: Any) -> CodexWorker:
    return CodexWorker(runner=runner, env={"PATH": "/usr/bin", "HOME": "/h"}, **kw)


def flag_values(argv: list[str], name: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == name]


# --- argv ----------------------------------------------------------------------------------


def test_is_a_worker() -> None:
    w = CodexWorker()
    assert isinstance(w, Worker)
    assert w.name == "codex"


def test_unknown_sandbox_rejected() -> None:
    with pytest.raises(ValueError):
        CodexWorker(sandbox="yolo")


def test_coder_argv(repo: Path, tmp_path: Path) -> None:
    argv = CodexWorker().build_argv(job(repo, model="gpt-5-codex", effort="high"), tmp_path)
    assert argv[:4] == ["codex", "exec", "--json", "--ephemeral"]
    assert flag_values(argv, "-C") == [str(repo)]
    assert flag_values(argv, "-s") == ["workspace-write"]
    assert flag_values(argv, "-c") == [
        'approval_policy="never"',
        'model_reasoning_effort="high"',
    ]
    assert flag_values(argv, "-m") == ["gpt-5-codex"]
    assert flag_values(argv, "-o") == [str(tmp_path / "last_message.txt")]
    assert "--output-schema" not in argv
    assert "--skip-git-repo-check" not in argv
    assert argv[-1] == "-"  # prompt on stdin
    for forbidden in (
        "--full-auto",
        "--ask-for-approval",
        "--dangerously-bypass-approvals-and-sandbox",
    ):
        assert forbidden not in argv


@pytest.mark.parametrize("role", ["planner", "validator", "reviewer"])
def test_read_only_roles_argv(repo: Path, tmp_path: Path, role: str) -> None:
    w = CodexWorker(sandbox="danger-full-access")  # applies to the coder only
    argv = w.build_argv(job(repo, role=role, output_schema=ValidationResult), tmp_path)
    assert flag_values(argv, "-s") == ["read-only"]
    assert flag_values(argv, "--output-schema") == [str(tmp_path / "schema.json")]


def test_non_git_workdir_skips_repo_check_and_extra_args(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    w = CodexWorker(command="/opt/codex", extra_args=["--ignore-user-config"])
    argv = w.build_argv(job(plain, effort="bogus"), tmp_path)
    assert argv[0] == "/opt/codex"
    assert "--skip-git-repo-check" in argv
    assert argv[-2:] == ["--ignore-user-config", "-"]
    assert not any("model_reasoning_effort" in a for a in argv)


def test_in_git_repo_handles_worktree_git_file(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    (wt / "sub").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    assert in_git_repo(wt / "sub")


# --- run -----------------------------------------------------------------------------------


async def test_coder_success(repo: Path) -> None:
    runner = FakeRunner(ok(fixture("success_coder.jsonl")), git=ok("?? app/limits.py\n"))
    out = await worker(runner).run(job(repo, timeout_s=99))
    r = out.result
    assert r.status == "success"
    assert r.summary == "Added a token-bucket limiter."
    assert r.files_changed == ["app/limits.py"]
    assert r.cost_usd is None  # codex reports no cost
    assert r.tokens == {
        "input": 10000,
        "cached_input": 8000,
        "output": 900,
        "reasoning_output": 300,
    }
    assert isinstance(out.parsed, CoderSummary)
    main = runner.calls[0]
    assert main.cwd == repo and main.stdin_text == "PROMPT TEXT" and main.timeout_s == 99
    assert runner.calls[1].argv[:3] == ["git", "-C", str(repo)]


async def test_coder_without_summary_is_error(repo: Path) -> None:
    out = await worker(FakeRunner(ok(fixture("coder_no_summary.jsonl")))).run(job(repo))
    assert out.result.status == "error"
    assert "jev-summary" in out.result.summary


async def test_structured_from_last_message_file(repo: Path) -> None:
    runner = FakeRunner(
        ok(fixture("success_text.jsonl")),
        last_message='{"verdict": "revise", "issues": ["missing tests"]}',
    )
    out = await worker(runner).run(job(repo, role="validator", output_schema=ValidationResult))
    assert out.result.status == "success"
    assert isinstance(out.parsed, ValidationResult) and out.parsed.issues == ["missing tests"]
    schema = runner.calls[0].schema
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"verdict", "issues", "challenges"}
    challenge = schema["$defs"]["ChoiceChallenge"]
    assert set(challenge["required"]) == {"choice", "alternative", "reason", "holds", "note"}


async def test_structured_from_events_when_no_file(repo: Path) -> None:
    runner = FakeRunner(ok(fixture("success_structured.jsonl")))
    out = await worker(runner).run(job(repo, role="reviewer", output_schema=ValidationResult))
    assert out.result.status == "success"
    assert isinstance(out.parsed, ValidationResult) and out.parsed.verdict == "approve"
    assert out.result.tokens is not None and out.result.tokens["input"] == 24763


async def test_structured_invalid_is_error(repo: Path) -> None:
    runner = FakeRunner(ok(fixture("structured_invalid.jsonl")))
    out = await worker(runner).run(job(repo, role="validator", output_schema=ValidationResult))
    assert out.result.status == "error"
    assert out.parsed is None


async def test_plain_text_role(repo: Path) -> None:
    out = await worker(FakeRunner(ok(fixture("success_text.jsonl")))).run(job(repo, role="planner"))
    assert out.result.status == "success"
    assert out.result.summary == "Repo contains app and tests directories."


async def test_timeout(repo: Path) -> None:
    out = await worker(FakeRunner(CommandResult(-15, "", "", True, 1200.0))).run(job(repo))
    assert out.result.status == "timeout"


@pytest.mark.parametrize(
    ("exit_code", "message"),
    [(127, "command not found: codex"), (126, "command not executable: codex")],
)
async def test_missing_binary_is_an_error(repo: Path, exit_code: int, message: str) -> None:
    out = await worker(FakeRunner(CommandResult(exit_code, "", message, False, 0.0))).run(job(repo))
    assert out.result.status == "error"
    assert message in out.result.summary


async def test_quota(repo: Path) -> None:
    runner = FakeRunner(CommandResult(1, fixture("quota_turn_failed_curly.jsonl"), "", False, 1))
    out = await worker(runner).run(job(repo))
    assert out.result.status == "quota_exhausted"
    assert "usage limit" in out.result.summary


async def test_secrets_never_reach_log_or_argv(repo: Path) -> None:
    stdout = fixture("auth_401.jsonl").replace("Missing bearer", f"bad key {SECRET}")
    runner = FakeRunner(CommandResult(1, stdout, f"CODEX_API_KEY={SECRET}", False, 1.0))
    w = CodexWorker(runner=runner, env={"CODEX_API_KEY": SECRET, "PATH": "/usr/bin"})
    out = await w.run(job(repo))
    assert out.result.status == "error"
    assert runner.calls[0].env["CODEX_API_KEY"] == SECRET
    assert all(SECRET not in a for a in runner.calls[0].argv)
    log = (repo.parent / "logs" / "codex.log").read_text()
    assert SECRET not in log
    assert SECRET not in out.result.summary


async def test_temp_files_are_removed(repo: Path) -> None:
    runner = FakeRunner(ok(fixture("success_structured.jsonl")), last_message="{}")
    await worker(runner).run(job(repo, role="validator", output_schema=ValidationResult))
    out_file = Path(flag_values(runner.calls[0].argv, "-o")[0])
    assert not out_file.parent.exists()
    assert not str(out_file).startswith(str(repo))


# --- classification ------------------------------------------------------------------------

CASES = [
    ("success_text.jsonl", 0, "", "success"),
    ("success_coder.jsonl", 0, "", "success"),
    ("reconnect_then_success.jsonl", 0, "", "success"),
    ("quota_turn_failed_curly.jsonl", 1, "", "quota_exhausted"),
    ("quota_error_straight.jsonl", 1, "", "quota_exhausted"),
    ("quota_out_of_credits.jsonl", 1, "", "quota_exhausted"),
    ("turn_failed_generic.jsonl", 1, "", "failure"),
    ("turn_failed_generic.jsonl", 0, "", "failure"),
    ("auth_401.jsonl", 1, "", "error"),
    ("no_turn_completed.jsonl", 0, "", "error"),
    ("no_turn_completed.jsonl", 1, "", "failure"),
    ("malformed.txt", 0, "", "error"),
    ("malformed.txt", 1, "", "failure"),
    ("success_text.jsonl", 2, "", "failure"),
    (None, 1, "Error: You\u2019ve hit your usage limit. Try again later.", "quota_exhausted"),
    (None, 1, "You hit your spend cap for this workspace", "quota_exhausted"),
    (None, 1, "Not logged in. Run `codex login` first.", "error"),
    (None, 1, "thread panicked", "failure"),
    # run_command's codes for a missing or non-executable binary: the worker cannot run.
    (None, 127, "command not found: codex", "error"),
    (None, 126, "command not executable: codex", "error"),
    ("turn_failed_generic.jsonl", 127, "", "failure"),  # codex itself ran: not a start failure
]


@pytest.mark.parametrize(("name", "exit_code", "stderr", "expected"), CASES)
def test_classify(name: str | None, exit_code: int, stderr: str, expected: str) -> None:
    stdout = fixture(name) if name else ""
    result = CommandResult(exit_code, stdout, stderr, False, 1.0)
    assert classify_codex(result, parse_events(stdout)) == expected


def test_classify_timeout_wins() -> None:
    stdout = fixture("success_text.jsonl")
    assert classify_codex(CommandResult(None, stdout, "", True, 9), parse_events(stdout)) == (
        "timeout"
    )


def test_parse_helpers() -> None:
    events = parse_events(fixture("success_text.jsonl") + "garbage\n[1,2]\n")
    assert events[0]["type"] == "thread.started"
    assert final_message(events) == "Repo contains app and tests directories."
    assert final_message([]) is None
    assert usage_from([]) is None
    two = parse_events(fixture("success_text.jsonl") * 2)
    tokens = usage_from(two)
    assert tokens is not None and tokens["input"] == 2 * 24763


def test_git_is_available_for_porcelain_parsing(tmp_path: Path) -> None:
    """Sanity check of `parse_porcelain` against real git output (git only, no agent CLI)."""
    from jev_agent.workers.summary import parse_porcelain

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a b.txt").write_text("x")
    (repo / "d").mkdir()
    (repo / "d" / "c.py").write_text("x")
    out = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert sorted(parse_porcelain(out)) == ["a b.txt", "d/c.py"]


def test_parse_porcelain_quoted_and_renamed() -> None:
    from jev_agent.workers.summary import parse_porcelain

    text = ' M src/a.py\n?? "caf\\303\\251.txt"\nR  old name.py -> new name.py\nx\n'
    assert parse_porcelain(text) == ["src/a.py", "café.txt", "new name.py"]
