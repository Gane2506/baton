from pathlib import Path
from typing import Any

import pytest

from jev_agent.config import JevConfig
from jev_agent.core.models import DecisionRequest, Idea, Plan, TaskCard, ValidationResult
from jev_agent.dispatch import requests as r
from jev_agent.dispatch.rules import RulesDispatcher, complexity

ALL = ["claude_code", "codex", "api"]
rules = RulesDispatcher()


async def _answers(req: DecisionRequest) -> dict[str, str]:
    decision = await rules.decide(req)
    assert decision.dispatcher == "rules"
    assert decision.invalid_answers(req) == []
    return decision.answers


def _opts(role: str, **cfg: Any) -> r.RoleOptions:
    return r.role_options(JevConfig.model_validate({"roles": {role: cfg}}), role, ALL)  # type: ignore[arg-type]


CARD = TaskCard(
    id="T1", title="t", description="d", files_hint=["a", "b"], acceptance_criteria=["x"]
)
PLAN = Plan(summary="s", tasks=[CARD])


async def test_d1_plans_with_first_planner(tmp_path: Path) -> None:
    req = r.d1_intake(Idea(text="x", repo_path=tmp_path), _opts("planner"))
    assert await _answers(req) == {
        "clear": "plan",
        "worker": "claude_code",
        "model": "default",
        "effort": "medium",
    }


async def test_d2_prefers_a_different_vendor() -> None:
    opts = _opts("validator", workers=["claude_code", "codex"], models=[])
    req = r.d2_validator(PLAN, 1, opts, "planner", "claude_code", None)
    assert (await _answers(req))["worker"] == "codex"


async def test_d2_uses_model_vendor_for_api_workers() -> None:
    opts = _opts("validator", workers=["api"], models=["anthropic/c", "openai/g"])
    req = r.d2_validator(PLAN, 1, opts, "planner", "api", "anthropic/opus")
    assert (await _answers(req))["model"] == "openai/g"


async def test_d2_without_preference_takes_first() -> None:
    req = r.d2_validator(PLAN, 1, _opts("validator"))
    assert (await _answers(req))["worker"] == "codex"


@pytest.mark.parametrize(
    ("verdict", "round_", "expected"),
    [("approve", 1, "approve"), ("revise", 1, "revise"), ("revise", 3, "escalate_human")],
)
async def test_d3(verdict: str, round_: int, expected: str) -> None:
    req = r.d3_after_validation(ValidationResult(verdict=verdict), round_, 3)  # type: ignore[arg-type]
    assert await _answers(req) == {"verdict": expected}


@pytest.mark.parametrize(
    ("files", "criteria", "tier", "effort"),
    [
        (1, 1, "trivial", "low"),
        (1, 2, "trivial", "low"),
        (2, 2, "standard", "medium"),
        (4, 1, "hard", "high"),
        (0, 8, "hard", "high"),
        # A one-function task with tests: two files and a planner's usual 5-6 criteria. The old
        # rule (criteria >= 5 -> hard) rated this "hard"/high in the 2026-10-12 smoke test.
        (2, 6, "standard", "medium"),
        (1, 5, "standard", "medium"),
    ],
)
async def test_d4_complexity_and_effort(files: int, criteria: int, tier: str, effort: str) -> None:
    card = TaskCard(
        id="T1",
        title="t",
        description="d",
        files_hint=[f"f{i}" for i in range(files)],
        acceptance_criteria=[f"c{i}" for i in range(criteria)],
    )
    answers = await _answers(r.d4_task(card, _opts("coder"), 0, 5))
    assert (answers["complexity"], answers["effort"]) == (tier, effort)
    assert complexity(r.task_card(card)) == tier


async def test_d4_effort_falls_back_to_nearest_allowed_level() -> None:
    card = TaskCard(id="T1", title="t", description="d", files_hint=list("abcde"))
    answers = await _answers(r.d4_task(card, _opts("coder", efforts=["low", "medium"]), 0, 5))
    assert answers["effort"] == "medium"


def _d5(**state: Any) -> DecisionRequest:
    base: dict[str, Any] = {
        "attempt": 1,
        "max_attempts": 3,
        "assignment": {},
        "worker_status": "success",
        "summary": "",
        "tests": "passed",
        "review": "skipped",
        "can_reassign": True,
        "task_spend_usd": 0,
        "task_budget_usd": 1,
    }
    return r.d5_after_attempt(CARD, **{**base, **state})


@pytest.mark.parametrize(
    ("state", "action"),
    [
        ({}, "done"),
        ({"tests": "skipped"}, "done"),
        ({"tests": "failed"}, "retry"),
        ({"review": "revise"}, "retry"),
        ({"worker_status": "failure", "attempt": 1}, "retry"),
        ({"worker_status": "timeout", "attempt": 2}, "escalate"),
        ({"worker_status": "error", "attempt": 3}, "reassign"),
        ({"worker_status": "error", "attempt": 3, "can_reassign": False}, "human"),
        ({"worker_status": "quota_exhausted"}, "reassign"),
        ({"worker_status": "quota_exhausted", "can_reassign": False}, "human"),
        ({"quality": "passed"}, "done"),
        (
            {"quality": "failed", "quality_findings": ["critical bug a.py:1: x (ai-review)"]},
            "retry",
        ),
        ({"quality": "failed", "attempt": 2}, "escalate"),
        ({"quality": "failed", "attempt": 3, "can_reassign": False}, "human"),
    ],
)
async def test_d5(state: dict[str, Any], action: str) -> None:
    assert await _answers(_d5(**state)) == {"action": action}


@pytest.mark.parametrize(
    ("merged", "tests", "rounds", "action"),
    [
        (True, "passed", 0, "ship"),
        (True, "skipped", 0, "ship"),
        (True, "passed", 2, "ship"),
        (True, "failed", 0, "fix_task"),
        (True, "failed", 1, "fix_task"),
        (True, "failed", 2, "human"),  # fix rounds used up
        (False, "skipped", 0, "human"),  # merge conflicts: a fix task would conflict too
    ],
)
async def test_d6(merged: bool, tests: str, rounds: int, action: str) -> None:
    conflicts = [] if merged else ["T2"]
    req = r.d6_integration(2, merged, conflicts, tests, fix_rounds=rounds, max_fix_rounds=2)
    assert await _answers(req) == {"action": action}


@pytest.mark.parametrize(
    ("quality", "rounds", "action"),
    [("passed", 0, "ship"), ("failed", 0, "fix_task"), ("failed", 2, "human")],
)
async def test_d6_quality_gate(quality: str, rounds: int, action: str) -> None:
    req = r.d6_integration(
        1,
        True,
        [],
        "passed",
        fix_rounds=rounds,
        max_fix_rounds=2,
        quality=quality,
        quality_findings=["2 critical finding(s)"],
    )
    assert await _answers(req) == {"action": action}
    if quality == "failed":
        assert req.state["quality_failed"] is True
        assert req.state["quality_findings"] == ["2 critical finding(s)"]


def test_quality_state_is_compact() -> None:
    long = ["x" * 500] * 9
    req = _d5(quality="failed", quality_findings=long)
    assert len(req.state["quality_findings"]) == 5
    assert all(len(f) <= 200 for f in req.state["quality_findings"])
    assert "quality" not in _d5().state
