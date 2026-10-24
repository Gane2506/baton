"""Pure grading and gating for the Coding Decision Agent (issue #187).

Roles, stated once so the rest of the code cannot drift from them:

- Jev is the main orchestrator. It owns every pipeline decision (D1-D8) and the
  pipeline itself (INTAKE -> PLANNING -> VALIDATING -> AWAITING_APPROVAL ->
  EXECUTING -> INTEGRATING -> DONE). Nothing in this module makes a pipeline
  decision on its own.
- The Coding Decision Agent is the orchestrator *for code-writing agents*.
  When the work is code, it grades a diff, routes a coding task to a model tier
  and skill, grades a tool call or an agent trace, and proposes a merge action.
  Jev (and the quality gate) still decide what happens with that proposal.

The model is `S4MPL3BI4S/Coding_Decision_Agent`, a Laya derivative fine-tuned
to grade coding agent work (Apache-2.0). It does not write code and does not
emit prose: it returns labels plus calibrated probabilities. This module never
talks to the model; `backend.py` does that, behind a protocol tests can fake.
Everything here is pure, so the gates are testable without the model installed.

Gating rule (from the model card): gate on `answer_confidence` -- the
calibrated probability -- never on the entropy-style `confidence` field, and
never on a label alone. A merge action is trusted only when its
`answer_confidence` reaches the configured threshold (0.7 by default). Below
it, the outcome is `human_review` whatever the label says: an unconfident
"accept" is not an accept.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "CODE_QUALITY_MAX",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_MODEL_ID",
    "MAX_CONTEXT_CHARS",
    "AgentTraceGrade",
    "CodeReviewGrade",
    "GateDecision",
    "GateOutcome",
    "MergeAction",
    "ModelTier",
    "RoutingGrade",
    "Skill",
    "ToolCallGrade",
    "compact_diff",
    "gate_code_review",
    "gate_trace",
    "routing_hint",
]

# The model card's starting point for the calibrated-confidence gate.
DEFAULT_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_MODEL_ID = "S4MPL3BI4S/Coding_Decision_Agent"
# The model's context is 2048 tokens; at roughly 4 characters per token, with
# headroom kept for the fixed question wording, a compacted diff is capped here.
# This is a character budget, not a token count: no tokenizer is a dependency.
MAX_CONTEXT_CHARS = 7_000
CODE_QUALITY_MAX = 4

MergeAction = Literal["accept", "request_changes", "needs_tests", "reject"]
ModelTier = Literal["small_fast", "mid", "frontier", "reasoning"]
Skill = Literal[
    "code_edit",
    "debug",
    "write_tests",
    "refactor",
    "explain",
    "shell_ops",
    "research",
    "plan",
]
TraceAction = Literal["continue", "observe", "human_review", "stop"]
GateDecision = Literal["accept", "rework", "human_review", "block"]


def _finite_confidence(value: object) -> float:
    """A confidence value safe to gate on.

    Non-finite floats are a fail-open hole: `float("nan") < 0.7` is False, so
    a NaN-confidence "accept" would pass the gate. NaN, +inf and -inf (and
    non-numbers, including bools) are therefore distrusted as 0.0, never
    passed through (issue #192).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) else 0.0


@dataclass(frozen=True)
class CodeReviewGrade:
    """One `code_review` grading: labels plus their calibrated probabilities."""

    code_quality: int  # 0-4, the model's scale
    instruction_followed: bool
    likely_correct: bool
    merge_action: MergeAction
    # Calibrated probabilities keyed by question, e.g. {"merge_action": 0.82}.
    # Missing keys are treated as 0.0 by the gate (untrusted), never as confident.
    answer_confidence: dict[str, float] = field(default_factory=dict)

    def confidence_for(self, question: str) -> float:
        return _finite_confidence(self.answer_confidence.get(question, 0.0))


@dataclass(frozen=True)
class RoutingGrade:
    """One `routing` grading: which tier and skill a coding task needs."""

    model_tier: ModelTier
    task_difficulty: int  # 0-3
    skill: Skill
    answer_confidence: dict[str, float] = field(default_factory=dict)

    def confidence_for(self, question: str) -> float:
        return _finite_confidence(self.answer_confidence.get(question, 0.0))


@dataclass(frozen=True)
class ToolCallGrade:
    """One `tool_call` grading, kept for the trace/loop use, not the merge gate."""

    should_call_tool: str
    call_verdict: str
    answer_confidence: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentTraceGrade:
    """One `agent_trace` grading: what a running coding agent should do next."""

    action: TraceAction
    needs_review: bool
    outcome: str
    risk: int  # 0-3
    urgency: int  # 0-3
    answer_confidence: dict[str, float] = field(default_factory=dict)

    def confidence_for(self, question: str) -> float:
        return _finite_confidence(self.answer_confidence.get(question, 0.0))


@dataclass(frozen=True)
class GateOutcome:
    """What the caller should do with a grade, and why (audit-logged as text)."""

    decision: GateDecision
    reason: str
    # The label that drove the decision, or "" when confidence overruled it.
    merge_action: MergeAction | str = ""
    confidence: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "merge_action": self.merge_action,
            "confidence": round(self.confidence, 3),
        }


