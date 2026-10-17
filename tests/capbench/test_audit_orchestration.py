"""Audit-fix orchestration tests for capbench (audit majors 1-2, minors 1-7).

No model spend, no network: `run_task` (the only spender) is always stubbed,
and the CLI's `run` command is tested with a stubbed `run_suite`. Git is used
locally only, the same as the existing `test_tasks.py` fixtures.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest
from typer.testing import CliRunner

import jev_agent.capbench
import jev_agent.capbench.cli as cli_mod
import jev_agent.capbench.grading as grading_mod
import jev_agent.capbench.runner as runner_mod
from jev_agent.capbench.grading import GRADE_ERROR, GRADE_FAIL, GRADE_PASS, grade_main
from jev_agent.capbench.models import (
    GRADE_ARGV,
    SuiteResult,
    TaskResult,
    TaskSpec,
    load_task,
)
from jev_agent.capbench.report import badge_payload, render_scoreboard
from jev_agent.capbench.runner import (
    BenchmarkConfig,
    _checkout_result,
    _failed_task,
    _result_diff,
    benchmark_toml,
    prepare_sandbox,
    run_suite,
    write_suite_json,
)
from jev_agent.cli.app import app
from jev_agent.core.workspace import result_branch

runner = CliRunner()

REPO_ROOT = Path(jev_agent.capbench.__file__).resolve().parents[3]


# --- helpers ---------------------------------------------------------------------


def _make_task_dir(root: Path, task_id: str, title: str) -> Path:
    """A minimal tasks/<id>/ dir: brief.md, fixture/, grade.py (for load_task)."""
    task_dir = root / task_id
    (task_dir / "fixture").mkdir(parents=True)
    (task_dir / "brief.md").write_text(f"# {title}\n\nDo the thing.\n", encoding="utf-8")
    (task_dir / "fixture" / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")
    (task_dir / "grade.py").write_text("# grader stub\n", encoding="utf-8")
    return task_dir


def _make_tasks_dir(root: Path, *task_ids: str) -> Path:
    tasks_dir = root / "tasks"
    for task_id in task_ids:
        _make_task_dir(tasks_dir, task_id, f"Task {task_id}")
    return tasks_dir


def _ok_result(task_id: str) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        title=f"Task {task_id}",
        passed=True,
        correctness=80.0,
        ship_score=70,
        cost_usd=1.0,
        duration_min=10.0,
        profile="test",
        run_date="2026-10-16",
    )


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


# --- major 1: per-task failure isolation + incremental suite JSON ------------------


def test_run_suite_isolates_mid_suite_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad task becomes a `_failed_task` with a note; the suite completes."""
    tasks_dir = _make_tasks_dir(tmp_path, "t-a", "t-b")
    calls: list[str] = []

    def fake_run_task(
        task_id: str, config: BenchmarkConfig, tasks_dir: Path | None = None
    ) -> TaskResult:
        calls.append(task_id)
        if task_id == "t-b":
            raise RuntimeError("boom")
        return _ok_result(task_id)

    monkeypatch.setattr(runner_mod, "run_task", fake_run_task)
    config = BenchmarkConfig(profile="test", per_run_usd=3.0)
    out = tmp_path / "suite.json"

    suite = run_suite(["t-a", "t-b"], config, tasks_dir, out_path=out)

    assert calls == ["t-a", "t-b"]
    assert len(suite.tasks) == 2
    ok, failed = suite.tasks
    assert ok.passed and ok.task_id == "t-a"
    assert failed.task_id == "t-b"
    assert not failed.passed
    # zeros, not guesses
    assert failed.correctness == 0.0
    assert failed.cost_usd == 0.0
    assert failed.duration_min == 0.0
    assert failed.ship_score is None
    assert failed.grade is not None
    assert failed.grade.tests_passed == 0 and failed.grade.tests_total == 0
    # an explanatory note, not a silent zero
    assert failed.note.startswith("harness error: ")
    assert "boom" in failed.note
    assert failed.grade.reasons == [failed.note]
    # the suite JSON survived too (budget provenance recorded)
    written = SuiteResult.model_validate_json(out.read_text(encoding="utf-8"))
    assert written.evaluated
    assert [t.task_id for t in written.tasks] == ["t-a", "t-b"]
    assert written.per_run_usd == 3.0
    # atomic write: no temp file left behind
    assert not (tmp_path / "suite.json.tmp").exists()


