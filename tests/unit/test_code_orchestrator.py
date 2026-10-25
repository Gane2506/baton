"""The Coding Decision Agent as code orchestrator (issue #187).

Everything here is pure or fake-backed: the real model is never loaded in
tests (it is a 421M checkpoint behind `laya` + torch, and it is not a test
dependency). The gates are what matter, and they are tested exactly:
confidence first, label second, missing confidence never trusted.
"""

from __future__ import annotations

import importlib.util
import warnings

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
from jev_agent.core.models import TaskCard
from jev_agent.quality.gate import _code_grade_task_text, _test_summary, apply_code_grade
from jev_agent.quality.models import CheckResult, GateResult


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


class _UnavailableBackend:
    """A backend that is missing: every call raises Unavailable."""

    def grade_code(self, *, task: str, diff: str, tests: str) -> object:
        raise CodeOrchestratorUnavailable(
            "the Coding Decision Agent is not installed: install `laya`"
        )

    def route_model(self, request: str) -> object:
        raise CodeOrchestratorUnavailable(
            "the Coding Decision Agent is not installed: install `laya`"
        )


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


def test_nonfinite_confidence_never_passes_the_gate() -> None:
    # Issue #192: NaN < 0.7 is False, so a NaN-confidence "accept" used to
    # pass the gate fail-open. NaN, +inf and -inf are distrusted as 0.0.
    for bad in (float("nan"), float("inf"), float("-inf")):
        outcome = gate_code_review(_grade(answer_confidence={"merge_action": bad}))
        assert outcome.decision == "human_review", bad
        assert outcome.confidence == 0.0, bad


def test_nonfinite_trace_confidence_is_not_trusted_to_continue() -> None:
    grade = AgentTraceGrade(
        action="continue",
        needs_review=False,
        outcome="success",
        risk=0,
        urgency=0,
        answer_confidence={"action": float("nan")},
    )
    outcome = gate_trace(grade)
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


def test_backend_missing_is_a_clear_error_not_a_traceback_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate the package being absent: the loader must say so plainly
    # and name the install, not raise ImportError. (laya IS installed in
    # the --all-extras test env, so the absence is stubbed, not assumed.)
    orig_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None if name == "laya" else orig_find_spec(name),
    )
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
    # Existing configs referencing Laya must migrate cleanly (issue #193): they
    # still validate, warn, and are treated as `primary = "jev"` — the
    # `[dispatcher.laya]` table itself no longer exists on the config.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = JevConfig.model_validate(
            {"dispatcher": {"primary": "consensus", "laya": {"min_margin": 0.3}}}
        )
    assert cfg.dispatcher.primary == "jev"
    assert not hasattr(cfg.dispatcher, "laya")
    assert len(caught) == 2 and all(
        "consensus dispatcher was removed" in str(w.message) for w in caught
    )


def test_code_grade_task_text_uses_title_plus_description() -> None:
    """The gate grades against title + description, not a bare title.

    Measured live (issue #259): a bare title makes instruction_followed
    say "no" and merge_action say "request_changes" on a known-good diff.
    """
    task = TaskCard(id="T1", title="Fix x", description="The widget must not crash on empty input.")
    assert _code_grade_task_text(task) == "Fix x\n\nThe widget must not crash on empty input."
    assert _code_grade_task_text(TaskCard(id="T1", title="Fix x", description="")) == "Fix x"
    assert _code_grade_task_text(None) == "integration"
    long = TaskCard(id="T1", title="Fix x", description="d" * 3000)
    text = _code_grade_task_text(long)
    assert text.startswith("Fix x\n\n" + "d" * 1993) and text.endswith("[task truncated]")


