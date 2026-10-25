"""The live `laya` backend for the Coding Decision Agent (issue #259).

No model is loaded here: the backend is exercised through a fake `laya`
agent whose `system_one` returns canned laya-shaped answers. The question
wording is pinned against the fixture extracted mechanically from the public
training dataset `S4MPL3BI4S/coding-decision-cases` -- if the wording in
`laya_backend.py` drifts from the dataset, the pin test fails and the
constants must be re-extracted, not edited by hand.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from jev_agent.code_orchestrator import (
    CodeOrchestrator,
    CodeOrchestratorError,
    CodeOrchestratorUnavailable,
    gate_code_review,
    load_backend,
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
)

_FIXTURE = Path(__file__).parent / "fixtures" / "coding_decision_questions.json"


def _choice(label: str, confidence: float = 0.9) -> dict[str, object]:
    return {
        "type": "choice",
        "choice": label,
        "probabilities": {label: confidence},
        "confidence": confidence,
        "answer_confidence": confidence,
        "action": {"act_probability": 0.5},
    }


def _score(probabilities: dict[str, float], confidence: float = 0.85) -> dict[str, object]:
    return {
        "type": "score",
        "score": 2.0,
        "legend": {k: f"level {k}" for k in probabilities},
        "probabilities": probabilities,
        "confidence": confidence,
        "answer_confidence": confidence,
        "action": {"act_probability": 0.5},
    }


def _review_answers(**overrides: object) -> dict[str, object]:
    answers: dict[str, object] = {
        "code_quality": _score({"0": 0.05, "1": 0.10, "2": 0.15, "3": 0.60, "4": 0.10}),
        "instruction_followed": _choice("yes", 0.90),
        "likely_correct": _choice("yes", 0.88),
        "merge_action": _choice("accept", 0.92),
    }
    answers.update(overrides)
    return answers


class _FakeLayaAgent:
    """A `laya.Agent` stand-in: records calls, returns canned answers."""

    def __init__(
        self, answers: dict[str, object] | None = None, *, truncated: bool = False
    ) -> None:
        self.answers = answers if answers is not None else _review_answers()
        self.truncated = truncated
        self.seen_states: list[object] = []
        self.seen_questions: list[object] = []

    def system_one(self, state: object, questions: object) -> dict[str, object]:
        self.seen_states.append(state)
        self.seen_questions.append(questions)
        return {
            "model": "laya-rl-agent",
            "answers": self.answers,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 0,
                "truncated": self.truncated,
            },
        }


def test_question_wording_matches_dataset_fixture() -> None:
    """The shipped wording is the dataset's wording, verbatim.

    The fixture was extracted mechanically from
    `S4MPL3BI4S/coding-decision-cases` (see `build_fixture.py` in the
    program notes). A drift here means the model is being asked something
    it was not trained on, and its calibrated confidence is meaningless.
    """
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["code_review"] == CODE_REVIEW_QUESTIONS
    assert fixture["routing"] == ROUTING_QUESTIONS
    assert fixture["tool_call"] == TOOL_CALL_QUESTIONS
    assert fixture["agent_trace"] == AGENT_TRACE_QUESTIONS


def test_package_import_does_not_import_torch_or_laya() -> None:
    """`laya` + torch stay optional: importing the package must not load them."""
    repo_root = Path(__file__).parents[2]
    code = (
        "import sys; import jev_agent.code_orchestrator; "
        "print('torch' in sys.modules, 'laya' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert result.stdout.strip() == "False False"


def test_detect_language() -> None:
    py_diff = "diff --git a/x.py b/x.py\n+++ b/x.py\n+print(1)\n"
    assert detect_language(py_diff) == "python"
    mixed = (
        "diff --git a/a.py b/a.py\n+++ b/a.py\n"
        "diff --git a/b.ts b/b.ts\n+++ b/b.ts\n"
        "diff --git a/c.ts b/c.ts\n+++ b/c.ts\n"
    )
    assert detect_language(mixed) == "typescript"
    assert detect_language("diff --git a/x.xyz b/x.xyz\n+++ b/x.xyz\n") is None
    assert detect_language("no files here\n") is None
    assert detect_language("") is None


def test_diff_stats() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n+more\n"
        "diff --git a/b.py b/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-gone\n"
    )
    assert diff_stats(diff) == {"files": 2, "added": 2, "removed": 2}
    assert diff_stats("") == {"files": 0, "added": 0, "removed": 0}


def test_build_code_review_state_shape() -> None:
    diff = "diff --git a/a.py b/a.py\n+++ b/a.py\n+print(1)\n"
    state = build_code_review_state(
        task="fix the bug", diff=diff, tests="pytest: 3 passed", language="python"
    )
    assert list(state) == ["family", "task", "language", "diff_stats", "diff", "notes"]
    assert state["family"] == "code_review"
    assert state["task"] == "fix the bug"
    assert state["language"] == "python"
    assert state["diff_stats"] == {"files": 1, "added": 1, "removed": 0}
    assert state["diff"] == diff
    # No training row carries a separate `tests` key: test output goes in
    # `notes`, the row's free-text field.
    assert state["notes"] == "pytest: 3 passed"

    bare = build_code_review_state(task="t", diff=diff)
    assert "language" not in bare
    assert "notes" not in bare


def test_build_routing_state_shape() -> None:
    assert build_routing_state("refactor billing") == {
        "family": "routing",
        "request": "refactor billing",
    }


def test_live_backend_grades_through_the_orchestrator() -> None:
    """A fake `laya` agent drives the real adapter + normaliser + gate."""
    agent = _FakeLayaAgent()
    orch = CodeOrchestrator(backend=LayaBackend(agent=agent))
    diff = "diff --git a/a.py b/a.py\n+++ b/a.py\n+print(1)\n"
    grade = orch.grade_code(task="fix the bug", diff=diff)
    assert grade.code_quality == 3  # argmax of the score probabilities
    assert grade.instruction_followed is True
    assert grade.likely_correct is True
    assert grade.merge_action == "accept"
    assert grade.answer_confidence["merge_action"] == 0.92
    assert gate_code_review(grade).decision == "accept"
    # The state sent to the model is the training-row shape.
    sent = agent.seen_states[0]
    assert isinstance(sent, dict)
    assert sent["family"] == "code_review"
    assert sent["task"] == "fix the bug"
    assert sent["language"] == "python"
    assert sent["diff_stats"] == {"files": 1, "added": 1, "removed": 0}
    assert agent.seen_questions[0] is CODE_REVIEW_QUESTIONS


def test_score_answer_is_argmax_with_lowest_on_ties() -> None:
    backend = LayaBackend(
        agent=_FakeLayaAgent(
            _review_answers(code_quality=_score({"0": 0.1, "1": 0.1, "2": 0.4, "3": 0.4, "4": 0.0}))
        )
    )
    grade = CodeOrchestrator(backend=backend).grade_code(task="t", diff="x")
    assert grade.code_quality == 2  # tie between 2 and 3 resolves lowest


def test_missing_answer_is_a_value_error_not_a_default() -> None:
    # The adapter raises ValueError for a mis-shaped verdict; the
    # orchestrator wraps backend failures as CodeOrchestratorError (the
    # gate blocks fail-closed either way -- the point is no silent default).
    answers = _review_answers()
    del answers["merge_action"]
    backend = LayaBackend(agent=_FakeLayaAgent(answers))
    with pytest.raises(CodeOrchestratorError, match="no answer for 'merge_action'"):
        CodeOrchestrator(backend=backend).grade_code(task="t", diff="x")


def test_missing_probabilities_is_a_value_error() -> None:
    bad = _score({"0": 0.5})
    del bad["probabilities"]
    backend = LayaBackend(agent=_FakeLayaAgent(_review_answers(code_quality=bad)))
    with pytest.raises(CodeOrchestratorError, match="no probabilities"):
        CodeOrchestrator(backend=backend).grade_code(task="t", diff="x")


def test_truncated_state_warns() -> None:
    backend = LayaBackend(agent=_FakeLayaAgent(truncated=True))
    with pytest.warns(UserWarning, match="truncated to the model's 2048-token context"):
        CodeOrchestrator(backend=backend).grade_code(task="t", diff="x")


def test_live_backend_routes() -> None:
    agent = _FakeLayaAgent(
        answers={
            "model_tier": _choice("mid", 0.8),
            "task_difficulty": _score({"0": 0.1, "1": 0.6, "2": 0.2, "3": 0.1}),
            "skill": _choice("refactor", 0.75),
        }
    )
    route = CodeOrchestrator(backend=LayaBackend(agent=agent)).route("refactor billing")
    assert route.model_tier == "mid"
    assert route.task_difficulty == 1
    assert route.skill == "refactor"
    assert agent.seen_states[0] == {"family": "routing", "request": "refactor billing"}


def test_live_backend_grades_trace_and_tool_call() -> None:
    agent = _FakeLayaAgent(
        answers={
            "action": _choice("observe", 0.8),
            "needs_review": _choice("no", 0.85),
            "outcome": _choice("partial", 0.7),
            "risk": _score({"0": 0.7, "1": 0.2, "2": 0.05, "3": 0.05}),
            "urgency": _score({"0": 0.6, "1": 0.3, "2": 0.1, "3": 0.0}),
        }
    )
    backend = LayaBackend(agent=agent)
    trace = CodeOrchestrator(backend=backend).grade_trace("some trace text")
    assert trace.action == "observe"
    assert trace.needs_review is False
    assert trace.outcome == "partial"
    assert trace.risk == 0
    assert trace.urgency == 0

    agent.answers = {
        "should_call_tool": _choice("ask_followup", 0.9),
        "call_verdict": _choice("abstain", 0.88),
    }
    call = CodeOrchestrator(backend=backend).grade_tool_call("some tool call")
    assert call.should_call_tool == "ask_followup"
    assert call.call_verdict == "abstain"


def test_load_backend_prefers_laya_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """`find_spec("laya")` hit -> the built-in backend, without importing it."""
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: object() if name == "laya" else None
    )
    backend = load_backend("S4MPL3BI4S/Coding_Decision_Agent")
    assert isinstance(backend, LayaBackend)
    assert backend.model_id == "S4MPL3BI4S/Coding_Decision_Agent"


@pytest.mark.skipif(importlib.util.find_spec("laya") is not None, reason="laya is installed")
def test_missing_laya_is_unavailable_not_an_import_error() -> None:
    backend = LayaBackend()
    with pytest.raises(CodeOrchestratorUnavailable, match="not installed"):
        backend.grade_code(task="t", diff="x")
