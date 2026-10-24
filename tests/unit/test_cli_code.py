"""`jev code ...` CLI commands (issue #198): status table, grade happy path
and file errors, route output and unavailable-backend handling — plus the
other two #198 error paths (dashboard port-in-use, design worker failure).

The real Coding Decision Agent backend is never loaded here (it is a 421M
checkpoint behind `laya` + torch, not a test dependency): grade/route tests
either inject a fake orchestrator or assert the clean unavailable-backend
error, never a traceback.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli import code_cmds
from jev_agent.cli.app import app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.code_orchestrator import CodeReviewGrade, RoutingGrade
from jev_agent.workers.base import WorkerJob, WorkerOutput
from jev_agent.workers.registry import WorkerRegistry

runner = CliRunner()


def _assert_clean_error(result: object, fragment: str) -> None:
    """Exit 1, an `error:` line on stderr, and no traceback anywhere."""
    assert result.exit_code == 1, result.output  # type: ignore[attr-defined]
    assert isinstance(result.exception, SystemExit)  # type: ignore[attr-defined]
    assert "Traceback" not in result.output  # type: ignore[attr-defined]
    assert "error:" in result.stderr  # type: ignore[attr-defined]
    assert fragment in " ".join(result.stderr.split())  # type: ignore[attr-defined]


class _FakeOrchestrator:
    """A canned Coding Decision Agent, standing in for the real backend."""

    def __init__(self, model_id: str | None = None) -> None:
        self.model_id = model_id
        self.available = True

    def grade_code(self, *, task: str, diff: str, tests: str = "") -> CodeReviewGrade:
        assert task and diff
        return CodeReviewGrade(
            code_quality=3,
            instruction_followed=True,
            likely_correct=True,
            merge_action="accept",
            answer_confidence={"merge_action": 0.9},
        )

    def route(self, request: str) -> RoutingGrade:
        assert request
        return RoutingGrade(
            model_tier="frontier",
            task_difficulty=2,
            skill="refactor",
            answer_confidence={"model_tier": 0.8},
        )


@pytest.fixture
def fake_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_cmds, "CodeOrchestrator", _FakeOrchestrator)


def test_code_status_table_reports_backend_unavailable(
    fixture_repo: Path,
) -> None:
    # No fake: the real backend is not installed in the test env, so
    # `backend_available` is "no" — and status still exits 0 with a table.
    result = runner.invoke(app, ["code", "status", "--repo", str(fixture_repo)])
    assert result.exit_code == 0, result.output
    assert result.exception is None
    text = " ".join(result.output.split())
    assert "Coding Decision Agent" in text
    assert "backend_available" in text
    assert "model" in text


def test_code_grade_happy_path(fixture_repo: Path, fake_orchestrator: None, tmp_path: Path) -> None:
    diff = tmp_path / "change.diff"
    diff.write_text("diff --git a/x.py b/x.py\n+print(1)\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "code",
            "grade",
            "--task",
            "fix x",
            "--diff-file",
            str(diff),
            "--repo",
            str(fixture_repo),
        ],
    )
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "code_quality" in text and "3/4" in text
    assert "merge_action" in text and "accept" in text
    assert "gate" in text


def test_code_grade_missing_diff_file_is_a_clean_error(fixture_repo: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "code",
            "grade",
            "--task",
            "fix x",
            "--diff-file",
            str(tmp_path / "no-such.diff"),
            "--repo",
            str(fixture_repo),
        ],
    )
    _assert_clean_error(result, "could not read diff file")


def test_code_grade_non_utf8_and_missing_tests_files_are_clean_errors(
    fixture_repo: Path, tmp_path: Path
) -> None:
    diff = tmp_path / "change.diff"
    diff.write_text("diff --git a/x.py b/x.py\n+print(1)\n", encoding="utf-8")
    bad = tmp_path / "tests.txt"
    bad.write_bytes(b"\xff\xfe not utf-8\n")
    result = runner.invoke(
        app,
        [
            "code",
            "grade",
            "--task",
            "fix x",
            "--diff-file",
            str(diff),
            "--tests-file",
            str(bad),
            "--repo",
            str(fixture_repo),
        ],
    )
    _assert_clean_error(result, "could not read tests file")
    result = runner.invoke(
        app,
        [
            "code",
            "grade",
            "--task",
            "fix x",
            "--diff-file",
            str(diff),
            "--tests-file",
            str(tmp_path / "no-such-tests.txt"),
            "--repo",
            str(fixture_repo),
        ],
    )
    _assert_clean_error(result, "could not read tests file")


def test_code_grade_unavailable_backend_is_a_clean_error(
    fixture_repo: Path, tmp_path: Path
) -> None:
    diff = tmp_path / "change.diff"
    diff.write_text("diff --git a/x.py b/x.py\n+print(1)\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "code",
            "grade",
            "--task",
            "fix x",
            "--diff-file",
            str(diff),
            "--repo",
            str(fixture_repo),
        ],
    )
    _assert_clean_error(result, "not installed")


def test_code_route_output(fixture_repo: Path, fake_orchestrator: None) -> None:
    result = runner.invoke(app, ["code", "route", "Refactor billing", "--repo", str(fixture_repo)])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "model_tier" in text and "frontier" in text
    assert "skill" in text and "refactor" in text
    assert "suggested_effort" in text


def test_code_route_unavailable_backend_is_a_clean_error(
    fixture_repo: Path,
) -> None:
    result = runner.invoke(app, ["code", "route", "Refactor billing", "--repo", str(fixture_repo)])
    _assert_clean_error(result, "not installed")


def test_dashboard_port_in_use_is_a_clean_error(
    fixture_repo: Path,
) -> None:
    # Hold a port, then start the dashboard on it: the pre-bind probe must
    # fail with an `error:` line before uvicorn logs anything or serves.
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    port = held.getsockname()[1]
    held.listen(1)
    try:
        result = runner.invoke(
            app,
            [
                "dashboard",
                "--no-open",
                "--port",
                str(port),
                "--repo",
                str(fixture_repo),
            ],
        )
    finally:
        held.close()
    _assert_clean_error(result, "could not start the dashboard")
    assert result.stdout == ""


def test_dashboard_uvicorn_failure_is_a_clean_error(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A free port, but uvicorn itself fails (e.g. the race after the probe):
    # that failure must also become an `error:` line, not a traceback.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    fake_uvicorn = types.ModuleType("uvicorn")
    # `import uvicorn` inside the command re-validates the module spec.
    fake_uvicorn.__spec__ = importlib.util.find_spec("uvicorn")

    def _run(*args: object, **kwargs: object) -> None:
        raise OSError(98, "Address already in use")

    fake_uvicorn.run = _run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    result = runner.invoke(
        app,
        ["dashboard", "--no-open", "--port", str(port), "--repo", str(fixture_repo)],
    )
    _assert_clean_error(result, "could not start the dashboard")


class _CrashingWorker:
    """A designer worker whose transport fails mid-call (issue #198)."""

    name = "crashing"

    async def run(self, job: WorkerJob) -> WorkerOutput:
        raise RuntimeError("transport failed: model CLI exited mid-call")


def test_design_generate_worker_failure_is_a_clean_error(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    monkeypatch.setattr(WorkerRegistry, "get", lambda self, name: _CrashingWorker())
    result = runner.invoke(
        app,
        [
            "design",
            "generate",
            "--brief",
            "A small marketing site",
            "--repo",
            str(fixture_repo),
        ],
    )
    _assert_clean_error(result, "the designer worker failed")
