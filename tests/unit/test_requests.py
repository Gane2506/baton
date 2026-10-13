from pathlib import Path

from jev_agent.config import JevConfig
from jev_agent.core.models import ChoiceChallenge, Idea, Plan, TaskCard, ValidationResult
from jev_agent.dispatch import requests as r


def _config(**roles: dict[str, object]) -> JevConfig:
    return JevConfig.model_validate({"roles": roles})


ALL = ["claude_code", "codex", "api"]


def test_role_options_from_config() -> None:
    cfg = _config(coder={"models": ["a/x", "b/y"]})
    opts = r.role_options(cfg, "coder", ALL)
    assert opts.workers == ["claude_code", "codex"]
    assert opts.models == ["a/x", "b/y"]
    assert opts.efforts == ["low", "medium", "high"]


def test_empty_pool_uses_default_sentinel() -> None:
    opts = r.role_options(_config(coder={"efforts": []}), "coder", ALL)
    assert opts.models == [r.DEFAULT_MODEL]
    assert opts.efforts == [r.DEFAULT_EFFORT]


def test_disabled_and_unregistered_workers_are_excluded() -> None:
    cfg = JevConfig.model_validate({"workers": {"codex": {"enabled": False, "command": "codex"}}})
    assert r.role_options(cfg, "validator", ALL).workers == ["api"]
    assert r.role_options(cfg, "coder", ["codex", "api"]).workers == []


def test_fixed_selection_offers_one_choice() -> None:
    cfg = _config(planner={"selection": "fixed", "models": ["m1", "m2"]})
    opts = r.role_options(cfg, "planner", ALL)
    assert (opts.workers, opts.models) == (["claude_code"], ["m1"])


def test_vendor_of() -> None:
    assert r.vendor_of("api", "openai/gpt-x") == "openai"
    assert r.vendor_of("claude_code", None) == "anthropic"
    assert r.vendor_of("codex", "gpt-5") == "openai"
    assert r.vendor_of("api", None) is None


def test_requests_have_stable_questions_and_compact_state(tmp_path: Path) -> None:
    opts = r.role_options(JevConfig(), "coder", ALL)
    idea = Idea(text="x" * 5000, repo_path=tmp_path)
    d1 = r.d1_intake(idea, opts)
    assert set(d1.questions) == {"clear", "worker", "model", "effort"}
    assert len(d1.state["idea"]) == 1000

    card = TaskCard(id="T1", title="t", description="d" * 5000, files_hint=["a", "b"])
    plan = Plan(summary="s", tasks=[card])
    d2 = r.d2_validator(plan, 1, opts, "planner", "claude_code", None)
    assert d2.state["prefer_different_vendor_than"]["vendor"] == "anthropic"
    assert "d" * 100 not in str(d2.state)  # descriptions are never sent

    d3 = r.d3_after_validation(ValidationResult(verdict="revise", issues=["i"] * 9), 1, 3)
    assert d3.questions == {"verdict": ["approve", "revise", "escalate_human"]}
    assert len(d3.state["validation"]["issues"]) == 5

    d4 = r.d4_task(card, opts, 0.5, 5.0)
    assert d4.questions["complexity"] == ["trivial", "standard", "hard"]
    assert d4.state["task"]["files_count"] == 2

    d5 = r.d5_after_attempt(
        card,
        attempt=1,
        max_attempts=3,
        assignment={"worker": "codex", "model": None, "effort": "low"},
        worker_status="failure",
        summary="s",
        tests="skipped",
        review="skipped",
        can_reassign=True,
        task_spend_usd=0,
        task_budget_usd=1.5,
    )
    assert d5.questions == {"action": ["done", "retry", "escalate", "reassign", "human"]}
    d6 = r.d6_integration(1, True, [], "passed", fix_rounds=1, max_fix_rounds=2)
    assert d6.questions == {"action": ["ship", "fix_task", "human"]}
    assert (d6.state["fix_rounds"], d6.state["max_fix_rounds"]) == (1, 2)


def test_d3_carries_the_failed_challenges_compactly() -> None:
    challenges = [ChoiceChallenge(choice="kept", holds=True)] + [
        ChoiceChallenge(choice=f"c{i}", alternative="y", reason="r" * 500, holds=False)
        for i in range(7)
    ]
    card = r.d3_after_validation(ValidationResult(verdict="revise", challenges=challenges), 1, 3)
    validation = card.state["validation"]
    assert (validation["challenge_count"], validation["failed_challenge_count"]) == (8, 7)
    failed = validation["failed_challenges"]
    assert [c["choice"] for c in failed] == ["c0", "c1", "c2", "c3", "c4"]
    assert len(failed[0]["reason"]) == 160 and "kept" not in str(failed)


def test_d3_without_challenges() -> None:
    validation = r.d3_after_validation(ValidationResult(verdict="approve"), 1, 3).state[
        "validation"
    ]
    assert (validation["challenge_count"], validation["failed_challenges"]) == (0, [])
