"""Backend for the Coding Decision Agent: load it, normalise its verdicts.

Two facts shape this module, and both are stated here so a later reader does
not "fix" them into a bug:

1. `coding_decision_agent` is NOT on PyPI (checked 2026-10-24: the PyPI JSON
   endpoint 404s, and the model card's install block only says
   `pip install laya`). The training repo is unpublished too, so the model
   card's `CodingDecisionAgent` class ships nowhere importable. What Baton
   does instead (issue #259): the fixed question wording was recovered
   verbatim from the public training dataset the model card itself names,
   `S4MPL3BI4S/coding-decision-cases` -- its rows carry the exact
   `questions` dicts the model was trained and benchmarked on -- and
   `laya_backend.LayaBackend` asks those questions through the `laya`
   runtime. The wording is data from the dataset, pinned by tests, not a
   guess: a guessed wording would score against a distribution the model was
   never trained on, and the calibrated confidence would be meaningless. If
   the `coding_decision_agent` package ever becomes installable,
   `load_backend` prefers it; until then the built-in backend is the real
   one.
2. The model is heavy (a 421M ModernBERT derivative, via `laya` + torch) and
   must never become a hard dependency of `jev`: importing this module must
   not import torch, and CI must not download weights. Loading is lazy, and
   when the package is absent every grade call fails with a clear
   `CodeOrchestratorUnavailable` instead of a traceback or a faked grade.
   Callers that can proceed without a grade (the quality gate) treat that as
   "not graded"; callers that cannot (the `jev code` CLI) report it and stop.
   Note `import laya` itself imports torch (via `laya.router`), so even the
   availability check uses `importlib.util.find_spec` rather than importing
   the package: `jev code status` must stay cheap.

A backend is any object with `grade_code(task, diff, tests)` and
`route_model(request)` returning objects with the attributes the model card
documents (`merge_action`, `code_quality`, `instruction_followed`,
`likely_correct`, `answer_confidence`, and for routing `model_tier`,
`task_difficulty`, `skill`, `answer_confidence`). Tests inject a fake; the
loader below is the only place the real package is touched.
"""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass
from typing import Any, Protocol, cast

from jev_agent.code_orchestrator.grades import (
    DEFAULT_MODEL_ID,
    MAX_CONTEXT_CHARS,
    AgentTraceGrade,
    CodeReviewGrade,
    RoutingGrade,
    ToolCallGrade,
    compact_diff,
    validate_trace_grade,
)

__all__ = [
    "CodeOrchestrator",
    "CodeOrchestratorError",
    "CodeOrchestratorUnavailable",
    "GraderBackend",
    "load_backend",
    "normalise_code_review",
    "normalise_routing",
    "normalise_tool_call",
    "normalise_trace",
]


class CodeOrchestratorError(Exception):
    """The Coding Decision Agent backend failed while grading or routing.

    This is the inference-failure class (issue #203, C3): a backend that
    raised, timed out, or returned something unusable is not a missing
    backend, and callers must not treat it as one by accident. Its subclass
    `CodeOrchestratorUnavailable` is the missing-backend case only.
    """


class CodeOrchestratorUnavailable(CodeOrchestratorError):
    """The Coding Decision Agent backend is not installed or cannot load.

    Callers that can proceed without a grade (the quality gate) record this
    as "unavailable" and do not block; callers that cannot (the `jev code`
    CLI) report it and stop. See the error-taxonomy note on
    `CodeOrchestrator.grade_code` for why an inference failure is *not*
    this class, and blocks at the quality gate instead.
    """


class GraderBackend(Protocol):
    """The surface `CodingDecisionAgent` exposes, as the model card documents."""

    def grade_code(self, *, task: str, diff: str, tests: str) -> Any: ...
    def route_model(self, request: str, /) -> Any: ...


def _confidence_map(raw: Any) -> dict[str, float]:
    if isinstance(raw, dict):
        out: dict[str, float] = {}
        for key, value in raw.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[str(key)] = float(value)
        return out
    return {}


