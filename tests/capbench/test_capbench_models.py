"""The capability benchmark rubric: dimensions, composite and suite scores."""

from __future__ import annotations

import pytest

from jev_agent.capbench.models import (
    GradeReport,
    TaskResult,
    composite_score,
    correctness_score,
    cost_score,
    load_task,
    load_tasks,
    suite_score,
    task_result_from_grade,
    time_score,
    unevaluated_suite,
)


def _grade(**overrides: object) -> GradeReport:
    base: dict[str, object] = {
        "task_id": "t1-cli-flag",
        "passed": True,
        "tests_passed": 7,
        "tests_total": 10,
        "diff_added": 20,
        "diff_files": 1,
        "diff_within_band": True,
        "ship_score": 80,
        "reasons": [],
        "details": {},
    }
    base.update(overrides)
    return GradeReport.model_validate(base)


def test_correctness_is_test_fraction() -> None:
    assert correctness_score(7, 10) == pytest.approx(70.0)
    assert correctness_score(0, 5) == 0.0
    assert correctness_score(5, 5) == pytest.approx(100.0)


def test_correctness_no_tests_is_zero() -> None:
    assert correctness_score(0, 0) == 0.0  # no evidence, not a free pass


def test_cost_score_bands() -> None:
    assert cost_score(0.0) == 100.0
    assert cost_score(0.50) == 100.0
    assert cost_score(2.0) == 0.0
    assert cost_score(99.0) == 0.0
    assert cost_score(1.25) == pytest.approx(50.0)  # midpoint of 0.50..2.00


def test_time_score_bands() -> None:
    assert time_score(0.0) == 100.0
    assert time_score(5.0) == 100.0
    assert time_score(30.0) == 0.0
    assert time_score(120.0) == 0.0
    assert time_score(17.5) == pytest.approx(50.0)  # midpoint of 5..30


def test_composite_weights() -> None:
    # 50% correctness, 25% ship, 15% cost, 10% time
    score = composite_score(70.0, 80, 1.0, 10.0)
    expected = 0.50 * 70.0 + 0.25 * 80.0 + 0.15 * cost_score(1.0) + 0.10 * time_score(10.0)
    assert score == pytest.approx(expected)
    assert score == pytest.approx(73.0)


def test_composite_missing_ship_score_counts_zero() -> None:
    assert composite_score(100.0, None, 0.0, 0.0) == pytest.approx(75.0)


def test_suite_score_is_mean() -> None:
    tasks = [
        TaskResult(
            task_id="a",
            title="a",
            passed=True,
            correctness=80.0,
            ship_score=80,
            cost_usd=0.4,
            duration_min=4.0,
            profile="default",
            run_date="2026-10-15",
        ),
        TaskResult(
            task_id="b",
            title="b",
            passed=False,
            correctness=40.0,
            ship_score=None,
            cost_usd=1.0,
            duration_min=10.0,
            profile="default",
            run_date="2026-10-15",
        ),
    ]
    expected = (tasks[0].composite + tasks[1].composite) / 2
    assert suite_score(tasks) == pytest.approx(expected)


def test_suite_score_empty_is_none() -> None:
    assert suite_score([]) is None


def test_task_result_from_grade() -> None:
    grade = _grade()
    task = task_result_from_grade(
        load_task("t1-cli-flag"),
        grade,
        run_id="run-1",
        cost_usd=0.4,
        duration_min=4.0,
        profile="default",
        run_date="2026-10-15",
    )
    assert task.task_id == "t1-cli-flag"
    assert task.passed is True
    assert task.correctness == pytest.approx(70.0)
    assert task.ship_score == 80
    assert task.run_id == "run-1"


def test_unevaluated_suite_has_no_scores() -> None:
    suite = unevaluated_suite(load_tasks())
    assert suite.evaluated is False
    assert suite.score is None  # the only honest number: no fabricated scores
    assert len(suite.tasks) == 3
    assert all(t.note == "not run yet" for t in suite.tasks)
    assert all(t.ship_score is None and t.run_id is None for t in suite.tasks)


def test_load_tasks_sorted() -> None:
    ids = [spec.id for spec in load_tasks()]
    assert ids == ["t1-cli-flag", "t2-seeded-bug", "t3-validation"]


def test_load_unknown_task() -> None:
    with pytest.raises(FileNotFoundError):
        load_task("t9-nope")
