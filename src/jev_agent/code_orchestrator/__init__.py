"""The Coding Decision Agent as Baton's code orchestrator (issue #187).

Jev stays the main orchestrator (pipeline + D1-D8 decisions). This package is
the orchestrator for code-writing agents only: it grades diffs, routes coding
tasks, and proposes merge actions, gated on calibrated confidence. See
`grades.py` for the role contract and `docs/code-orchestrator.md`.
"""

from __future__ import annotations

from jev_agent.code_orchestrator.backend import (
    CodeOrchestrator,
    CodeOrchestratorUnavailable,
    GraderBackend,
    load_backend,
    normalise_code_review,
    normalise_routing,
)
from jev_agent.code_orchestrator.grades import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MODEL_ID,
    MAX_CONTEXT_CHARS,
    AgentTraceGrade,
    CodeReviewGrade,
    GateOutcome,
    RoutingGrade,
    ToolCallGrade,
    compact_diff,
    gate_code_review,
    gate_trace,
    routing_hint,
)

__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_MODEL_ID",
    "MAX_CONTEXT_CHARS",
    "AgentTraceGrade",
    "CodeOrchestrator",
    "CodeOrchestratorUnavailable",
    "CodeReviewGrade",
    "GateOutcome",
    "GraderBackend",
    "RoutingGrade",
    "ToolCallGrade",
    "compact_diff",
    "gate_code_review",
    "gate_trace",
    "load_backend",
    "normalise_code_review",
    "normalise_routing",
    "routing_hint",
]
