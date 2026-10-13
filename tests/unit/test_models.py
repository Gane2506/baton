from pathlib import Path

import pytest
from pydantic import ValidationError

from jev_agent.core.models import (
    ChoiceChallenge,
    Decision,
    DecisionRequest,
    Plan,
    TaskCard,
    ValidationResult,
)


def _task(tid: str, deps: list[str] | None = None) -> TaskCard:
    return TaskCard(id=tid, title=tid, description="d", depends_on=deps or [])


def test_plan_accepts_valid_graph() -> None:
    plan = Plan(summary="s", tasks=[_task("T1"), _task("T2", ["T1"])])
    assert [t.id for t in plan.tasks] == ["T1", "T2"]


@pytest.mark.parametrize(
    "tasks",
    [
        [_task("T1"), _task("T1")],
        [_task("T1", ["T9"])],
        [_task("T1", ["T1"])],
        [],
    ],
)
def test_plan_rejects_bad_graph(tasks: list[TaskCard]) -> None:
    with pytest.raises(ValidationError):
        Plan(summary="s", tasks=tasks)


def test_task_id_format() -> None:
    with pytest.raises(ValidationError):
        TaskCard(id="task-1", title="t", description="d")


def test_decision_request_needs_answers() -> None:
    with pytest.raises(ValidationError):
        DecisionRequest(point="D1", state={}, questions={"q": []})


def test_invalid_answers() -> None:
    req = DecisionRequest(point="D4", state={}, questions={"effort": ["low", "high"], "w": ["a"]})
    ok = Decision(point="D4", answers={"effort": "low", "w": "a"}, dispatcher="rules")
    bad = Decision(point="D4", answers={"effort": "max"}, dispatcher="jev")
    assert ok.invalid_answers(req) == []
    assert bad.invalid_answers(req) == ["effort: 'max' not in ['low', 'high']", "w: missing"]


def test_worker_result_path() -> None:
    from jev_agent.core.models import WorkerResult

    r = WorkerResult(status="success", summary="ok", raw_log_path=Path("x.log"))
    assert r.files_changed == []


def _unsound(note: str = "the alternative is simpler") -> ChoiceChallenge:
    return ChoiceChallenge(
        choice="a new module",
        alternative="extend utils.py",
        reason="(not stated)",
        holds=False,
        note=note,
    )


def test_failed_challenge_is_an_issue_and_forces_revise() -> None:
    sound = ChoiceChallenge(choice="pytest", alternative="unittest", holds=True)
    result = ValidationResult(verdict="approve", challenges=[sound, _unsound()])
    assert result.verdict == "revise"
    assert result.issues == [
        "Choice: a new module instead of extend utils.py. the alternative is simpler "
        "(planner's reason: (not stated))"
    ]


def test_sound_challenges_keep_the_verdict() -> None:
    sound = ChoiceChallenge(choice="pytest", holds=True)
    assert ValidationResult(verdict="approve", challenges=[sound]).issues == []
    assert ValidationResult(verdict="approve", challenges=[sound]).verdict == "approve"


def test_challenge_issues_are_idempotent_on_reload() -> None:
    result = ValidationResult(verdict="revise", challenges=[_unsound(), _unsound()])
    assert len(result.issues) == 1  # the same mistake is reported once
    assert ValidationResult.model_validate_json(result.model_dump_json()) == result


def test_validation_without_challenges_loads() -> None:
    # Runs stored before #67 have no `challenges` key.
    old = ValidationResult.model_validate_json('{"verdict": "approve", "issues": []}')
    assert (old.verdict, old.issues, old.challenges) == ("approve", [], [])


def test_challenge_needs_a_choice_and_a_holds_flag() -> None:
    with pytest.raises(ValidationError):
        ChoiceChallenge.model_validate({"holds": True})
    with pytest.raises(ValidationError):
        ChoiceChallenge.model_validate({"choice": "x"})