def test_run_suite_keeps_partial_results_on_uncatchable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`out_path` is rewritten after every task, so even a hard crash mid-suite
    keeps the completed results on disk."""
    tasks_dir = _make_tasks_dir(tmp_path, "t-a", "t-b")

    def fake_run_task(
        task_id: str, config: BenchmarkConfig, tasks_dir: Path | None = None
    ) -> TaskResult:
        if task_id == "t-a":
            return _ok_result(task_id)
        raise KeyboardInterrupt()  # not an Exception subclass: isolation cannot catch it

    monkeypatch.setattr(runner_mod, "run_task", fake_run_task)
    out = tmp_path / "partial.json"

    with pytest.raises(KeyboardInterrupt):
        run_suite(["t-a", "t-b"], BenchmarkConfig(), tasks_dir, out_path=out)

    partial = SuiteResult.model_validate_json(out.read_text(encoding="utf-8"))
    assert [t.task_id for t in partial.tasks] == ["t-a"]


def test_run_suite_dedupes_task_ids_preserving_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicate ids must not double-spend or double-count in the mean."""
    tasks_dir = _make_tasks_dir(tmp_path, "t-a", "t-b")
    calls: list[str] = []

    def fake_run_task(
        task_id: str, config: BenchmarkConfig, tasks_dir: Path | None = None
    ) -> TaskResult:
        calls.append(task_id)
        return _ok_result(task_id)

    monkeypatch.setattr(runner_mod, "run_task", fake_run_task)

    suite = run_suite(["t-b", "t-a", "t-b", "t-a", "t-a"], BenchmarkConfig(), tasks_dir)

    assert calls == ["t-b", "t-a"]
    assert [t.task_id for t in suite.tasks] == ["t-b", "t-a"]


# --- major 2: orchestration unit tests --------------------------------------------


