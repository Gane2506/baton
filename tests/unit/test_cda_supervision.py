"""Async trace supervision (issue #379, CDA Phase 2).

Covers: the gate-to-supervision mapping (including low-confidence
continue/observe becoming human_review and stop never being
downgraded), the blind supervisor (unavailable backend observes, never
scores), inference failures failing closed to human_review, unusable
grades failing closed the same way, tool calls observed but never
gated, the async entry points, stage-boundary recording, and the
supervisor's ledger writes (including unavailable with no quality
fields).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from jev_agent.code_orchestrator import (
    AgentTraceGrade,
    CodeOrchestratorError,
    CodeOrchestratorUnavailable,
    RatingLedger,
    ToolCallGrade,
    TraceSupervisor,
)
from jev_agent.code_orchestrator.grades import TraceAction


def _trace(action: str, confidence: float) -> AgentTraceGrade:
    return AgentTraceGrade(
        action=cast("TraceAction", action),
        needs_review=False,
        outcome="in_progress",
        risk=1,
        urgency=1,
        answer_confidence={"action": confidence},
    )


def _tool_call() -> ToolCallGrade:
    return ToolCallGrade(
        should_call_tool="yes",
        call_verdict="correct",
        answer_confidence={"should_call_tool": 0.9, "call_verdict": 0.85},
    )


class _FakeBackend:
    """A backend that returns canned grades, or raises on demand."""

    def __init__(
        self,
        *,
        trace_grade: AgentTraceGrade | None = None,
        tool_grade: ToolCallGrade | None = None,
        error: Exception | None = None,
    ) -> None:
        self._trace_grade = trace_grade
        self._tool_grade = tool_grade
        self._error = error

    def grade_trace(self, trace: str) -> AgentTraceGrade:
        if self._error is not None:
            raise self._error
        assert self._trace_grade is not None
        return self._trace_grade

    def grade_tool_call(self, tool_call: str) -> ToolCallGrade:
        if self._error is not None:
            raise self._error
        assert self._tool_grade is not None
        return self._tool_grade


@pytest.mark.parametrize(
    ("action", "confidence", "expected"),
    [
        ("continue", 0.9, "continue"),
        ("observe", 0.9, "observe"),
        ("human_review", 0.9, "human_review"),
        ("human_review", 0.2, "human_review"),
        ("stop", 0.9, "stop"),
        ("stop", 0.2, "stop"),
        ("continue", 0.5, "human_review"),
        ("observe", 0.5, "human_review"),
    ],
)
def test_supervise_trace_maps_gate_to_action(action: str, confidence: float, expected: str) -> None:
    supervisor = TraceSupervisor(_FakeBackend(trace_grade=_trace(action, confidence)))
    decision = supervisor.supervise_trace("agent trace text")
    assert decision.action == expected
    assert decision.trace_action == action
    assert decision.confidence == pytest.approx(confidence)
    assert decision.risk == 1
    assert decision.urgency == 1


def test_supervise_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError, match="threshold"):
        TraceSupervisor(_FakeBackend(trace_grade=_trace("continue", 0.9)), threshold=1.5)


def test_supervise_unavailable_observes_and_records_no_score(tmp_path: Path) -> None:
    ledger = RatingLedger(tmp_path / "cda-ratings.jsonl")
    supervisor = TraceSupervisor(
        _FakeBackend(error=CodeOrchestratorUnavailable("no backend installed")),
        ledger=ledger,
    )
    decision = supervisor.supervise_trace("trace", run_id="R1", task_id="T1")
    assert decision.action == "observe"
    assert decision.recorded is True
    assert "unavailable" in decision.reason
    assert decision.gate_decision == ""
    records = ledger.records()
    assert len(records) == 1
    assert records[0].kind == "trace"
    assert records[0].status == "unavailable"
    assert records[0].code_quality is None
    assert records[0].confidence is None
    assert records[0].gate_decision == ""


def test_supervise_error_fails_closed_to_human_review(tmp_path: Path) -> None:
    ledger = RatingLedger(tmp_path / "cda-ratings.jsonl")
    supervisor = TraceSupervisor(
        _FakeBackend(error=CodeOrchestratorError("model crashed")),
        ledger=ledger,
    )
    decision = supervisor.supervise_trace("trace")
    assert decision.action == "human_review"
    assert decision.recorded is True
    records = ledger.records()
    assert len(records) == 1
    assert records[0].status == "error"


def test_supervise_unusable_grade_fails_closed_to_human_review() -> None:
    supervisor = TraceSupervisor(_FakeBackend(trace_grade=_trace("bogus_action", 0.9)))
    decision = supervisor.supervise_trace("trace")
    assert decision.action == "human_review"
    assert "failed to grade" in decision.reason


def test_supervise_tool_call_is_observed_never_gated(tmp_path: Path) -> None:
    ledger = RatingLedger(tmp_path / "cda-ratings.jsonl")
    supervisor = TraceSupervisor(_FakeBackend(tool_grade=_tool_call()), ledger=ledger)
    decision = supervisor.supervise_tool_call("tool call text")
    assert decision.action == "observe"
    assert "should_call_tool=yes" in decision.tool_verdict
    assert "call_verdict=correct" in decision.tool_verdict
    assert decision.recorded is True
    records = ledger.records()
    assert len(records) == 1
    assert records[0].kind == "tool_call"
    assert records[0].status == "graded"
    assert "call_verdict=correct" in records[0].detail


def test_supervise_tool_call_unavailable_observes(tmp_path: Path) -> None:
    ledger = RatingLedger(tmp_path / "cda-ratings.jsonl")
    supervisor = TraceSupervisor(
        _FakeBackend(error=CodeOrchestratorUnavailable("no backend")),
        ledger=ledger,
    )
    decision = supervisor.supervise_tool_call("tool call text")
    assert decision.action == "observe"
    assert ledger.records()[0].status == "unavailable"


def test_supervise_tool_call_error_fails_closed(tmp_path: Path) -> None:
    supervisor = TraceSupervisor(_FakeBackend(error=ValueError("bad verdict shape")))
    decision = supervisor.supervise_tool_call("tool call text")
    assert decision.action == "human_review"


async def test_supervise_trace_async_returns_decision() -> None:
    supervisor = TraceSupervisor(_FakeBackend(trace_grade=_trace("continue", 0.95)))
    decision = await supervisor.supervise_trace_async("trace")
    assert decision.action == "continue"


async def test_supervise_tool_call_async_observes() -> None:
    supervisor = TraceSupervisor(_FakeBackend(tool_grade=_tool_call()))
    decision = await supervisor.supervise_tool_call_async("tool call")
    assert decision.action == "observe"


def test_supervise_at_boundary_records_stage(tmp_path: Path) -> None:
    ledger = RatingLedger(tmp_path / "cda-ratings.jsonl")
    supervisor = TraceSupervisor(_FakeBackend(trace_grade=_trace("observe", 0.9)), ledger=ledger)
    decision = supervisor.supervise_at_boundary("EXECUTING", "trace")
    assert decision.action == "observe"
    assert decision.stage == "EXECUTING"
    assert ledger.records()[0].detail == "stage=EXECUTING"


async def test_supervise_at_boundary_async_records_stage(tmp_path: Path) -> None:
    ledger = RatingLedger(tmp_path / "cda-ratings.jsonl")
    supervisor = TraceSupervisor(_FakeBackend(trace_grade=_trace("continue", 0.9)), ledger=ledger)
    decision = await supervisor.supervise_at_boundary_async("VALIDATING", "trace")
    assert decision.action == "continue"
    assert decision.stage == "VALIDATING"
    assert ledger.records()[0].detail == "stage=VALIDATING"


def test_supervise_without_ledger_still_decides() -> None:
    supervisor = TraceSupervisor(_FakeBackend(trace_grade=_trace("stop", 0.9)))
    decision = supervisor.supervise_trace("trace")
    assert decision.action == "stop"
    assert decision.recorded is False