def test_apply_code_grade_blocks_on_reject_and_records_grade() -> None:
    result = apply_code_grade(
        _result(),
        settings=CodeOrchestratorSettings(enabled=True),
        task_text="Fix x",
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
        task_text="Fix x",
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
        task_text="Fix x",
        diff="+x\n",
        orchestrator=CodeOrchestrator(backend=_FakeBackend("accept", 0.4)),
    )
    assert result.blocked is True
    assert result.code_grade is not None
    assert result.code_grade["gate"]["decision"] == "human_review"


def test_apply_code_grade_inference_error_blocks_fail_closed_not_unavailable() -> None:
    """C3, gate side: an inference failure is not a missing model.

    The old code let a backend exception propagate as whatever the backend
    raised; the gate's generic handler blocked, but only by accident. Now
    the orchestrator wraps it as CodeOrchestratorError, and the gate blocks
    with status "error" -- never the "unavailable" path, which must stay
    reserved for a backend that was never installed.
    """

    class _ExplodingBackend(_FakeBackend):
        def grade_code(self, *, task: str, diff: str, tests: str) -> object:
            raise RuntimeError("torch OOM on the grading pass")

    result = apply_code_grade(
        _result(),
        settings=CodeOrchestratorSettings(enabled=True),
        task_text="Fix x",
        diff="+x\n",
        orchestrator=CodeOrchestrator(backend=_ExplodingBackend()),
    )
    assert result.blocked is True
    assert result.code_grade is not None
    assert result.code_grade["status"] == "error"
    assert "CodeOrchestratorError" in result.code_grade["detail"]


# ---------------------------------------------------------------------------
# Issue #203 fixes
# ---------------------------------------------------------------------------


def test_bool_label_rejects_unknown_strings_instead_of_coercing_to_false() -> None:
    """I4: 'maybe' used to become a confident False; now it raises."""
    base: dict[str, object] = {
        "code_quality": 3,
        "merge_action": "accept",
        "answer_confidence": {"merge_action": 0.9},
    }
    with pytest.raises(ValueError, match="not a yes/no label"):
        normalise_code_review({**base, "instruction_followed": "maybe", "likely_correct": "yes"})
    with pytest.raises(ValueError, match="not a yes/no label"):
        normalise_code_review({**base, "instruction_followed": "", "likely_correct": "yes"})
    for spelling, expected in (
        ("yes", True),
        ("YES", True),
        ("true", True),
        ("1", True),
        ("no", False),
        ("No", False),
        ("false", False),
        ("0", False),
    ):
        grade = normalise_code_review(
            {**base, "instruction_followed": spelling, "likely_correct": spelling}
        )
        assert grade.instruction_followed is expected
        assert grade.likely_correct is expected


def test_gate_trace_validates_threshold_action_and_scores() -> None:
    """I5: gate_trace is now as strict as gate_code_review."""
    good = AgentTraceGrade(
        action="observe",
        needs_review=False,
        outcome="progress",
        risk=1,
        urgency=1,
        answer_confidence={"action": 0.9},
    )
    with pytest.raises(ValueError, match="threshold must be 0-1"):
        gate_trace(good, threshold=1.5)
    with pytest.raises(ValueError, match="unknown trace action"):
        gate_trace(
            AgentTraceGrade(
                action="wander",  # type: ignore[arg-type]
                needs_review=False,
                outcome="progress",
                risk=1,
                urgency=1,
                answer_confidence={"action": 0.9},
            )
        )
    for field, value in (("risk", 4), ("risk", -1), ("urgency", 9), ("risk", True)):
        kwargs: dict[str, object] = {
            "action": "observe",
            "needs_review": False,
            "outcome": "progress",
            "risk": 1,
            "urgency": 1,
            "answer_confidence": {"action": 0.9},
        }
        kwargs[field] = value
        with pytest.raises(ValueError, match="out of 0-3"):
            gate_trace(AgentTraceGrade(**kwargs))  # type: ignore[arg-type]