_YES_LABELS = ("yes", "true", "1")
_NO_LABELS = ("no", "false", "0")


def _bool_label(value: Any, *, field: str) -> bool:
    """A documented yes/no label into a bool, nothing else (issue #203, I4).

    Only `yes`/`no`, `true`/`false`, `1`/`0` (case-insensitive, whitespace
    trimmed) are accepted. The old version returned `False` for any other
    string, so "maybe" silently became a confident "no" -- exactly the
    silent coercion a mis-shaped verdict must never get. Anything else
    raises `ValueError`, and the quality gate blocks fail-closed on it.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _YES_LABELS:
            return True
        if lowered in _NO_LABELS:
            return False
    raise ValueError(f"coding decision agent: {field} is not a yes/no label ({value!r})")


def _field(raw: Any, name: str) -> Any:
    """One field of a verdict, whether it arrived as a dict or an object."""
    if isinstance(raw, dict):
        return raw.get(name)
    return getattr(raw, name, None)


def normalise_code_review(raw: Any) -> CodeReviewGrade:
    """A model verdict object (or dict) into a `CodeReviewGrade`.

    Raises `ValueError` on a label outside the model's documented sets: a
    mis-shaped verdict must never be silently coerced into a merge decision.
    """
    merge_action = _field(raw, "merge_action")
    if merge_action not in ("accept", "request_changes", "needs_tests", "reject"):
        raise ValueError(f"coding decision agent: unknown merge_action {merge_action!r}")
    quality = _field(raw, "code_quality")
    if not isinstance(quality, int) or isinstance(quality, bool) or not 0 <= quality <= 4:
        raise ValueError(f"coding decision agent: code_quality out of 0-4 ({quality!r})")
    return CodeReviewGrade(
        code_quality=quality,
        instruction_followed=_bool_label(
            _field(raw, "instruction_followed"), field="instruction_followed"
        ),
        likely_correct=_bool_label(_field(raw, "likely_correct"), field="likely_correct"),
        merge_action=merge_action,
        answer_confidence=_confidence_map(_field(raw, "answer_confidence")),
    )


def normalise_routing(raw: Any) -> RoutingGrade:
    """A model routing object (or dict) into a `RoutingGrade`."""
    tier = _field(raw, "model_tier")
    if tier not in ("small_fast", "mid", "frontier", "reasoning"):
        raise ValueError(f"coding decision agent: unknown model_tier {tier!r}")
    skill = _field(raw, "skill")
    if skill not in (
        "code_edit",
        "debug",
        "write_tests",
        "refactor",
        "explain",
        "shell_ops",
        "research",
        "plan",
    ):
        raise ValueError(f"coding decision agent: unknown skill {skill!r}")
    difficulty = _field(raw, "task_difficulty")
    if not isinstance(difficulty, int) or isinstance(difficulty, bool) or not 0 <= difficulty <= 3:
        raise ValueError(f"coding decision agent: task_difficulty out of 0-3 ({difficulty!r})")
    return RoutingGrade(
        model_tier=tier,
        task_difficulty=difficulty,
        skill=skill,
        answer_confidence=_confidence_map(_field(raw, "answer_confidence")),
    )


def normalise_trace(raw: Any) -> AgentTraceGrade:
    """A model trace verdict object (or dict) into an `AgentTraceGrade`.

    Validated exactly like the trace gate validates it: unknown actions,
    out-of-range `risk`/`urgency`, and non-yes/no `needs_review` raise
    `ValueError` instead of being coerced into a loop decision.
    """
    action = _field(raw, "action")
    if action not in ("continue", "observe", "human_review", "stop"):
        raise ValueError(f"coding decision agent: unknown trace action {action!r}")
    risk = _field(raw, "risk")
    if not isinstance(risk, int) or isinstance(risk, bool) or not 0 <= risk <= 3:
        raise ValueError(f"coding decision agent: risk out of 0-3 ({risk!r})")
    urgency = _field(raw, "urgency")
    if not isinstance(urgency, int) or isinstance(urgency, bool) or not 0 <= urgency <= 3:
        raise ValueError(f"coding decision agent: urgency out of 0-3 ({urgency!r})")
    outcome = _field(raw, "outcome")
    if not isinstance(outcome, str):
        raise ValueError(f"coding decision agent: outcome is not a string ({outcome!r})")
    grade = AgentTraceGrade(
        action=action,
        needs_review=_bool_label(_field(raw, "needs_review"), field="needs_review"),
        outcome=outcome,
        risk=risk,
        urgency=urgency,
        answer_confidence=_confidence_map(_field(raw, "answer_confidence")),
    )
    validate_trace_grade(grade)
    return grade


def normalise_tool_call(raw: Any) -> ToolCallGrade:
    """A model tool-call verdict object (or dict) into a `ToolCallGrade`.

    The model card documents the `tool_call` family's questions but no
    closed label set for `should_call_tool`/`call_verdict` (unlike
    `merge_action`), so this validates shape only -- both fields must be
    non-empty strings -- and refuses to invent a label set to coerce into.
    """
    should = _field(raw, "should_call_tool")
    verdict = _field(raw, "call_verdict")
    if not isinstance(should, str) or not should.strip():
        raise ValueError(f"coding decision agent: should_call_tool is not a label ({should!r})")
    if not isinstance(verdict, str) or not verdict.strip():
        raise ValueError(f"coding decision agent: call_verdict is not a label ({verdict!r})")
    return ToolCallGrade(
        should_call_tool=should,
        call_verdict=verdict,
        answer_confidence=_confidence_map(_field(raw, "answer_confidence")),
    )


def load_backend(model_id: str = DEFAULT_MODEL_ID) -> GraderBackend:
    """Load the real backend, or raise `CodeOrchestratorUnavailable`.

    Import is done here (not at module import) so `jev` starts without torch.
    Preference order, both cheap to check:

    1. The model card's `coding_decision_agent.CodingDecisionAgent`, if
       installed (the training repo's package; unpublished as of 2026-10-24,
       so this branch is for the day it exists).
    2. Baton's built-in `LayaBackend` (`laya_backend.py`): the same
       checkpoint through the `laya` runtime, with the trained question
       wording from the public case dataset. Presence is checked with
       `find_spec`, not an import -- `import laya` pulls torch, and this
       loader must stay cheap enough for `jev code status`.

    Neither branch downloads weights: both backends load lazily on first
    grade, so merely checking availability never fetches the ~842 MB
    checkpoint.
    """
    try:
        module = importlib.import_module("coding_decision_agent")
    except ImportError:
        module = None
    if module is not None:
        factory = getattr(module, "CodingDecisionAgent", None)
        if factory is not None:
            try:
                return cast(GraderBackend, factory(model_id))
            except Exception as exc:
                raise CodeOrchestratorUnavailable(
                    f"could not load Coding Decision Agent {model_id!r}: {exc}"
                ) from exc
    if importlib.util.find_spec("laya") is None:
        raise CodeOrchestratorUnavailable(
            "the Coding Decision Agent is not installed: install `laya` "
            "(`pip install laya`; CPU-only torch works -- see "
            "docs/code-orchestrator.md), then retry. Baton does not bundle "
            "the model, and it will not guess the model's fixed question "
            "wording through a raw `laya` call."
        )
    from jev_agent.code_orchestrator.laya_backend import LayaBackend

    return LayaBackend(model_id)


@dataclass
class CodeOrchestrator:
    """The code orchestrator: grades and routes, gated, never deciding alone.

    `backend=None` means "load lazily on first use": `available` is then
    unknown until a grade is attempted, and a missing backend surfaces as
    `CodeOrchestratorUnavailable` from `grade_code`/`route`, never as a fake
    grade. Pass an explicit backend (a fake in tests, a loaded model in
    production) to skip loading.

    Error taxonomy, decided consciously (issue #203, C3) because the old
    code left it implicit and inconsistent:

    - Missing backend -> `CodeOrchestratorUnavailable`. The quality gate
      records "unavailable" and does NOT block (grading is opt-in; a user
      who enabled it without installing the model gets a note, not a
      broken run), and D4 proceeds without a hint.
    - Backend raised during inference, or the backend object itself is
      unusable -> `CodeOrchestratorError` (not the subclass above). The
      quality gate blocks fail-closed on it: a grade was attempted and
      cannot be trusted, which is not the same as no grade being
      configured. D4 still proceeds without a hint -- a routing hint is
      advisory, so a failed hint must never crash `_assign`.
    - Mis-shaped verdict -> `ValueError` from the normalisers. The quality
      gate blocks fail-closed on it (same reasoning as an inference
      failure); D4 proceeds without a hint. The CLI reports both cases
      with `fail(...)`, never a traceback.
    """

    model_id: str = DEFAULT_MODEL_ID
    backend: GraderBackend | None = None
    max_diff_chars: int = MAX_CONTEXT_CHARS

    def __post_init__(self) -> None:
        if self.max_diff_chars <= 0:
            raise ValueError(f"max_diff_chars must be positive, got {self.max_diff_chars}")

    def _backend(self) -> GraderBackend:
        if self.backend is None:
            self.backend = load_backend(self.model_id)
        return self.backend

    @property
    def available(self) -> bool:
        """True only when a backend is already loaded or loadable now."""
        if self.backend is not None:
            return True
        try:
            self._backend()
        except CodeOrchestratorUnavailable:
            return False
        return True

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """One backend call, with inference failures wrapped (C3).

        Only the backend call itself is wrapped: normalisation runs
        outside this, so a mis-shaped verdict stays a `ValueError` and is
        not mistaken for an inference failure.
        """
        backend = self._backend()
        fn = getattr(backend, method, None)
        if fn is None:
            raise CodeOrchestratorUnavailable(
                f"the installed Coding Decision Agent backend does not expose "
                f"`{method}`; install the backend from the "
                f"S4MPL3BI4S/Coding_Decision_Agent model repo that does, or "
                f"use grade_code/route_model."
            )
        try:
            return fn(*args, **kwargs)
        except CodeOrchestratorError:
            raise
        except Exception as exc:
            raise CodeOrchestratorError(
                f"coding decision agent backend failed on `{method}`: {type(exc).__name__}: {exc}"
            ) from exc

    def grade_code(self, *, task: str, diff: str, tests: str = "") -> CodeReviewGrade:
        """Grade a diff, compacting it first (issue #203, I10).

        Compaction lives here, not in the caller: the old contract ("the
        caller compacts first") meant any caller that forgot sent an
        over-context diff to the model. Callers may still pre-compact --
        `compact_diff` is idempotent for diffs already within budget --
        but they no longer have to remember. `max_diff_chars` is the
        caller's configured budget (the quality gate and CLI pass
        `[code_orchestrator] max_diff_chars`); the default is the model's
        7000-character budget.
        """
        compacted = compact_diff(diff, max_chars=self.max_diff_chars)
        return normalise_code_review(
            self._call("grade_code", task=task, diff=compacted, tests=tests)
        )

    def route(self, request: str) -> RoutingGrade:
        """Route a coding task to a model tier and skill."""
        return normalise_routing(self._call("route_model", request))

    def grade_trace(self, trace: str) -> AgentTraceGrade:
        """Grade an agent trace: what a running coding agent should do next.

        Wired through the backend only when the installed backend exposes
        `grade_trace` (issue #203, I11): Baton does not invent a call
        shape or question wording for the trace family, so a backend
        without the method raises `CodeOrchestratorUnavailable` instead
        of scoring against an untrained wording. The returned grade is
        gated by `gate_trace` in `grades.py`, never trusted raw.
        """
        return normalise_trace(self._call("grade_trace", trace))

    def grade_tool_call(self, tool_call: str) -> ToolCallGrade:
        """Grade a tool call, on the same backend-if-exposed terms as
        `grade_trace` (issue #203, I11)."""
        return normalise_tool_call(self._call("grade_tool_call", tool_call))
