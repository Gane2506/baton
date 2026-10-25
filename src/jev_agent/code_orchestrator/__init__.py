"""The Coding Decision Agent as Baton's code orchestrator (issue #187).

Jev stays the main orchestrator (pipeline + D1-D8 decisions). This package is
the orchestrator for code-writing agents only: it grades diffs, routes coding
tasks, and proposes merge actions, gated on calibrated confidence. See
`grades.py` for the role contract and `docs/code-orchestrator.md`.
"""

from __future__ import annotations

from jev_agent.code_orchestrator.backend import (
    CodeOrchestrator,
    CodeOrchestratorError,
    CodeOrchestratorUnavailable,
    GraderBackend,
    load_backend,
    normalise_code_review,
    normalise_routing,
    normalise_tool_call,
    normalise_trace,
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
from jev_agent.code_orchestrator.laya_backend import (
    AGENT_TRACE_QUESTIONS,
    CODE_REVIEW_QUESTIONS,
    ROUTING_QUESTIONS,
    TOOL_CALL_QUESTIONS,
    LayaBackend,
    build_code_review_state,
    build_routing_state,
    detect_language,
    diff_stats,
    format_training_diff,
)

__all__ = [
    "AGENT_TRACE_QUESTIONS",
    "CODE_REVIEW_QUESTIONS",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_MODEL_ID",
    "MAX_CONTEXT_CHARS",
    "ROUTING_QUESTIONS",
    "TOOL_CALL_QUESTIONS",
    "AgentTraceGrade",
    "CodeOrchestrator",
    "CodeOrchestratorError",
    "CodeOrchestratorUnavailable",
    "CodeReviewGrade",
    "GateOutcome",
    "GraderBackend",
    "LayaBackend",
    "RoutingGrade",
    "ToolCallGrade",
    "build_code_review_state",
    "build_routing_state",
    "compact_diff",
    "detect_language",
    "diff_stats",
    "format_training_diff",
    "gate_code_review",
    "gate_trace",
    "load_backend",
    "normalise_code_review",
    "normalise_routing",
    "normalise_tool_call",
    "normalise_trace",
    "routing_hint",
]