def test_routing_hint_is_gated_and_empty_below_threshold() -> None:
    """I6: a low-confidence routing hint is no hint, not a weak tier."""
    low = RoutingGrade(
        model_tier="frontier",
        task_difficulty=2,
        skill="refactor",
        answer_confidence={"model_tier": 0.4},
    )
    assert routing_hint(low) == {}
    assert routing_hint(low, threshold=0.3)["model_tier"] == "frontier"
    assert routing_hint(low, threshold=0.4)["model_tier"] == "frontier"
    with pytest.raises(ValueError, match="threshold must be 0-1"):
        routing_hint(low, threshold=2.0)
    missing = RoutingGrade(
        model_tier="frontier",
        task_difficulty=2,
        skill="refactor",
        answer_confidence={},
    )
    assert routing_hint(missing) == {}


def test_compact_diff_two_line_long_diff_gets_a_marker() -> None:
    """C2: the exact case the swarm review verified -- 2 lines, 16k chars."""
    import re

    diff = "a" * 8000 + "\n" + "b" * 8000
    compacted = compact_diff(diff)
    assert len(compacted) <= 7000
    match = re.search(r"\[(\d+) character\(s\) omitted to fit", compacted)
    assert match is not None, "no omission marker on a truncated 2-line diff"
    # The marker's count is computed from the head/tail actually returned.
    # The full marker line, reconstructed from the match's span context:
    # everything between the head and the tail is the marker.
    marker_start = compacted.index("... [")
    marker_end = compacted.index("] ...", marker_start) + len("] ...")
    kept = len(compacted) - (marker_end - marker_start) - 2  # surrounding newlines
    assert int(match.group(1)) == len(diff) - kept
    assert compacted.startswith("a" * 100)
    assert compacted.endswith("b" * 100)


def test_compact_diff_long_lines_never_overflow_or_lose_the_marker() -> None:
    """C2: head+tail rebalance used to overflow max_chars and slice the marker."""
    import re

    diff = "\n".join("x" * 2000 for _ in range(10))
    compacted = compact_diff(diff, max_chars=7000)
    assert len(compacted) <= 7000
    match = re.search(r"\[(\d+) line\(s\) omitted to fit", compacted)
    assert match is not None
    kept_lines = len([line for line in compacted.splitlines() if "omitted to fit" not in line])
    assert int(match.group(1)) == 10 - kept_lines


def test_compact_diff_tiny_budget_still_names_the_omission() -> None:
    compacted = compact_diff("a" * 500, max_chars=30)
    assert len(compacted) <= 30
    assert "omitted" in compacted
    with pytest.raises(ValueError, match="max_chars must be positive"):
        compact_diff("abc", max_chars=0)


def test_backend_inference_errors_are_wrapped_not_leaked() -> None:
    """C3: a backend exception becomes CodeOrchestratorError, never raw."""
    from jev_agent.code_orchestrator import CodeOrchestratorError

    class _ExplodingBackend(_FakeBackend):
        def grade_code(self, *, task: str, diff: str, tests: str) -> object:
            raise RuntimeError("boom")

        def route_model(self, request: str) -> object:
            raise TimeoutError("model call timed out")

    orch = CodeOrchestrator(backend=_ExplodingBackend())
    with pytest.raises(CodeOrchestratorError, match="backend failed"):
        orch.grade_code(task="fix x", diff="+x\n")
    with pytest.raises(CodeOrchestratorError, match="backend failed"):
        orch.route("refactor billing")
    # And the missing-model class is a subclass, so D4's single except
    # (CodeOrchestratorError) covers both the missing and the failed case.
    assert issubclass(CodeOrchestratorUnavailable, CodeOrchestratorError)


