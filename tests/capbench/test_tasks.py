"""Benchmark task validation: schema, self-contained fixtures, and graders that are
neither trivially green (fail on the unmodified fixture) nor unsatisfiable
(pass on a hand-written reference solution). No model spend: the reference
solutions play the FakeWorker role."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from jev_agent.capbench.grading import run_grade_script
from jev_agent.capbench.models import GradeReport, load_task, load_tasks

TASKS = {spec.id: spec for spec in load_tasks()}

T1_SOLUTION = '''\
"""Demo CLI for capability benchmark task T1."""

from __future__ import annotations

import argparse
import json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo-cli", description="Greet somebody.")
    parser.add_argument("--name", default="world", help="Who to greet.")
    parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format."
    )
    args = parser.parse_args(argv)
    greeting = f"Hello, {args.name}!"
    if args.format == "json":
        print(json.dumps({"greeting": greeting, "name": args.name}))
    else:
        print(greeting)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

T2_SOLUTION = '''\
"""Tiny calculator for capability benchmark task T2."""

from __future__ import annotations


def add(a: float, b: float) -> float:
    """Return a + b."""
    return a + b


def mul(a: float, b: float) -> float:
    """Return a * b."""
    return a * b
'''

T3_SOLUTION = '''\
"""Minimal config parser for capability benchmark task T3 (hardened)."""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {"host": "localhost", "port": 8080, "debug": False}


def parse_config(data: dict[str, Any]) -> dict[str, Any]:
    """Build a config dict from raw data; ValueError on malformed input."""
    if not isinstance(data, dict):
        raise ValueError(f"config must be a mapping, got {type(data).__name__}")
    host = data.get("host", DEFAULTS["host"])
    if not isinstance(host, str):
        raise ValueError(f"host must be a string, got {type(host).__name__}")
    port = data.get("port", DEFAULTS["port"])
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError(f"port must be an integer, got {port!r}") from None
    return {"host": host, "port": port, "debug": bool(data.get("debug", DEFAULTS["debug"]))}
'''

SOLUTIONS = {
    "t1-cli-flag": {"demo_cli.py": T1_SOLUTION},
    "t2-seeded-bug": {"calc.py": T2_SOLUTION},
    "t3-validation": {"config.py": T3_SOLUTION},
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout


def _sandbox(task_id: str, tmp_path: Path, solution: dict[str, str] | None) -> Path:
    """A git repo: fixture committed, optional reference solution applied."""
    repo = tmp_path / task_id
    shutil.copytree(TASKS[task_id].fixture_dir, repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    if solution:
        for name, source in solution.items():
            (repo / name).write_text(source, encoding="utf-8")
    return repo


def _grade(task_id: str, repo: Path, tmp_path: Path) -> tuple[int, GradeReport]:
    diff = _git(repo, "diff")
    diff_path = tmp_path / "diff.patch"
    diff_path.write_text(diff, encoding="utf-8")
    run_json = tmp_path / "run.json"
    run_json.write_text(
        json.dumps({"run_id": "test", "ship_score": 90, "cost_usd": 0.3, "state": "DONE"}),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(TASKS[task_id].grade_py),
            "--result",
            str(repo),
            "--run-json",
            str(run_json),
            "--diff",
            str(diff_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.returncode, GradeReport.model_validate_json(proc.stdout)


def test_task_schema() -> None:
    assert set(TASKS) == {"t1-cli-flag", "t2-seeded-bug", "t3-validation"}
    for task_id, spec in TASKS.items():
        assert (spec.dir / "brief.md").read_text(encoding="utf-8").strip(), task_id
        assert any(spec.fixture_dir.iterdir()), task_id
        tree = ast.parse(spec.grade_py.read_text(encoding="utf-8"))
        assert isinstance(tree, ast.Module)
        # the grader speaks the --result/--run-json/--diff protocol (via grade_main)
        source = spec.grade_py.read_text(encoding="utf-8")
        assert "grade_main" in source, task_id
        for flag in ("--result", "--run-json", "--diff"):
            assert flag in source, (task_id, flag)


def test_fixtures_are_self_contained() -> None:
    """No fixture may import jev_agent or otherwise depend on Baton's internals."""
    for _task_id, spec in TASKS.items():
        for path in spec.fixture_dir.rglob("*"):
            if path.is_file() and path.suffix == ".py":
                assert "jev_agent" not in path.read_text(encoding="utf-8"), path


def test_t1_fixture_runs(tmp_path: Path) -> None:
    repo = _sandbox("t1-cli-flag", tmp_path, None)
    proc = subprocess.run(
        [sys.executable, "demo_cli.py", "--help"], cwd=repo, capture_output=True, text=True
    )
    assert proc.returncode == 0


def test_t2_fixture_ships_a_failing_test(tmp_path: Path) -> None:
    """The seeded bug: the fixture suite fails before the fix (proves T2 is real)."""
    repo = _sandbox("t2-seeded-bug", tmp_path, None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "1 failed" in proc.stdout


def test_t3_fixture_imports_and_parses(tmp_path: Path) -> None:
    repo = _sandbox("t3-validation", tmp_path, None)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from config import parse_config; print(parse_config({'host': 'h', 'port': '1'}))",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0


@pytest.mark.parametrize("task_id", sorted(TASKS))
def test_grader_fails_on_unmodified_fixture(task_id: str, tmp_path: Path) -> None:
    """The task is not trivially green: the grader fails the raw fixture."""
    repo = _sandbox(task_id, tmp_path, None)
    code, report = _grade(task_id, repo, tmp_path)
    assert code == 1, report
    assert report.passed is False
    assert report.reasons, "a failing grade must say why"


@pytest.mark.parametrize("task_id", sorted(TASKS))
def test_grader_passes_reference_solution(task_id: str, tmp_path: Path) -> None:
    """The grader is satisfiable: a hand-written solution passes."""
    repo = _sandbox(task_id, tmp_path, SOLUTIONS[task_id])
    code, report = _grade(task_id, repo, tmp_path)
    assert code == 0, report
    assert report.passed is True
    assert report.tests_passed == report.tests_total > 0
    assert report.diff_within_band is True


def test_run_grade_script_agrees_with_subprocess(tmp_path: Path) -> None:
    """The harness helper parses the same protocol the tests exercise directly."""
    repo = _sandbox("t2-seeded-bug", tmp_path, SOLUTIONS["t2-seeded-bug"])
    diff_path = tmp_path / "d.patch"
    diff_path.write_text(_git(repo, "diff"), encoding="utf-8")
    run_json = tmp_path / "r.json"
    run_json.write_text(json.dumps({"run_id": "x", "ship_score": 88}), encoding="utf-8")
    report = run_grade_script(TASKS["t2-seeded-bug"].grade_py, repo, run_json, diff_path)
    assert report.passed is True
    assert report.ship_score == 88
    assert load_task("t2-seeded-bug").id == "t2-seeded-bug"
