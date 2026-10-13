from jev_agent.config.schema import BudgetConfig
from jev_agent.core.assign import Assignment, can_reassign, escalate, fit_free, reassign
from jev_agent.core.budget import Budget
from jev_agent.dispatch.requests import DEFAULT_EFFORT, DEFAULT_MODEL, RoleOptions, model_fits


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


# ---- (worker, model) pairs (issue #33) ----------------------------------------------------------

SLUG = "anthropic/claude-sonnet-4.5"


def test_model_fits_slugs_on_api_and_cli_names_on_cli_workers() -> None:
    assert model_fits("api", SLUG)
    assert not model_fits("api", "sonnet")
    for cli in ("claude_code", "codex"):
        assert model_fits(cli, "sonnet")
        assert not model_fits(cli, SLUG)
    for worker in ("api", "claude_code", "codex"):
        assert model_fits(worker, None)
        assert model_fits(worker, DEFAULT_MODEL)


def test_fit_swaps_a_slug_on_a_cli_worker_for_its_first_runnable_model() -> None:
    opts = RoleOptions("planner", ["claude_code", "api"], [SLUG, "sonnet"], ["low"])
    assert fit_free(Assignment("claude_code", SLUG, "low"), opts) == Assignment(
        "claude_code", "sonnet", "low"
    )
    only_slug = RoleOptions("planner", ["claude_code", "api"], [SLUG], ["low"])
    assert fit_free(Assignment("claude_code", SLUG, "low"), only_slug) == Assignment(
        "claude_code", None, "low"
    )


def test_fit_swaps_a_cli_name_on_api_for_its_first_runnable_model() -> None:
    only_cli = RoleOptions("planner", ["claude_code", "api"], ["sonnet"], ["low"])
    assert fit_free(Assignment("api", "sonnet", None), only_cli) == Assignment("api", None, None)
    mixed = RoleOptions("planner", ["claude_code", "api"], ["sonnet", SLUG], ["low"])
    assert fit_free(Assignment("api", "sonnet", None), mixed) == Assignment("api", SLUG, None)


def test_reassign_and_escalate_keep_pairs_runnable() -> None:
    opts = RoleOptions("planner", ["claude_code", "api"], ["sonnet", SLUG, "opus"], ["high"])
    assert reassign(Assignment("claude_code", "sonnet", "high"), opts) == Assignment(
        "api", SLUG, "high"
    )
    assert reassign(Assignment("api", SLUG, "high"), opts) == Assignment(
        "claude_code", "sonnet", "high"
    )
    # Escalation skips models the worker cannot run.
    assert escalate(Assignment("claude_code", "sonnet", "high"), opts) == Assignment(
        "claude_code", "opus", "high"
    )
    assert escalate(Assignment("api", SLUG, "high"), opts) == Assignment("api", SLUG, "high")
