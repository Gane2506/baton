from pathlib import Path

import pytest
from pydantic import ValidationError

from jev_agent.core.models import Decision, DecisionRequest, Plan, TaskCard


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