def test_prepare_sandbox_copies_fixture_into_fresh_repo(tmp_path: Path) -> None:
    """The agent builds in the sandbox: fixture contents copied, a seeded git
    commit, the benchmark TOML written, base SHA == rev-parse HEAD."""
    tasks_dir = _make_tasks_dir(tmp_path, "t-a")
    spec = load_task("t-a", tasks_dir)
    parent = tmp_path / "sandboxes"
    config = BenchmarkConfig(per_run_usd=5.0)

    sandbox, base_sha = prepare_sandbox(spec, parent, config)

    assert sandbox == parent / "t-a"
    # fixture contents, but not the grader's own files
    assert (sandbox / "hello.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (sandbox / "brief.md").exists()
    assert not (sandbox / "grade.py").exists()
    # a real git repo with the seeded commit
    assert _git(sandbox, "rev-parse", "HEAD") == base_sha
    assert "benchmark fixture: t-a" in _git(sandbox, "log", "--format=%s")
    # the benchmark TOML: fixed budget cap + quality level
    toml = (sandbox / "jev.toml").read_text(encoding="utf-8")
    assert "per_run_usd = 5.0" in toml
    assert "per_task_usd = 1.5" in toml
    assert 'level = "basic"' in toml


def test_benchmark_toml_caps_per_task_usd() -> None:
    """per_task_usd = min(1.5, per_run_usd): the task cap never exceeds the run."""
    assert "per_task_usd = 1.5" in benchmark_toml(5.0)
    assert "per_task_usd = 1.5" in benchmark_toml(1.5)
    assert "per_task_usd = 1.0" in benchmark_toml(1.0)
    assert "per_run_usd = 5.0" in benchmark_toml(5.0)


def test_failed_task_zero_dimensions(tmp_path: Path) -> None:
    """`_failed_task` records zeros and an explanatory note - never guesses."""
    spec = TaskSpec(id="t-a", title="Task t-a", dir=tmp_path, brief="brief")
    started = datetime.now(UTC)

    failed = _failed_task(
        spec, BenchmarkConfig(profile="test"), started, run_id="run-1", note="boom"
    )

    assert failed.task_id == "t-a"
    assert not failed.passed
    assert failed.correctness == 0.0
    assert failed.ship_score is None
    assert failed.cost_usd == 0.0
    assert failed.duration_min == 0.0
    assert failed.run_id == "run-1"
    assert failed.note == "boom"
    assert failed.run_date == started.date().isoformat()
    grade = failed.grade
    assert grade is not None
    assert grade.tests_passed == 0 and grade.tests_total == 0
    assert grade.diff_added == 0 and grade.diff_files == 0
    assert grade.diff_within_band is False
    assert grade.reasons == ["boom"]


def _hand_built_result_repo(tmp_path: Path, run_id: str) -> tuple[Path, str]:
    """A git repo with a committed base and a `jev/<run>/result` branch on top."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "code.py").write_text("X = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "base",
    )
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", result_branch(run_id))
    (repo / "code.py").write_text("X = 2\n", encoding="utf-8")
    (repo / "new.py").write_text("Y = 3\n", encoding="utf-8")
    _git(repo, "add", "-A")  # -a alone would not stage the new file
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "agent result",
    )
    _git(repo, "checkout", "-q", "main")
    return repo, base_sha


def test_result_diff_and_checkout_result(tmp_path: Path) -> None:
    """`_result_diff` diffs base..result-branch; `_checkout_result` restores only
    tracked files (no `.jev/` sandbox state leaks into the graded checkout)."""
    run_id = "run-1"
    repo, base_sha = _hand_built_result_repo(tmp_path, run_id)
    # untracked sandbox state must never reach the graded checkout
    (repo / ".jev").mkdir()
    (repo / ".jev" / "capbench.diff").write_text("stale", encoding="utf-8")

    diff_path, diff_text = _result_diff(repo, base_sha, run_id)

    assert diff_path == repo / ".jev" / "capbench.diff"
    assert "new.py" in diff_text
    assert "+Y = 3" in diff_text
    assert diff_path.read_text(encoding="utf-8") == diff_text

    dest = tmp_path / "checkout"
    _checkout_result(repo, run_id, dest)

    assert (dest / "code.py").read_text(encoding="utf-8") == "X = 2\n"
    assert (dest / "new.py").read_text(encoding="utf-8") == "Y = 3\n"
    assert not (dest / ".jev").exists()


def test_result_diff_empty_when_no_result_branch(tmp_path: Path) -> None:
    """A run that never produced a result branch diffs to nothing."""
    repo, base_sha = _hand_built_result_repo(tmp_path, "run-1")
    diff_path, diff_text = _result_diff(repo, base_sha, "run-never-started")
    assert diff_text == ""
    assert diff_path.read_text(encoding="utf-8") == ""


def test_write_suite_json_round_trips(tmp_path: Path) -> None:
    from jev_agent.capbench.models import unevaluated_suite

    suite = unevaluated_suite()
    path = tmp_path / "suite.json"
    assert write_suite_json(suite, path) == path
    assert SuiteResult.model_validate_json(path.read_text(encoding="utf-8")) == suite


# --- minor 2: evaluated-but-empty suite --------------------------------------------


def test_render_scoreboard_empty_evaluated_suite(tmp_path: Path) -> None:
    """`SuiteResult(evaluated=True, tasks=[])` renders a notice instead of crashing."""
    empty_tasks = tmp_path / "tasks"
    empty_tasks.mkdir()
    suite = SuiteResult(evaluated=True, tasks=[])
    out = render_scoreboard(suite, tasks_dir=empty_tasks)
    assert "no tasks" in out


def test_badge_payload_distinguishes_empty_from_unevaluated() -> None:
    """'evaluated with nothing to average' is not 'not run yet'."""
    empty = badge_payload(SuiteResult(evaluated=True, tasks=[]))
    assert empty["evaluated"] is True
    assert empty["suite_score"] is None
    assert empty["message"] != "not run yet"

    never = badge_payload(SuiteResult(evaluated=False, tasks=[]))
    assert never["evaluated"] is False
    assert never["message"] == "not run yet"


# --- minor 3: budget provenance -----------------------------------------------------


def test_scoreboard_renders_suite_budget(tmp_path: Path) -> None:
    """The scoreboard states the budget the suite actually ran under."""
    tasks_dir = _make_tasks_dir(tmp_path, "t-a")
    suite = SuiteResult(
        evaluated=True,
        per_run_usd=5.0,
        tasks=[_ok_result("t-a")],
    )
    out = render_scoreboard(suite, tasks_dir=tasks_dir)
    assert "per_run_usd = $5" in out

    # old JSON predates the field: fall back to the historical default
    old = SuiteResult.model_validate_json(
        json.dumps({"evaluated": True, "tasks": [], "profile": "default"})
    )
    assert old.per_run_usd is None
    empty_tasks = tmp_path / "empty"
    empty_tasks.mkdir()
    assert "per_run_usd = $2" in render_scoreboard(old, tasks_dir=empty_tasks)


# --- minor 1/6: --timeout-min wiring + overwrite guard ------------------------------


def _capbench(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["capbench", *args, "--repo", str(REPO_ROOT)])
    return result.exit_code, result.output


def test_run_refuses_to_overwrite_suite_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A previous suite's raw data is never silently clobbered."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    existing = workdir / "capbench-suite.json"
    existing.write_text('{"evaluated": true}', encoding="utf-8")

    called: list[bool] = []

    def fake_run_suite(
        task_ids: list[str] | None,
        config: BenchmarkConfig,
        tasks_dir: Path | None = None,
        out_path: Path | None = None,
    ) -> SuiteResult:
        called.append(True)
        suite = SuiteResult(evaluated=True, per_run_usd=config.per_run_usd, tasks=[])
        if out_path is not None:
            write_suite_json(suite, out_path)
        return suite

    monkeypatch.setattr(cli_mod, "run_suite", fake_run_suite)

    code, out = _capbench("run", "--task", "t1-cli-flag", "--workdir", str(workdir))
    assert code == 1
    assert "already exists" in out
    assert not called  # no spend happened
    assert existing.read_text(encoding="utf-8") == '{"evaluated": true}'

    # --force proceeds and overwrites
    code, out = _capbench("run", "--task", "t1-cli-flag", "--workdir", str(workdir), "--force")
    assert code == 0, out
    assert called
    payload = json.loads(existing.read_text(encoding="utf-8"))
    assert payload["evaluated"] is True
    assert payload["tasks"] == []
    assert payload["per_run_usd"] == 2.0  # the --budget default made it into the JSON


def test_timeout_min_wired_into_benchmark_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--timeout-min` reaches `BenchmarkConfig.timeout_min`."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    seen: list[BenchmarkConfig] = []

    def fake_run_suite(
        task_ids: list[str] | None,
        config: BenchmarkConfig,
        tasks_dir: Path | None = None,
        out_path: Path | None = None,
    ) -> SuiteResult:
        seen.append(config)
        if out_path is not None:
            write_suite_json(SuiteResult(evaluated=True, tasks=[]), out_path)
        return SuiteResult(evaluated=True, tasks=[])

    monkeypatch.setattr(cli_mod, "run_suite", fake_run_suite)

    code, out = _capbench(
        "run", "--all", "--workdir", str(workdir), "--timeout-min", "45", "--force"
    )
    assert code == 0, out
    assert seen and seen[0].timeout_min == 45.0

    code, out = _capbench("run", "--all", "--workdir", str(workdir), "--force")
    assert code == 0, out
    assert seen[-1].timeout_min is None


# --- minor 8: public API surface ----------------------------------------------------


def test_package_reexports_intended_api() -> None:
    """`jev_agent.capbench` re-exports the intended API (matches `runner.__all__`)."""
    for name in (
        "write_suite_json",
        "benchmark_toml",
        "unevaluated_suite",
        "GRADE_ARGV",
        "grade_main",
    ):
        assert name in jev_agent.capbench.__all__, name
        assert hasattr(jev_agent.capbench, name), name
    for name in runner_mod.__all__:
        assert name in jev_agent.capbench.__all__, name
        assert getattr(jev_agent.capbench, name) is getattr(runner_mod, name), name
    assert jev_agent.capbench.GRADE_ARGV is GRADE_ARGV
    assert jev_agent.capbench.grade_main is grade_main


# --- minor 7: grading.py ------------------------------------------------------------


def _grade_argv(tmp_path: Path, diff_text: str) -> tuple[Path, Path, Path]:
    result_dir = tmp_path / "result"
    result_dir.mkdir()
    (result_dir / "mod.py").write_text("X = 1\n", encoding="utf-8")
    run_json = tmp_path / "run.json"
    run_json.write_text(json.dumps({"run_id": "x", "ship_score": 80}), encoding="utf-8")
    diff = tmp_path / "diff.patch"
    diff.write_text(diff_text, encoding="utf-8")
    return result_dir, run_json, diff


def test_grade_main_diff_stats_computed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hoisted `diff_stats` feeds details, reasons and the report alike."""
    diff = (
        "diff --git a/mod.py b/mod.py\n"
        "index 111..222 100644\n"
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+a = 1\n+b = 2\n+c = 3\n"
    )
    result_dir, run_json, diff_path = _grade_argv(tmp_path, diff)
    hidden = {"test_ok.py": "def test_ok():\n    assert True\n"}
    argv = [
        "grade.py",
        "--result",
        str(result_dir),
        "--run-json",
        str(run_json),
        "--diff",
        str(diff_path),
    ]
    monkeypatch.setattr(sys, "argv", argv)

    code = grade_main("t-x", hidden, max_added=2, max_files=1)  # 3 added > band of 2
    assert code == GRADE_FAIL
    report = json.loads(capsys.readouterr().out)
    assert report["diff_added"] == 3
    assert report["diff_files"] == 1
    assert report["diff_within_band"] is False
    assert any("diff too big" in reason for reason in report["reasons"])
    assert report["details"]["diff"] == "+3/-0 in 1 file(s)"

    code = grade_main("t-x", hidden, max_added=10, max_files=2)
    assert code == GRADE_PASS
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is True
    assert report["diff_within_band"] is True
    assert report["reasons"] == []


def test_grade_main_timeout_is_harness_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hidden-pytest timeout exits 2 with a JSON error line (not a traceback)."""
    result_dir, run_json, diff_path = _grade_argv(tmp_path, "")
    argv = [
        "grade.py",
        "--result",
        str(result_dir),
        "--run-json",
        str(run_json),
        "--diff",
        str(diff_path),
    ]
    monkeypatch.setattr(sys, "argv", argv)

    def boom(*args: object, **kwargs: object) -> NoReturn:
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=300)

    monkeypatch.setattr(grading_mod, "run_hidden_pytest", boom)

    code = grade_main("t-x", {"test_ok.py": "def test_ok(): pass"}, max_added=10, max_files=2)
    assert code == GRADE_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert "timed out" in payload["error"]
