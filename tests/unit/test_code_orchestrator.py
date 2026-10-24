"""The Coding Decision Agent as code orchestrator (issue #187).

Everything here is pure or fake-backed: the real model is never loaded in
tests (it is a 421M checkpoint behind `laya` + torch, and it is not a test
dependency). The gates are what matter, and they are tested exactly:
confidence first, label second, missing confidence never trusted.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jev_agent.code_orchestrator import (
    CodeOrchestrator,
    CodeOrchestratorUnavailable,
    CodeReviewGrade,
    RoutingGrade,
    compact_diff,
    gate_code_review,
    gate_trace,
    load_backend,
    normalise_code_review,
    normalise_routing,
    routing_hint,
)
from jev_agent.code_orchestrator.grades import AgentTraceGrade
from jev_agent.config.schema import CodeOrchestratorSettings, JevConfig
from jev_agent.quality.gate import apply_code_grade
from jev_agent.quality.models import GateResult


def _grade(**overrides: object) -> CodeReviewGrade:
    values: dict[str, object] = {
        "code_quality": 3,
        "instruction_followed": True,
        "likely_correct": True,
        "merge_action": "accept",
        "answer_confidence": {"merge_action": 0.9},
    }
    values.update(overrides)
    return CodeReviewGrade(**values)  # type: ignore[arg-type]


def _result() -> GateResult:
    return GateResult(level="basic", scope="task", task_id="T1", number=1)


class _FakeBackend:
    """A backend returning canned verdict objects, like the real package's."""

    def __init__(self, merge_action: str = "accept", confidence: float = 0.9) -> None:
        self.merge_action = merge_action
        self.confidence = confidence

    def grade_code(self, *, task: str, diff: str, tests: str) -> object:
        return {
            "code_quality": 3,
            "instruction_followed": "yes",
            "likely_correct": "yes",
            "merge_action": self.merge_action,
            "answer_confidence": {"merge_action": self.confidence},
        }

    def route_model(self, request: str) -> object:
        return {
            "model_tier": "frontier",
            "task_difficulty": 2,
            "skill": "refactor",
            "answer_confidence": {"model_tier": 0.8},
        }


def test_confident_accept_passes() -> None:
    outcome = gate_code_review(_grade())
    assert outcome.decision == "accept"
    assert outcome.merge_action == "accept"


def test_unconfident_accept_goes_to_human_not_through() -> None:
    # The core rule: confidence is checked before the label.
    outcome = gate_code_review(_grade(answer_confidence={"merge_action": 0.5}))
    assert outcome.decision == "human_review"


def test_missing_confidence_is_never_trusted() -> None:
    outcome = gate_code_review(_grade(answer_confidence={}))
    assert outcome.decision == "human_review"
    assert outcome.confidence == 0.0


def test_request_changes_and_needs_tests_are_rework() -> None:
    for action in ("request_changes", "needs_tests"):
        assert gate_code_review(_grade(merge_action=action)).decision == "rework"


def test_confident_reject_blocks() -> None:
    assert gate_code_review(_grade(merge_action="reject")).decision == "block"


def test_threshold_is_configurable_and_validated() -> None:
    assert (
        gate_code_review(_grade(answer_confidence={"merge_action": 0.6}), threshold=0.5).decision
        == "accept"
    )
    with pytest.raises(ValueError, match="threshold must be 0-1"):
        gate_code_review(_grade(), threshold=1.5)


def test_trace_gate_never_downgrades_stop_or_human_review() -> None:
    for action, decision in (("stop", "block"), ("human_review", "human_review")):
        grade = AgentTraceGrade(
            action=action,  # type: ignore[arg-type]
            needs_review=True,
            outcome="failure",
            risk=3,
            urgency=3,
            answer_confidence={"action": 0.99},
        )
        assert gate_trace(grade).decision == decision
    low = AgentTraceGrade(
        action="continue",
        needs_review=False,
        outcome="success",
        risk=0,
        urgency=0,
        answer_confidence={"action": 0.4},
    )
    assert gate_trace(low).decision == "human_review"


def test_routing_hint_maps_tiers_to_effort() -> None:
    hint = routing_hint(
        RoutingGrade(
            model_tier="small_fast",
            task_difficulty=0,
            skill="code_edit",
            answer_confidence={"model_tier": 0.9},
        )
    )
    assert hint["suggested_effort"] == "low"
    assert hint["skill"] == "code_edit"
    hint = routing_hint(
        RoutingGrade(
            model_tier="reasoning",
            task_difficulty=3,
            skill="plan",
            answer_confidence={"model_tier": 0.9},
        )
    )
    assert hint["suggested_effort"] == "xhigh"


def test_compact_diff_passes_short_diffs_through() -> None:
    diff = "diff --git a/x.py b/x.py\n+print(1)\n"
    assert compact_diff(diff) == diff