def gate_code_review(
    grade: CodeReviewGrade, *, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
) -> GateOutcome:
    """Turn a code-review grade into a merge gate decision.

    Order matters, and it is deliberate: confidence is checked *before* the
    label, so a confident reject blocks, an unconfident accept goes to a
    human, and only a confident accept passes.

    - confidence < threshold -> `human_review` (any label, including accept)
    - merge_action == "accept" -> `accept`
    - "request_changes" / "needs_tests" -> `rework`, with the action named
    - "reject" -> `block`
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"threshold must be 0-1, got {threshold}")
    confidence = grade.confidence_for("merge_action")
    if confidence < threshold:
        return GateOutcome(
            decision="human_review",
            reason=(
                f"coding decision agent is not confident enough to merge "
                f"(merge_action={grade.merge_action} at {confidence:.2f} "
                f"< {threshold:.2f}); a human must decide"
            ),
            merge_action=grade.merge_action,
            confidence=confidence,
        )
    if grade.merge_action == "accept":
        return GateOutcome(
            decision="accept",
            reason=(
                f"coding decision agent accepts (code_quality "
                f"{grade.code_quality}/{CODE_QUALITY_MAX}, confidence {confidence:.2f})"
            ),
            merge_action="accept",
            confidence=confidence,
        )
    if grade.merge_action in ("request_changes", "needs_tests"):
        return GateOutcome(
            decision="rework",
            reason=(
                f"coding decision agent asks for {grade.merge_action} "
                f"(code_quality {grade.code_quality}/{CODE_QUALITY_MAX}, "
                f"confidence {confidence:.2f})"
            ),
            merge_action=grade.merge_action,
            confidence=confidence,
        )
    return GateOutcome(
        decision="block",
        reason=(
            f"coding decision agent rejects (code_quality "
            f"{grade.code_quality}/{CODE_QUALITY_MAX}, confidence {confidence:.2f})"
        ),
        merge_action="reject",
        confidence=confidence,
    )


def gate_trace(
    grade: AgentTraceGrade, *, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
) -> GateOutcome:
    """Turn an agent-trace grade into a loop decision.

    The trace's own `human_review`/`stop` actions are never downgraded by this
    function, and a low-confidence `continue` is not trusted: it becomes
    `human_review`. `observe` maps to `rework` in gate terms (keep going, but
    watch the next step); callers that only need the raw action should read
    `grade.action` directly.
    """
    confidence = grade.confidence_for("action")
    if grade.action in ("human_review", "stop"):
        decision: GateDecision = "human_review" if grade.action == "human_review" else "block"
        return GateOutcome(
            decision=decision,
            reason=f"coding decision agent trace action: {grade.action} "
            f"(outcome={grade.outcome}, risk={grade.risk}, urgency={grade.urgency})",
            merge_action=grade.action,
            confidence=confidence,
        )
    if confidence < threshold:
        return GateOutcome(
            decision="human_review",
            reason=(
                f"coding decision agent trace action {grade.action} is not confident "
                f"enough ({confidence:.2f} < {threshold:.2f}); a human must decide"
            ),
            merge_action=grade.action,
            confidence=confidence,
        )
    if grade.action == "continue":
        return GateOutcome(
            decision="accept",
            reason=f"coding decision agent trace action: continue (outcome={grade.outcome})",
            merge_action="continue",
            confidence=confidence,
        )
    return GateOutcome(
        decision="rework",
        reason=f"coding decision agent trace action: observe (outcome={grade.outcome})",
        merge_action="observe",
        confidence=confidence,
    )


def routing_hint(grade: RoutingGrade) -> dict[str, str]:
    """A routing grade as a plain hint for Jev's D4 decision.

    This is a hint, not an assignment: Jev still picks worker/model/effort at
    D4, and the hint is only added to the D4 request state (see tasks.py).
    The tier-to-effort mapping is a starting heuristic, not a model claim:
    `reasoning` work gets the highest effort, `small_fast` the lowest.
    """
    effort = {
        "small_fast": "low",
        "mid": "medium",
        "frontier": "high",
        "reasoning": "xhigh",
    }[grade.model_tier]
    return {
        "model_tier": grade.model_tier,
        "skill": grade.skill,
        "task_difficulty": str(grade.task_difficulty),
        "suggested_effort": effort,
        "confidence": f"{grade.confidence_for('model_tier'):.2f}",
    }


def compact_diff(diff: str, *, max_chars: int = MAX_CONTEXT_CHARS) -> str:
    """Compact a diff to fit the model's 2048-token context window.

    Short diffs pass through untouched. A long diff keeps its head (where the
    change starts) and its tail (where it ends), with an explicit truncation
    marker in the middle, so a grader never silently scores a partial diff as
    if it were whole. The marker names how many lines were dropped.
    """
    if len(diff) <= max_chars:
        return diff
    lines = diff.splitlines()
    if len(lines) <= 2:
        return diff[:max_chars]
    # Split the budget roughly 60/40 head/tail; the marker itself costs a line.
    head_count = max(1, int(len(lines) * 0.6))
    tail_count = max(1, len(lines) - head_count - 1)
    # Rebalance by characters, not just lines: very long lines still fit.
    head = "\n".join(lines[:head_count])
    tail = "\n".join(lines[len(lines) - tail_count :])
    while len(head) + len(tail) > max_chars and (head_count > 1 or tail_count > 1):
        if len(head) >= len(tail) and head_count > 1:
            head_count -= 1
            head = "\n".join(lines[:head_count])
        elif tail_count > 1:
            tail_count -= 1
            tail = "\n".join(lines[len(lines) - tail_count :])
        else:
            break
    dropped = len(lines) - head_count - tail_count
    marker = f"\n... [{dropped} line(s) omitted to fit the coding decision model's context] ...\n"
    result = head + marker + tail
    return result[:max_chars] if len(result) > max_chars else result
