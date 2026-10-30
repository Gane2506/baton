"""Pre-flight plan critique at VALIDATING (issue #380, CDA Phase 3).

Covers: the pure `critique_plan` cases (unavailable, inconclusive,
confident notes from the model signal and from structure, threshold
validation and boundary) and the pipeline `_validate` hook's pass-through
branches (disabled, missing backend, low confidence, routing failure)
plus its note-appending branch.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from jev_agent.code_orchestrator import RoutingGrade, critique_plan
from jev_agent.code_orchestrator.grades import Skill
from jev_agent.core.models import Plan, TaskCard, ValidationResult


def _routing(difficulty: int, confidence: float, skill: str = "debug") -> RoutingGrade:
    return RoutingGrade(
        model_tier="mid",
        task_difficulty=difficulty,
        skill=cast("Skill", skill),
        answer_confidence={"model_tier": confidence},
    )


def _task(title: str = "Do it", description: str = "Do the thing") -> tuple[str, str]:
    return (title, description)


def test_critique_unavailable_has_no_notes() -> None:
    critique = critique_plan([_task()], grade=None)
    assert critique.status == "unavailable"
    assert critique.notes == ()
    assert critique.difficulty is None


def test_critique_inconclusive_below_threshold_has_no_notes() -> None:
    critique = critique_plan([_task()], grade=_routing(3, 0.5))
    assert critique.status == "inconclusive"
    assert critique.notes == ()
    # The grade still rides along for observability, just not as notes.
    assert critique.difficulty == 3
    assert critique.skill == "debug"
    assert critique.confidence == pytest.approx(0.5)


def test_critique_at_threshold_is_available() -> None:
    critique = critique_plan([_task()], grade=_routing(1, 0.7))
    assert critique.status == "available"
    assert critique.notes == ()


def test_critique_confident_easy_plan_has_no_notes() -> None:
    critique = critique_plan([_task(), _task()], grade=_routing(1, 0.9))
    assert critique.status == "available"
    assert critique.notes == ()


def test_critique_maximum_difficulty_suggests_splitting() -> None:
    critique = critique_plan([_task()], grade=_routing(3, 0.9))
    assert critique.status == "available"
    assert len(critique.notes) == 1
    assert critique.notes[0].startswith("CDA plan critique: ")
    assert "splitting" in critique.notes[0]


def test_critique_empty_plan_and_blank_tasks_are_noted() -> None:
    critique = critique_plan([], grade=_routing(1, 0.9))
    assert any("no tasks" in note for note in critique.notes)
    critique = critique_plan(
        [("", "has no title"), ("Titled", ""), ("", "")],
        grade=_routing(1, 0.9),
    )
    assert any("2 task(s) have no title" in note for note in critique.notes)
    assert any("2 task(s) have no description" in note for note in critique.notes)


def test_critique_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError, match="threshold"):
        critique_plan([_task()], grade=_routing(1, 0.9), threshold=1.5)


def _plan() -> Plan:
    return Plan(
        summary="Build the thing",
        tasks=[
            TaskCard(id="T1", title="First", description="First thing"),
            TaskCard(id="T2", title="Second", description="Second thing"),
        ],
    )


def _validation() -> ValidationResult:
    return ValidationResult(verdict="approve", issues=["validator issue"])


def _pipeline(monkeypatch: pytest.MonkeyPatch, *, enabled: bool, orchestrator: Any) -> Any:
    import jev_agent.core.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "CodeOrchestrator", orchestrator)
    harness = SimpleNamespace(
        store=SimpleNamespace(),
        config=SimpleNamespace(
            code_orchestrator=SimpleNamespace(
                enabled=enabled,
                model_id="test",
                max_diff_chars=7000,
                confidence_threshold=0.7,
            )
        ),
    )
    return pipeline_mod.Pipeline(cast(Any, harness))


class _FakeOrchestrator:
    """A routing backend returning a canned grade, or raising on demand."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._grade: RoutingGrade | None = kwargs.pop("grade", None)
        self._error: Exception | None = kwargs.pop("error", None)
        self._available: bool = kwargs.pop("available", True)

    @property
    def available(self) -> bool:
        return self._available

    def route(self, request: str) -> RoutingGrade:
        if self._error is not None:
            raise self._error
        assert self._grade is not None
        return self._grade


async def test_validate_hook_appends_notes_on_confident_hard_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = _pipeline(
        monkeypatch,
        enabled=True,
        orchestrator=lambda *a, **k: _FakeOrchestrator(grade=_routing(3, 0.9)),
    )
    validation = await pipeline._critique_plan(_plan(), _validation())
    assert validation.issues[0] == "validator issue"  # the validator's own issue stays first
    assert any("splitting" in issue for issue in validation.issues[1:])
    assert validation.verdict == "approve"  # advisory only: D3's verdict logic is untouched


async def test_validate_hook_passes_through_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = _pipeline(
        monkeypatch,
        enabled=False,
        orchestrator=lambda *a, **k: _FakeOrchestrator(grade=_routing(3, 0.9)),
    )
    validation = await pipeline._critique_plan(_plan(), _validation())
    assert validation.issues == ["validator issue"]


async def test_validate_hook_passes_through_when_backend_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = _pipeline(
        monkeypatch,
        enabled=True,
        orchestrator=lambda *a, **k: _FakeOrchestrator(available=False),
    )
    validation = await pipeline._critique_plan(_plan(), _validation())
    assert validation.issues == ["validator issue"]


async def test_validate_hook_passes_through_on_low_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = _pipeline(
        monkeypatch,
        enabled=True,
        orchestrator=lambda *a, **k: _FakeOrchestrator(grade=_routing(3, 0.5)),
    )
    validation = await pipeline._critique_plan(_plan(), _validation())
    assert validation.issues == ["validator issue"]


async def test_validate_hook_passes_through_on_routing_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jev_agent.code_orchestrator import CodeOrchestratorError

    pipeline = _pipeline(
        monkeypatch,
        enabled=True,
        orchestrator=lambda *a, **k: _FakeOrchestrator(
            error=CodeOrchestratorError("model crashed")
        ),
    )
    validation = await pipeline._critique_plan(_plan(), _validation())
    assert validation.issues == ["validator issue"]
