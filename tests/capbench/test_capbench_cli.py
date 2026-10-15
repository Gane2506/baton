"""`jev capbench` CLI: parsing, dry-run, grade and report. No runs (no model spend)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

import jev_agent.capbench
from jev_agent.capbench.models import load_tasks, unevaluated_suite
from jev_agent.cli.app import app

runner = CliRunner()

REPO_ROOT = Path(jev_agent.capbench.__file__).resolve().parents[3]


def _capbench(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["capbench", *args, "--repo", str(REPO_ROOT)])
    return result.exit_code, result.output


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["capbench", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("run", "grade", "report", "tasks"):
        assert command in result.output


def test_tasks_lists_three() -> None:
    code, out = _capbench("tasks")
    assert code == 0, out
    for task_id in ("t1-cli-flag", "t2-seeded-bug", "t3-validation"):
        assert task_id in out


def test_run_rejects_unknown_task() -> None:
    code, out = _capbench("run", "--task", "t9-nope")
    assert code == 1
    assert "unknown benchmark task" in out


def test_run_dry_run_spends_nothing(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    code, out = _capbench("run", "--task", "t1-cli-flag", "--dry-run", "--workdir", str(workdir))
    assert code == 0, out
    assert "t1-cli-flag" in out
    assert "No model spend in a dry run" in out
    assert not workdir.exists()  # nothing built


def test_grade_unmodified_fixture_fails(tmp_path: Path) -> None:
    spec = {s.id: s for s in load_tasks()}["t1-cli-flag"]
    result_dir = tmp_path / "result"
    shutil.copytree(spec.fixture_dir, result_dir)
    run_json = tmp_path / "run.json"
    run_json.write_text(json.dumps({"run_id": "x", "ship_score": None}), encoding="utf-8")
    diff = tmp_path / "diff.patch"
    diff.write_text("", encoding="utf-8")
    code, out = _capbench(
        "grade",
        "t1-cli-flag",
        "--result",
        str(result_dir),
        "--run-json",
        str(run_json),
        "--diff",
        str(diff),
    )
    assert code == 1, out
    assert '"passed": false' in out


def test_grade_unknown_task() -> None:
    code, out = _capbench("grade", "t9-nope", "--result", ".", "--run-json", ".", "--diff", ".")
    assert code == 1
    assert "t9-nope" in out


def test_report_regenerates_docs(tmp_path: Path) -> None:
    suite_json = tmp_path / "suite.json"
    suite_json.write_text(
        unevaluated_suite(load_tasks()).model_dump_json(indent=2), encoding="utf-8"
    )
    md_out = tmp_path / "benchmarks.md"
    json_out = tmp_path / "benchmarks.json"
    code, out = _capbench(
        "report",
        "--suite-json",
        str(suite_json),
        "--md-out",
        str(md_out),
        "--json-out",
        str(json_out),
    )
    assert code == 0, out
    assert "unevaluated" in md_out.read_text(encoding="utf-8")
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["message"] == "not run yet"
    assert "template" in out  # warns that the suite is unevaluated


def test_report_rejects_bad_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    code, out = _capbench("report", "--suite-json", str(bad))
    assert code == 1
    assert "cannot read suite JSON" in out
