"""Async trace supervision (issue #379, CDA Phase 2).

Phase 1 graded traces only when a human asked (`jev code trace`) — the
pipeline never watched a running agent. This module is the watcher: given
a running agent's trace text, it grades the trace through the backend,
runs the grade through `gate_trace`, and returns a supervision decision
the caller acts on:

- `continue` — the trace grade is confident and says keep going.
- `observe` — keep going, but watch the next step (a confident `observe`
  grade, or a blind supervisor: the backend is missing, so no grade ran
  and there is nothing to act on yet).
- `human_review` — a human must decide (the trace grade itself says so,
  a confident grade is missing, or grading failed).
- `stop` — the trace grade says stop, confidently enough to act on.

The mapping from gate to supervision action is fixed, not interpreted
per call site: gate `accept` -> `continue`, `rework` -> `observe`,
`human_review` -> `human_review`, `block` -> `stop`. The gate already
encodes the confidence rules (a low-confidence `continue` is not
trusted, a `stop` is never downgraded); the supervisor only translates
its vocabulary into the loop's vocabulary.

Two rules the rest of the code must not break:

- Supervision never blocks the pipeline. Grading takes minutes on CPU,
  so the async entry points run the blocking backend call in a thread
  (`asyncio.to_thread`), exactly like Phase 1's write-after-grade
  ledger writes. A stage-boundary caller awaits the supervision from a
  background task or not at all — never inline in the run's hot path.
  The sync entry points exist for the CLI, which has no loop to block.
- Supervision never decides anything on its own beyond the decision it
  returns. The caller (pipeline, dashboard, human) acts on it; the
  supervisor does not halt runs, cancel tasks, or change assignments.
  A decision is advisory the same way a routing hint is: it carries the
  grade's evidence and the gate's reason, and the caller owns the act.

Tool calls are sampled, not gated. The `tool_call` family documents no
closed label set for `should_call_tool`/`call_verdict` (see
`backend.normalise_tool_call`: it validates shape only and refuses to
invent labels to coerce into), so there is no gate function for tool
calls and this module invents none. A sampled tool call is graded,
recorded to the ledger, and returned as `observe` with its verdict in
the reason — watched, never acted on by label guesswork. An unavailable
backend observes blind; an inference failure escalates to
`human_review`, the same fail-closed rule as a trace grading failure.

Ledger rules from Phase 1 still hold: a grade that did not run records
`status="unavailable"` with no quality fields, never a score; an
inference failure records `status="error"`; writes are best-effort and
never raise (the supervisor reports `recorded=False` when the ledger
could not be written, and still returns its decision — a lost grade
must not lose the supervision answer).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from jev_agent.code_orchestrator.backend import (
    CodeOrchestratorError,
    CodeOrchestratorUnavailable,
)
from jev_agent.code_orchestrator.grades import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    AgentTraceGrade,
    ToolCallGrade,
    gate_trace,
)
from jev_agent.code_orchestrator.ledger import (
    GradeRecord,
    RatingLedger,
    trace_record,
    unavailable_record,
)

__all__ = [
    "SupervisionAction",
    "SupervisionDecision",
    "TraceGraderBackend",
    "TraceSupervisor",
]

log = logging.getLogger(__name__)

SupervisionAction = Literal["continue", "observe", "human_review", "stop"]

_GATE_TO_SUPERVISION: dict[str, SupervisionAction] = {
    "accept": "continue",
    "rework": "observe",
    "human_review": "human_review",
    "block": "stop",
}


class TraceGraderBackend(Protocol):
    """The backend surface supervision needs: trace and tool-call grading."""

    def grade_trace(self, trace: str) -> AgentTraceGrade: ...
    def grade_tool_call(self, tool_call: str) -> ToolCallGrade: ...


@dataclass(frozen=True)
class SupervisionDecision:
    """One supervision answer: what the caller should do with a running trace."""

    action: SupervisionAction
    reason: str
    gate_decision: str = ""  # GateDecision, or "" when no grade ran
    confidence: float = 0.0  # calibrated action confidence, or 0.0 when blind
    trace_action: str = ""  # the model's raw trace action, or "" when blind
    risk: int | None = None
    urgency: int | None = None
    recorded: bool = False  # whether the grade reached the ledger
    stage: str = ""  # stage-boundary name, e.g. "EXECUTING"; "" when unscoped
    tool_verdict: str = ""  # sampled tool calls only: should_call_tool/verdict


class TraceSupervisor:
    """Supervise running agent traces through the trace gate (issue #379).

    Constructed with a backend (the real `CodeOrchestrator` or a fake in
    tests) and optionally a `RatingLedger`; every supervised grade is
    recorded to the ledger best-effort, and every decision carries the
    gate's reason so the caller can audit why it was told what it was
    told. The 0.7 threshold default is the model's calibrated gate, not
    a new bar: supervision trusts a trace grade exactly as far as the
    gate trusts a diff grade.
    """

    def __init__(
        self,
        backend: TraceGraderBackend,
        *,
        ledger: RatingLedger | None = None,
        threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be 0-1, got {threshold}")
        self._backend = backend
        self._ledger = ledger
        self._threshold = threshold

    def supervise_trace(
        self,
        trace: str,
        *,
        run_id: str = "",
        task_id: str = "",
        stage: str = "",
        source: str = "supervisor",
    ) -> SupervisionDecision:
        """Grade one trace and return its supervision decision.

        The stage name is recorded in the ledger detail when given: it is
        how a stage-boundary caller says where the trace was taken, and
        it costs the grade nothing to carry.
        """
        detail = f"stage={stage}" if stage else ""
        try:
            grade = self._backend.grade_trace(trace)
            # Inside the try deliberately: a grade the gate cannot use
            # (unknown action, risk out of range) is an unusable grade,
            # the same fail-closed case as a backend inference failure.
            outcome = gate_trace(grade, threshold=self._threshold)
        except CodeOrchestratorUnavailable as exc:
            recorded = self._record(
                unavailable_record(
                    "trace", detail=str(exc), run_id=run_id, task_id=task_id, source=source
                )
            )
            return SupervisionDecision(
                action="observe",
                reason=f"trace supervision blind: backend unavailable ({exc}); no grade ran",
                recorded=recorded,
                stage=stage,
            )
        except (CodeOrchestratorError, ValueError) as exc:
            recorded = self._record(
                GradeRecord(
                    kind="trace",
                    status="error",
                    source=source,
                    run_id=run_id,
                    task_id=task_id,
                    detail=str(exc),
                )
            )
            return SupervisionDecision(
                action="human_review",
                reason=f"trace supervision failed to grade: {type(exc).__name__}: {exc}",
                recorded=recorded,
                stage=stage,
            )
        recorded = self._record(
            trace_record(
                grade, gate=outcome, run_id=run_id, task_id=task_id, source=source, detail=detail
            )
        )
        return SupervisionDecision(
            action=_GATE_TO_SUPERVISION[outcome.decision],
            reason=outcome.reason,
            gate_decision=outcome.decision,
            confidence=outcome.confidence,
            trace_action=grade.action,
            risk=grade.risk,
            urgency=grade.urgency,
            recorded=recorded,
            stage=stage,
        )

    async def supervise_trace_async(
        self,
        trace: str,
        *,
        run_id: str = "",
        task_id: str = "",
        stage: str = "",
        source: str = "supervisor",
    ) -> SupervisionDecision:
        """`supervise_trace` without blocking the caller's loop.

        The blocking model call runs in a thread; the caller awaits this
        from a background task, never inline in the run's hot path.
        """
        return await asyncio.to_thread(
            self.supervise_trace,
            trace,
            run_id=run_id,
            task_id=task_id,
            stage=stage,
            source=source,
        )

    def supervise_at_boundary(
        self,
        stage: str,
        trace: str,
        *,
        run_id: str = "",
        task_id: str = "",
        source: str = "supervisor",
    ) -> SupervisionDecision:
        """The stage-boundary entry point: supervise a trace taken at `stage`.

        Stage-boundary callers (pipeline stage transitions, dashboard job
        checkpoints) pass the stage name so the decision and the ledger
        record say where the trace was taken. It is `supervise_trace`
        with the stage named, not a second supervision path.
        """
        return self.supervise_trace(
            trace, run_id=run_id, task_id=task_id, stage=stage, source=source
        )

    async def supervise_at_boundary_async(
        self,
        stage: str,
        trace: str,
        *,
        run_id: str = "",
        task_id: str = "",
        source: str = "supervisor",
    ) -> SupervisionDecision:
        """`supervise_at_boundary` without blocking the caller's loop."""
        return await asyncio.to_thread(
            self.supervise_at_boundary,
            stage,
            trace,
            run_id=run_id,
            task_id=task_id,
            source=source,
        )

    def supervise_tool_call(
        self,
        tool_call: str,
        *,
        run_id: str = "",
        task_id: str = "",
        stage: str = "",
        source: str = "supervisor",
    ) -> SupervisionDecision:
        """Grade one sampled tool call: observed, never gated.

        There is no gate function for tool calls because the family
        documents no closed label set; inventing one would fake model
        semantics. The grade is recorded and the decision is always
        `observe` with the verdict in the reason — the sample is watched,
        and a human or a later trace grade acts on it if it matters.
        """
        detail = f"stage={stage}" if stage else ""
        try:
            grade = self._backend.grade_tool_call(tool_call)
        except CodeOrchestratorUnavailable as exc:
            recorded = self._record(
                unavailable_record(
                    "tool_call", detail=str(exc), run_id=run_id, task_id=task_id, source=source
                )
            )
            return SupervisionDecision(
                action="observe",
                reason=f"tool-call supervision blind: backend unavailable ({exc}); no grade ran",
                recorded=recorded,
                stage=stage,
            )
        except (CodeOrchestratorError, ValueError) as exc:
            recorded = self._record(
                GradeRecord(
                    kind="tool_call",
                    status="error",
                    source=source,
                    run_id=run_id,
                    task_id=task_id,
                    detail=str(exc),
                )
            )
            return SupervisionDecision(
                action="human_review",
                reason=f"tool-call supervision failed to grade: {type(exc).__name__}: {exc}",
                recorded=recorded,
                stage=stage,
            )
        verdict = f"should_call_tool={grade.should_call_tool}; call_verdict={grade.call_verdict}"
        recorded = self._record(
            GradeRecord(
                kind="tool_call",
                status="graded",
                source=source,
                run_id=run_id,
                task_id=task_id,
                detail=(f"{detail}; {verdict}" if detail else verdict),
            )
        )
        return SupervisionDecision(
            action="observe",
            reason=f"sampled tool call observed: {verdict}",
            recorded=recorded,
            stage=stage,
            tool_verdict=verdict,
        )

    async def supervise_tool_call_async(
        self,
        tool_call: str,
        *,
        run_id: str = "",
        task_id: str = "",
        stage: str = "",
        source: str = "supervisor",
    ) -> SupervisionDecision:
        """`supervise_tool_call` without blocking the caller's loop."""
        return await asyncio.to_thread(
            self.supervise_tool_call,
            tool_call,
            run_id=run_id,
            task_id=task_id,
            stage=stage,
            source=source,
        )

    def _record(self, entry: GradeRecord) -> bool:
        """Best-effort ledger write: False when there is no ledger or the
        write failed. The decision is returned either way."""
        if self._ledger is None:
            return False
        try:
            return self._ledger.record(entry)
        except Exception as exc:  # ledger.record never raises; belt and braces
            log.debug("trace supervision ledger write failed: %s: %s", type(exc).__name__, exc)
            return False