def test_compact_diff_marks_truncation() -> None:
    diff = "\n".join(f"+line {i}" for i in range(500))
    compacted = compact_diff(diff, max_chars=500)
    assert len(compacted) <= 500
    assert "omitted to fit" in compacted


def test_normalise_rejects_unknown_labels() -> None:
    with pytest.raises(ValueError, match="unknown merge_action"):
        normalise_code_review(
            {
                "code_quality": 3,
                "instruction_followed": "yes",
                "likely_correct": "yes",
                "merge_action": "ship_it",
                "answer_confidence": {},
            }
        )
    with pytest.raises(ValueError, match="code_quality out of 0-4"):
        normalise_code_review(
            {
                "code_quality": 9,
                "instruction_followed": "yes",
                "likely_correct": "yes",
                "merge_action": "accept",
                "answer_confidence": {},
            }
        )
    with pytest.raises(ValueError, match="unknown model_tier"):
        normalise_routing(
            {"model_tier": "huge", "task_difficulty": 1, "skill": "plan", "answer_confidence": {}}
        )


def test_backend_missing_is_a_clear_error_not_a_traceback_shape() -> None:
    # The package is not installed in the test env; the loader must say so
    # plainly and name the install, not raise ImportError.
    with pytest.raises(CodeOrchestratorUnavailable, match="not installed"):
        load_backend("S4MPL3BI4S/Coding_Decision_Agent")


def test_fake_backend_grades_and_routes() -> None:
    orch = CodeOrchestrator(backend=_FakeBackend())
    assert orch.available is True
    grade = orch.grade_code(task="fix x", diff="+x\n")
    assert grade.merge_action == "accept"
    assert orch.route("refactor billing").model_tier == "frontier"


def test_config_defaults_are_off_and_threshold_is_0_7() -> None:
    settings = JevConfig().code_orchestrator
    assert settings.enabled is False
    assert settings.model_id == "S4MPL3BI4S/Coding_Decision_Agent"
    assert settings.confidence_threshold == 0.7
    assert settings.max_diff_chars == 7000


def test_config_parses_enabled_section_and_rejects_bad_threshold() -> None:
    cfg = JevConfig.model_validate(
        {"code_orchestrator": {"enabled": True, "confidence_threshold": 0.8}}
    )
    assert cfg.code_orchestrator.enabled is True
    with pytest.raises(ValidationError):
        JevConfig.model_validate({"code_orchestrator": {"confidence_threshold": 1.5}})


def test_laya_section_still_parses_for_backward_compatibility() -> None:
    # Existing configs referencing Laya must migrate cleanly: they still
    # validate, unchanged. (Deprecation is documented, not enforced.)
    cfg = JevConfig.model_validate({"dispatcher": {"laya": {"min_margin": 0.3}}})
    assert cfg.dispatcher.laya.min_margin == 0.3


def test_apply_code_grade_blocks_on_reject_and_records_grade() -> None:
    result = apply_code_grade(
        _result(),
        settings=CodeOrchestratorSettings(enabled=True),
        task_title="Fix x",
        diff="+x\n",
        orchestrator=CodeOrchestrator(backend=_FakeBackend("reject", 0.95)),
    )
    assert result.blocked is True
    assert result.code_grade is not None
    assert result.code_grade["status"] == "graded"
    assert result.code_grade["gate"]["decision"] == "block"
    assert any("code orchestrator" in reason for reason in result.reasons)


def test_apply_code_grade_accept_passes_without_blocking() -> None:
    result = apply_code_grade(
        _result(),
        settings=CodeOrchestratorSettings(enabled=True),
        task_title="Fix x",
        diff="+x\n",
        orchestrator=CodeOrchestrator(backend=_FakeBackend("accept", 0.95)),
    )
    assert result.blocked is False
    assert result.code_grade is not None
    assert result.code_grade["gate"]["decision"] == "accept"


def test_apply_code_grade_low_confidence_blocks_as_human_review() -> None:
    result = apply_code_grade(
        _result(),
        settings=CodeOrchestratorSettings(enabled=True),
        task_title="Fix x",
        diff="+x\n",
        orchestrator=CodeOrchestrator(backend=_FakeBackend("accept", 0.4)),
    )
    assert result.blocked is True
    assert result.code_grade is not None
    assert result.code_grade["gate"]["decision"] == "human_review"


def test_apply_code_grade_unavailable_never_blocks() -> None:
    result = apply_code_grade(
        _result(),
        settings=CodeOrchestratorSettings(enabled=True),
        task_title="Fix x",
        diff="+x\n",
        orchestrator=CodeOrchestrator(backend=None),
    )
    assert result.blocked is False
    assert result.code_grade is not None
    assert result.code_grade["status"] == "unavailable"
