"""Issue #205 (swarm review: config remaining gaps): benchmark scores, traversal,
relative imports, grade timeout, and keep_sandbox cleanup."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from jev_agent.capbench import runner as runner_mod
from jev_agent.capbench.grading import (
    read_ship_score,
    run_grade_script,
    stdlib_only_imports,
)
from jev_agent.capbench.models import (
    GradeReport,
    TaskResult,
    correctness_score,
    load_task,
    task_result_from_grade,
)
from jev_agent.capbench.runner import BenchmarkConfig, run_task


def _task_dir(root: Path, task_id: str = "t1") -> Path:
    task = root / task_id
    (task / "fixture").mkdir(parents=True)
    (task / "brief.md").write_text("# Do a thing\n\nDetails.\n", encoding="utf-8")
    (task / "grade.py").write_text("print('{}')\n", encoding="utf-8")
    return task


# ---- 4. task-loading traversal ------------------------------------------------------------


def test_dotdot_task_id_cannot_escape_tasks_dir(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    _task_dir(tasks)
    outside = tmp_path / "outside"
    _task_dir(tmp_path, "outside")
    with pytest.raises(ValueError, match="escapes"):
        load_task("../outside", tasks)
    assert outside.exists()  # the crafted dir is untouched; nothing loaded from it


def test_absolute_task_id_cannot_replace_tasks_dir(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    _task_dir(tasks)
    outside = _task_dir(tmp_path, "outside")
    with pytest.raises(ValueError, match="escapes"):
        load_task(str(outside), tasks)


def test_symlink_inside_tasks_dir_cannot_point_out(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    _task_dir(tasks)
    outside = _task_dir(tmp_path, "outside")
    (tasks / "tlink").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        load_task("tlink", tasks)


def test_legit_task_still_loads(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    _task_dir(tasks)
    spec = load_task("t1", tasks)
    assert spec.id == "t1" and spec.title == "Do a thing"


# ---- 3. unbounded benchmark scores ---------------------------------------------------------


def test_correctness_clamps_more_passes_than_tests() -> None:
    assert correctness_score(5, 3) == 100.0  # was 166.7 on the public scoreboard
    assert correctness_score(0, 0) == 0.0
    assert correctness_score(7, 10) == pytest.approx(70.0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(150, 100), (-5, 0), (80, 80), (True, None), ("80", None), (None, None)],
)
def test_read_ship_score_clamps_and_rejects_bools(raw: object, expected: int | None) -> None:
    assert read_ship_score({"ship_score": raw}) == expected


@pytest.mark.parametrize("bad", [101, -1, 150])
def test_grade_report_ship_score_is_bounded(bad: int) -> None:
    with pytest.raises(ValidationError):
        GradeReport.model_validate(
            {
                "task_id": "t1",
                "passed": True,
                "tests_passed": 1,
                "tests_total": 1,
                "diff_added": 0,
                "diff_files": 0,
                "diff_within_band": True,
                "ship_score": bad,
            }
        )


def test_task_result_ship_score_is_bounded() -> None:
    with pytest.raises(ValidationError):
        TaskResult.model_validate(
            {
                "task_id": "t1",
                "title": "t",
                "passed": True,
                "correctness": 50.0,
                "ship_score": 101,
                "cost_usd": 0.0,
                "duration_min": 0.0,
                "profile": "default",
                "run_date": "2026-10-24",
            }
        )


def test_task_result_from_grade_clamps_correctness(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    _task_dir(tasks)
    spec = load_task("t1", tasks)
    grade = GradeReport.model_validate(
        {
            "task_id": "t1",
            "passed": True,
            "tests_passed": 5,
            "tests_total": 3,
            "diff_added": 0,
            "diff_files": 0,
            "diff_within_band": True,
            "ship_score": 80,
        }
    )
    result = task_result_from_grade(
        spec,
        grade,
        run_id=None,
        cost_usd=0.0,
        duration_min=0.0,
        profile="default",
        run_date="2026-10-24",
    )
    assert result.correctness == 100.0
    assert result.composite <= 100.0


# ---- 5. stdlib_only_imports relative imports -----------------------------------------------


def test_relative_imports_are_not_flagged(tmp_path: Path) -> None:
    module = tmp_path / "mod.py"
    module.write_text(
        "from .utils import helper\nfrom . import sibling\nfrom .. import parent\n",
        encoding="utf-8",
    )
    assert stdlib_only_imports([module]) == []


def test_absolute_non_stdlib_imports_still_flagged(tmp_path: Path) -> None:
    module = tmp_path / "mod.py"
    module.write_text(
        "import requests\nfrom pydantic import BaseModel\nfrom os import path\n",
        encoding="utf-8",
    )
    assert stdlib_only_imports([module]) == ["requests", "pydantic"]


# ---- grade timeout -------------------------------------------------------------------------


def test_hung_grade_script_is_a_harness_error_not_a_crash(tmp_path: Path) -> None:
    grade_py = tmp_path / "grade.py"
    grade_py.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="timed out"):
        run_grade_script(
            grade_py,
            tmp_path,
            tmp_path / "run.json",
            tmp_path / "diff",
            timeout_s=1,
        )


# ---- keep_sandbox early returns --------------------------------------------------------------


def _stub_run_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, keep_sandbox: bool) -> Path:
    """`run_task` with the run itself stubbed to fail at startup; returns the sandbox."""
    tasks = tmp_path / "tasks"
    _task_dir(tasks)
    workdir = tmp_path / "work"
    workdir.mkdir()
    sandbox = workdir / "t1"

    def fake_prepare(spec: object, parent: Path, config: object) -> tuple[Path, str]:
        sandbox.mkdir(parents=True, exist_ok=True)
        return sandbox, "base-sha"

    @contextlib.contextmanager
    def fake_context(path: Path) -> Iterator[None]:
        yield

    async def fake_start(*args: object, **kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(runner_mod, "prepare_sandbox", fake_prepare)
    monkeypatch.setattr(runner_mod, "open_context", fake_context)
    monkeypatch.setattr(runner_mod, "start_run", fake_start)
    config = BenchmarkConfig(workdir=workdir, keep_sandbox=keep_sandbox)
    result = run_task("t1", config, tasks)
    assert result.passed is False
    assert "run failed to start" in result.note
    return sandbox


def test_failed_run_removes_sandbox_when_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _stub_run_task(monkeypatch, tmp_path, keep_sandbox=False)
    assert not sandbox.exists()


def test_failed_run_keeps_sandbox_when_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _stub_run_task(monkeypatch, tmp_path, keep_sandbox=True)
    assert sandbox.exists()