def test_malformed_verdict_stays_a_value_error_not_an_inference_error() -> None:
    """C3: wrapping must not hide a mis-shaped verdict as an inference failure."""

    class _MisshapedBackend(_FakeBackend):
        def grade_code(self, *, task: str, diff: str, tests: str) -> object:
            return {
                "code_quality": 3,
                "instruction_followed": "yes",
                "likely_correct": "yes",
                "merge_action": "ship_it",
                "answer_confidence": {},
            }

    orch = CodeOrchestrator(backend=_MisshapedBackend())
    # pytest.raises(ValueError) already fails if this came back wrapped as
    # CodeOrchestratorError, since the two classes are unrelated.
    with pytest.raises(ValueError, match="unknown merge_action"):
        orch.grade_code(task="fix x", diff="+x\n")


def test_grade_code_compacts_internally_so_callers_cannot_forget() -> None:
    """I10: the caller hands over the raw diff; the orchestrator compacts."""

    class _CapturingBackend(_FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.seen_diff = ""

        def grade_code(self, *, task: str, diff: str, tests: str) -> object:
            self.seen_diff = diff
            return super().grade_code(task=task, diff=diff, tests=tests)

    backend = _CapturingBackend()
    orch = CodeOrchestrator(backend=backend)
    raw = "\n".join(f"+line {i}" for i in range(1000))
    assert len(raw) > 7000
    orch.grade_code(task="fix x", diff=raw)
    assert len(backend.seen_diff) <= 7000
    assert "omitted to fit" in backend.seen_diff
    # A caller-configured budget is honoured, not just the default.
    backend2 = _CapturingBackend()
    orch2 = CodeOrchestrator(backend=backend2, max_diff_chars=500)
    orch2.grade_code(task="fix x", diff=raw)
    assert len(backend2.seen_diff) <= 500
    with pytest.raises(ValueError, match="max_diff_chars must be positive"):
        CodeOrchestrator(backend=_FakeBackend(), max_diff_chars=0)


def test_trace_and_tool_call_wired_through_backend_when_exposed() -> None:
    """I11: the trace/tool families now have a call path, not just exports."""
    from jev_agent.code_orchestrator import normalise_tool_call, normalise_trace

    class _FullBackend(_FakeBackend):
        def grade_trace(self, trace: str) -> object:
            assert trace == "agent did step 1"
            return {
                "action": "observe",
                "needs_review": "no",
                "outcome": "progress",
                "risk": 1,
                "urgency": 1,
                "answer_confidence": {"action": 0.9},
            }

        def grade_tool_call(self, tool_call: str) -> object:
            return {
                "should_call_tool": "yes",
                "call_verdict": "correct",
                "answer_confidence": {},
            }

    orch = CodeOrchestrator(backend=_FullBackend())
    grade = orch.grade_trace("agent did step 1")
    assert grade.action == "observe"
    assert gate_trace(grade).decision == "rework"
    assert orch.grade_tool_call("read file").call_verdict == "correct"
    # A backend without those methods fails clearly instead of guessing.
    plain = CodeOrchestrator(backend=_FakeBackend())
    with pytest.raises(CodeOrchestratorUnavailable, match="does not expose `grade_trace`"):
        plain.grade_trace("agent did step 1")
    with pytest.raises(CodeOrchestratorUnavailable, match="does not expose `grade_tool_call`"):
        plain.grade_tool_call("read file")
    # Normalisers validate the trace/tool families like the code family.
    with pytest.raises(ValueError, match="unknown trace action"):
        normalise_trace(
            {
                "action": "wander",
                "needs_review": "no",
                "outcome": "progress",
                "risk": 1,
                "urgency": 1,
                "answer_confidence": {},
            }
        )
    with pytest.raises(ValueError, match="not a label"):
        normalise_tool_call({"should_call_tool": "", "call_verdict": "correct"})
    assert (
        normalise_tool_call(
            {"should_call_tool": "yes", "call_verdict": "correct", "answer_confidence": {}}
        ).should_call_tool
        == "yes"
    )


def test_apply_code_grade_unavailable_never_blocks() -> None:
    # The backend is missing (simulated: laya IS installed in the
    # --all-extras test env), so the gate records "unavailable" and does
    # not block.
    result = apply_code_grade(
        _result(),
        settings=CodeOrchestratorSettings(enabled=True),
        task_text="Fix x",
        diff="+x\n",
        orchestrator=CodeOrchestrator(backend=_UnavailableBackend()),
    )
    assert result.blocked is False
    assert result.code_grade is not None
    assert result.code_grade["status"] == "unavailable"


# ---------------------------------------------------------------------------
# Issue #254: the gate passes its test summary as the grader's `notes`
# ---------------------------------------------------------------------------


class _RecordingBackend(_FakeBackend):
    """A fake that records the `tests` string the gate passed through."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.seen_tests = "<not called>"

    def grade_code(self, *, task: str, diff: str, tests: str) -> object:
        self.seen_tests = tests
        return super().grade_code(task=task, diff=diff, tests=tests)


def _result_with_checks(*checks: CheckResult) -> GateResult:
    return GateResult(level="basic", scope="task", task_id="T1", number=1, checks=list(checks))


def test_apply_code_grade_passes_test_summary_as_notes() -> None:
    """The tests check's outcome reaches the grader as `tests` (-> `notes`).

    The gate's own tests check is the outcome the model would otherwise
    have to guess at; the summary is `tests: <status> — <detail>`.
    """
    backend = _RecordingBackend("accept", 0.95)
    apply_code_grade(
        _result_with_checks(
            CheckResult(status="pass", detail="`pytest` passed", name="tests"),
            CheckResult(status="pass", detail="clean", name="ruff"),
        ),
        settings=CodeOrchestratorSettings(enabled=True),
        task_text="Fix x",
        diff="+x\n",
        orchestrator=CodeOrchestrator(backend=backend),
    )
    assert backend.seen_tests == "tests: pass — `pytest` passed"


def test_apply_code_grade_passes_failing_test_summary_as_notes() -> None:
    """A failing suite's detail (including the failure tail) is passed too."""
    backend = _RecordingBackend("accept", 0.95)
    apply_code_grade(
        _result_with_checks(
            CheckResult(
                status="fail",
                detail="`pytest` failed with exit code 1: ...FAILED test_x",
                name="tests",
            )
        ),
        settings=CodeOrchestratorSettings(enabled=True),
        task_text="Fix x",
        diff="+x\n",
        orchestrator=CodeOrchestrator(backend=backend),
    )
    assert backend.seen_tests == (
        "tests: fail — `pytest` failed with exit code 1: ...FAILED test_x"
    )


def test_apply_code_grade_passes_no_notes_without_a_tests_check() -> None:
    """No tests check ran (no suite, or a level without one): `notes` stays out."""
    backend = _RecordingBackend("accept", 0.95)
    apply_code_grade(
        _result_with_checks(CheckResult(status="pass", detail="clean", name="ruff")),
        settings=CodeOrchestratorSettings(enabled=True),
        task_text="Fix x",
        diff="+x\n",
        orchestrator=CodeOrchestrator(backend=backend),
    )
    assert backend.seen_tests == ""


def test_test_summary_caps_a_long_failure_tail() -> None:
    """A long failure tail is capped at 2000 chars, not passed whole.

    The notes share the model's state budget with the task and the diff;
    a verbatim multi-kilobyte pytest tail would eat it.
    """
    summary = _test_summary([CheckResult(status="fail", detail="x" * 5000, name="tests")])
    assert len(summary) == 2000
    assert summary.startswith("tests: fail — ")
    assert summary.endswith("...")


def test_test_summary_skips_checks_before_the_tests_check() -> None:
    """Only the check named `tests` is the summary; earlier checks are ignored."""
    assert (
        _test_summary(
            [
                CheckResult(status="fail", detail="lint errors", name="ruff"),
                CheckResult(status="skipped", detail="no test suite detected", name="tests"),
            ]
        )
        == "tests: skipped — no test suite detected"
    )
