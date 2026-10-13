from jev_agent.config.schema import BudgetConfig
from jev_agent.core.assign import Assignment, can_reassign, escalate, reassign
from jev_agent.core.budget import Budget
from jev_agent.dispatch.requests import DEFAULT_EFFORT, DEFAULT_MODEL, RoleOptions


def test_budget_caps_are_inclusive() -> None:
    budget = Budget.from_config(BudgetConfig(per_run_usd=2.0, per_task_usd=1.0))
    assert not budget.run_exhausted(1.99)
    assert budget.run_exhausted(2.0)
    assert not budget.task_exhausted(0.5)
    assert budget.task_exhausted(1.5)


OPTS = RoleOptions(
    "coder", ["claude_code", "codex", "api"], ["m1", "m2"], ["low", "medium", "high"]
)


def test_assignment_from_answers_maps_sentinels_to_none() -> None:
    a = Assignment.from_answers(
        {"worker": "codex", "model": DEFAULT_MODEL, "effort": DEFAULT_EFFORT}
    )
    assert a == Assignment("codex", None, None)


def test_escalate_bumps_effort_then_model_then_stops() -> None:
    a = Assignment("codex", "m1", "medium")
    a = escalate(a, OPTS)
    assert a == Assignment("codex", "m1", "high")
    a = escalate(a, OPTS)
    assert a == Assignment("codex", "m2", "high")
    assert escalate(a, OPTS) == a


def test_escalate_with_default_model_and_effort_is_a_no_op() -> None:
    opts = RoleOptions("coder", ["codex"], [DEFAULT_MODEL], [DEFAULT_EFFORT])
    a = Assignment("codex", None, None)
    assert escalate(a, opts) == a


def test_reassign_moves_to_next_worker_and_wraps() -> None:
    assert reassign(Assignment("codex", "m1", "low"), OPTS).worker == "api"
    assert reassign(Assignment("api", "m1", "low"), OPTS).worker == "claude_code"
    assert reassign(Assignment("gone", None, None), OPTS).worker == "claude_code"
    single = RoleOptions("coder", ["codex"], ["m1"], ["low"])
    assert reassign(Assignment("codex", "m1", "low"), single).worker == "codex"
    assert not can_reassign(Assignment("codex", "m1", "low"), single)
    assert can_reassign(Assignment("codex", "m1", "low"), OPTS)
