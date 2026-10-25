"""Live smoke test for the Coding Decision Agent backend. Never runs in CI.

Run with: JEV_LIVE_TESTS=1 uv run pytest tests/live/test_code_orchestrator_live.py

Requires `laya` installed (`pip install laya`; CPU-only torch works -- see
docs/code-orchestrator.md). The first run downloads the ~842 MB checkpoint.
This is the live-model validation issue #203 (I11) noted as missing: until
this test runs against a real backend, the gates are proven only by unit
tests against fake backends. It grades one tiny diff and routes one tiny
task, and asserts only the documented contract: a closed merge-action
label, a 0-4 quality score, and a routing tier/skill from the documented
sets. It does not assert a particular grade -- a live model's verdict on a
sample diff is not a stable assertion.
"""

import math
import os

import pytest

from jev_agent.code_orchestrator import (
    CodeOrchestrator,
    CodeOrchestratorUnavailable,
    gate_code_review,
    gate_trace,
    routing_hint,
)

pytest.importorskip("laya")

_ENABLED = os.environ.get("JEV_LIVE_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not _ENABLED,
    reason="live test: set JEV_LIVE_TESTS=1 with laya installed",
)


def test_live_grade_and_route() -> None:
    orch = CodeOrchestrator()
    if not orch.available:
        raise CodeOrchestratorUnavailable("live backend did not load")
    grade = orch.grade_code(
        task="Add a --version flag",
        diff="diff --git a/cli.py b/cli.py\n+print(__version__)\n",
    )
    assert grade.merge_action in ("accept", "request_changes", "needs_tests", "reject")
    assert 0 <= grade.code_quality <= 4
    # Issue #192: a confidence must be finite to gate on -- a NaN would
    # pass the gate fail-open. The live backend must never emit one.
    assert all(math.isfinite(v) for v in grade.answer_confidence.values())
    # The gate must produce a decision, never crash, on a live verdict.
    assert gate_code_review(grade).decision in ("accept", "rework", "human_review", "block")
    route = orch.route("Add a --version flag to the CLI")
    assert route.model_tier in ("small_fast", "mid", "frontier", "reasoning")
    assert route.skill in (
        "code_edit",
        "debug",
        "write_tests",
        "refactor",
        "explain",
        "shell_ops",
        "research",
        "plan",
    )
    # A live routing hint may be empty (gated below 0.7); it must never be
    # a low-confidence hint presented as trusted.
    hint = routing_hint(route)
    assert hint == {} or float(hint["confidence"]) >= 0.7


def test_live_trace_grade_gates() -> None:
    orch = CodeOrchestrator()
    if not orch.available:
        raise CodeOrchestratorUnavailable("live backend did not load")
    try:
        grade = orch.grade_trace("agent edited cli.py and its tests pass")
    except CodeOrchestratorUnavailable:
        pytest.skip("installed backend does not expose grade_trace")
    assert grade.action in ("continue", "observe", "human_review", "stop")
    assert gate_trace(grade).decision in ("accept", "rework", "human_review", "block")
