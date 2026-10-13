"""Live smoke tests for the Claude Code and Codex workers. Never run in CI.

Run with: JEV_LIVE_TESTS=1 uv run pytest tests/live/test_cli_workers_live.py
Each test needs its CLI on PATH and logged in (an API key is the supported path: see
docs/security.md). Optional: JEV_LIVE_CLAUDE_MODEL, JEV_LIVE_CODEX_MODEL.
The prompt is a trivial read-only validator task in a throwaway git repo; it costs cents.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from jev_agent.core.models import ValidationResult
from jev_agent.workers.base import Worker, WorkerJob
from jev_agent.workers.claude_code import ClaudeCodeWorker
from jev_agent.workers.codex import CodexWorker

_ENABLED = os.environ.get("JEV_LIVE_TESTS") == "1"
_REASON = "live test: set JEV_LIVE_TESTS=1 and install the CLI"

PROMPT = (
    "You validate plans. Read README.md in this directory. Plan: 'T1: add a --version flag to "
    "the CLI, tested by pytest tests/test_cli.py'. Do not modify any file. Reply with JSON: "
    "verdict approve or revise, and a list of issues."
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("# demo\nA tiny CLI with a --help flag.\n")
    return repo


async def _run(worker: Worker, tmp_path: Path, model: str | None) -> None:
    repo = _repo(tmp_path)
    job = WorkerJob(
        role="validator",
        prompt=PROMPT,
        workdir=repo,
        log_path=tmp_path / "live.log",
        model=model,
        effort="low",
        timeout_s=300,
        output_schema=ValidationResult,
    )
    out = await worker.run(job)
    assert out.result.status == "success", (tmp_path / "live.log").read_text()[-2000:]
    assert isinstance(out.parsed, ValidationResult)
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
        ).stdout
        == ""
    ), "a read-only role changed files"


@pytest.mark.skipif(not (_ENABLED and shutil.which("claude")), reason=_REASON)
async def test_claude_code_validator(tmp_path: Path) -> None:
    await _run(ClaudeCodeWorker(), tmp_path, os.environ.get("JEV_LIVE_CLAUDE_MODEL"))


@pytest.mark.skipif(not (_ENABLED and shutil.which("codex")), reason=_REASON)
async def test_codex_validator(tmp_path: Path) -> None:
    await _run(CodexWorker(), tmp_path, os.environ.get("JEV_LIVE_CODEX_MODEL"))
