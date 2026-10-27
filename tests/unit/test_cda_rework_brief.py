"""Rework briefs (issue #329, Phase 1): the brief is composed from the
grade's structured outputs + the gate's failures + the AI review text,
because the model returns labels, never prose. Every line must name the
source it came from, and a signal that did not fire must produce no
instruction.
"""

from __future__ import annotations

from typing import Any

from jev_agent.code_orchestrator import compose_rework_brief
from jev_agent.quality.models import CheckResult, Finding, GateResult
from jev_agent.roles.schemas import QualityReview


def _result(
    *,
    blocked: bool = True,
    code_grade: dict[str, Any] | None = None,
    checks: list[CheckResult] | None = None,
    review: QualityReview | None = None,
    review_status: str = "done",
    reasons: list[str] | None = None,
) -> GateResult:
    return GateResult(
        level="basic",
        scope="task",
        task_id="T1",
        number=1,
        checks=checks or [],
        review=review,
        review_status=review_status,  # type: ignore[arg-type]
        blocked=blocked,
        reasons=reasons or (["tests failed"] if blocked else []),
        code_grade=code_grade,
    )


def _graded_grade(**overrides: object) -> dict[str, Any]:
    grade: dict[str, Any] = {
        "status": "graded",
        "model": "S4MPL3BI4S/Coding_Decision_Agent",
        "code_quality": 3,
        "instruction_followed": True,
        "likely_correct": True,
        "merge_action": "request_changes",
        "answer_confidence": {"merge_action": 0.9},
        "gate": {
            "decision": "rework",
            "reason": "coding decision agent asks for request_changes",
            "merge_action": "request_changes",
            "confidence": 0.9,
        },
    }
    grade.update(overrides)
    return grade


def test_brief_carries_grade_signals_failed_checks_and_review() -> None:
    result = _result(
        code_grade=_graded_grade(code_quality=1, instruction_followed=False, likely_correct=False),
        checks=[CheckResult(status="fail", name="tests", detail="1 test failed")],
        review=QualityReview(summary="The fix handles the happy path only."),
    )
    brief = compose_rework_brief(result)
    assert "Rework brief — gate T1-1 blocked." in brief
    assert "code_quality 1/4" in brief
    assert "did not follow the task instructions" in brief
    assert "likely incorrect" in brief
    assert "tests (fail): 1 test failed" in brief
    assert "The fix handles the happy path only." in brief


def test_brief_names_needs_tests_and_low_confidence_human_review() -> None:
    grade = _graded_grade(merge_action="needs_tests")
    grade["gate"] = {
        "decision": "human_review",
        "reason": "not confident enough",
        "merge_action": "needs_tests",
        "confidence": 0.4,
    }
    brief = compose_rework_brief(_result(code_grade=grade), threshold=0.7)
    assert "add a test that covers the changed behaviour" in brief
    assert "a human must decide" in brief
    assert "0.40 < 0.70" in brief


def test_brief_without_a_grade_still_carries_checks_and_review() -> None:
    result = _result(
        code_grade=None,
        checks=[
            CheckResult(
                status="error",
                name="ruff",
                detail="crashed",
                findings=[
                    Finding(
                        source="ruff",
                        severity="major",
                        category="maintainability",
                        title="unused import",
                        file="a.py",
                        line_start=3,
                    )
                ],
            )
        ],
        review=QualityReview(summary="Style is off in a.py."),
    )
    brief = compose_rework_brief(result)
    assert "Grade:" not in brief
    assert "ruff (error): crashed" in brief
    assert "major maintainability a.py:3: unused import (ruff)" in brief
    assert "Style is off in a.py." in brief


def test_brief_says_when_there_is_no_review() -> None:
    brief = compose_rework_brief(_result(code_grade=None, review=None, review_status="skipped"))
    assert "No AI review (skipped" in brief


def test_brief_with_nothing_to_correct_says_so() -> None:
    result = _result(blocked=False, reasons=[], code_grade=None, review=None, review_status="done")
    brief = compose_rework_brief(result)
    assert "No grade, check failure or review finding to correct on." in brief


def test_unfired_signals_produce_no_instruction() -> None:
    # A clean-ish grade that still blocks (e.g. a check failed): no
    # instruction about quality, instructions or correctness may appear.
    brief = compose_rework_brief(
        _result(
            code_grade=_graded_grade(code_quality=4),
            checks=[CheckResult(status="fail", name="tests", detail="flaky")],
            review=QualityReview(summary="Looks fine apart from the test."),
        )
    )
    assert "Code quality is" not in brief
    assert "did not follow" not in brief
    assert "likely incorrect" not in brief
    assert "tests (fail): flaky" in brief
