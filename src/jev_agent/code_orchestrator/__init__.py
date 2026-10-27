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
from jev_agent.code_orchestrator.briefs import BRIEF_MAX_CHARS, compose_rework_brief
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
from jev_agent.code_orchestrator.ledger import (
    LEDGER_FILENAME,
    Aggregate,
    GradeRecord,
    LedgerRatings,
    RatingLedger,
    code_review_record,
    enrich_hint,
    ledger_path,
    ledger_path_for_repo,
    routing_record,
    trace_record,
    unavailable_record,
)

__all__ = [
    "AGENT_TRACE_QUESTIONS",
    "BRIEF_MAX_CHARS",
    "CODE_REVIEW_QUESTIONS",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_MODEL_ID",
    "LEDGER_FILENAME",
    "MAX_CONTEXT_CHARS",
    "ROUTING_QUESTIONS",
    "TOOL_CALL_QUESTIONS",
    "AgentTraceGrade",
    "Aggregate",
    "CodeOrchestrator",
    "CodeOrchestratorError",
    "CodeOrchestratorUnavailable",
    "CodeReviewGrade",
    "GateOutcome",
    "GradeRecord",
    "GraderBackend",
    "LayaBackend",
    "LedgerRatings",
    "RatingLedger",
    "RoutingGrade",
    "ToolCallGrade",
    "build_code_review_state",
    "build_routing_state",
    "code_review_record",
    "compact_diff",
    "compose_rework_brief",
    "detect_language",
    "diff_stats",
    "enrich_hint",
    "format_training_diff",
    "gate_code_review",
    "gate_trace",
    "ledger_path",
    "ledger_path_for_repo",
    "load_backend",
    "normalise_code_review",
    "normalise_routing",
    "normalise_tool_call",
    "normalise_trace",
    "routing_hint",
    "routing_record",
    "trace_record",
    "unavailable_record",
]
