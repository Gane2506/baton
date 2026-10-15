"""The public scoreboard: unevaluated template vs. evaluated results, badge JSON."""

from __future__ import annotations

import json
from pathlib import Path

from jev_agent.capbench.models import (
    GradeReport,
    SuiteResult,
    TaskResult,
    load_tasks,
    unevaluated_suite,
)
from jev_agent.capbench.report import badge_payload, render_scoreboard, write_scoreboard


def _evaluated_suite() -> SuiteResult:
    grade = GradeReport(
        task_id="t1-cli-flag",
        passed=True,
        tests_passed=7,
        tests_total=8,
        diff_added=20,
        diff_files=1,
        diff_within_band=True,
        ship_score=82,
        reasons=[],
        details={},
    )
    task = TaskResult(
        task_id="t1-cli-flag",
        title="T1",
        run_id="run-1",
        passed=True,
        correctness=87.5,
        ship_score=82,
        cost_usd=0.40,
        duration_min=4.2,
        profile="default",
        run_date="2026-10-15",
        grade=grade,
    )
    return SuiteResult(
        evaluated=True,
        profile="default",
        commit="abc1234",
        tasks=[task],
        note="n=1",
    )


def test_unevaluated_markdown_is_clearly_a_template() -> None:
    md = render_scoreboard(unevaluated_suite(load_tasks()))
    assert "unevaluated" in md
    assert "not run" in md
    assert "Nothing below is measured" in md
    # the one-line disambiguation header pointing at docs/ship.md
    assert "docs/ship.md" in md
    assert "KPI" in md


def test_unevaluated_badge_is_not_a_score() -> None:
    payload = badge_payload(unevaluated_suite(load_tasks()))
    assert payload["schemaVersion"] == 1
    assert payload["label"] == "capbench"
    assert payload["message"] == "not run yet"
    assert payload["color"] == "lightgrey"
    assert payload["evaluated"] is False
    assert payload["suite_score"] is None


def test_evaluated_markdown_shows_dimensions() -> None:
    md = render_scoreboard(_evaluated_suite())
    assert "✅ pass" in md
    assert "7/8" in md  # hidden tests
    assert "$0.40" in md
    assert "Suite score:" in md


def test_evaluated_badge_message_and_color() -> None:
    payload = badge_payload(_evaluated_suite())
    assert payload["message"] == "89/100"  # 0.5*87.5 + 0.25*82 + 0.15*100 + 0.1*100
    assert payload["color"] == "brightgreen"  # >= 85, the project's own bar
    assert payload["evaluated"] is True


def test_badge_color_bands() -> None:
    def payload_for(correctness: float, ship: int) -> dict[str, object]:
        suite = _evaluated_suite()
        task = suite.tasks[0].model_copy(update={"correctness": correctness, "ship_score": ship})
        suite = suite.model_copy(update={"tasks": [task]})
        result = badge_payload(suite)
        assert isinstance(result, dict)
        return result

    # composite = 0.5*correctness + 0.25*ship + 25 (cost and time maxed)
    assert payload_for(100.0, 100)["color"] == "brightgreen"  # 100
    assert payload_for(80.0, 40)["color"] == "green"  # 75
    assert payload_for(40.0, 20)["color"] == "yellow"  # 50
    assert payload_for(10.0, 0)["color"] == "orange"  # 30
    assert payload_for(0.0, 0)["color"] == "red"  # 25


def test_write_scoreboard_writes_both_files(tmp_path: Path) -> None:
    md_path = tmp_path / "benchmarks.md"
    json_path = tmp_path / "benchmarks.json"
    write_scoreboard(unevaluated_suite(load_tasks()), md_path=md_path, json_path=json_path)
    assert md_path.is_file() and "unevaluated" in md_path.read_text(encoding="utf-8")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == 1
