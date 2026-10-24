"""Backend for the Coding Decision Agent: load it, normalise its verdicts.

Two facts shape this module, and both are stated here so a later reader does
not "fix" them into a bug:

1. `coding_decision_agent` is NOT on PyPI (checked 2026-10-24: the PyPI JSON
   endpoint 404s, and the model card's install block only says
   `pip install laya`). The card says the fixed question wording "ships in
   the `coding_decision_agent` package and in `configs/schema.py` of the
   training repo". So this module does NOT invent question wording and does
   NOT call `laya.load(...)` with guessed questions: a guessed wording would
   score against a distribution the model was never trained on, and the
   calibrated confidence would be meaningless. The supported backend is the
   real `coding_decision_agent.CodingDecisionAgent` (from the training repo /
   the model repo's package, installed alongside `laya`).
2. The model is heavy (a 421M ModernBERT derivative, via `laya` + torch) and
   must never become a hard dependency of `jev`: importing this module must
   not import torch, and CI must not download weights. Loading is lazy, and
   when the package is absent every grade call fails with a clear
   `CodeOrchestratorUnavailable` instead of a traceback or a faked grade.
   Callers that can proceed without a grade (the quality gate) treat that as
   "not graded"; callers that cannot (the `jev code` CLI) report it and stop.

A backend is any object with `grade_code(task, diff, tests)` and
`route_model(request)` returning objects with the attributes the model card
documents (`merge_action`, `code_quality`, `instruction_followed`,
`likely_correct`, `answer_confidence`, and for routing `model_tier`,
`task_difficulty`, `skill`, `answer_confidence`). Tests inject a fake; the
loader below is the only place the real package is touched.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Protocol, cast

from jev_agent.code_orchestrator.grades import (
    DEFAULT_MODEL_ID,
    CodeReviewGrade,
    RoutingGrade,
)

__all__ = [
    "CodeOrchestrator",
    "CodeOrchestratorUnavailable",
    "GraderBackend",
    "load_backend",
    "normalise_code_review",
    "normalise_routing",
]


class CodeOrchestratorUnavailable(Exception):
    """The Coding Decision Agent backend is not installed or cannot load."""


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


def _bool_label(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("yes", "true", "1")
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


def load_backend(model_id: str = DEFAULT_MODEL_ID) -> GraderBackend:
    """Load the real backend, or raise `CodeOrchestratorUnavailable`.

    Import is done here (not at module import) so `jev` starts without torch.
    """
    try:
        module = importlib.import_module("coding_decision_agent")
    except ImportError as exc:
        raise CodeOrchestratorUnavailable(
            "the Coding Decision Agent is not installed: install `laya` "
            "(`pip install laya`) and the `coding_decision_agent` package from "
            "the S4MPL3BI4S/Coding_Decision_Agent model repo, then retry. "
            "Baton does not bundle the model, and it will not guess the "
            "model's fixed question wording through a raw `laya` call."
        ) from exc
    factory = getattr(module, "CodingDecisionAgent", None)
    if factory is None:
        raise CodeOrchestratorUnavailable(
            "the installed `coding_decision_agent` package has no "
            "`CodingDecisionAgent` class; install the package from the "
            "S4MPL3BI4S/Coding_Decision_Agent model repo."
        )
    try:
        return cast(GraderBackend, factory(model_id))
    except Exception as exc:
        raise CodeOrchestratorUnavailable(
            f"could not load Coding Decision Agent {model_id!r}: {exc}"
        ) from exc


@dataclass
class CodeOrchestrator:
    """The code orchestrator: grades and routes, gated, never deciding alone.

    `backend=None` means "load lazily on first use": `available` is then
    unknown until a grade is attempted, and a missing backend surfaces as
    `CodeOrchestratorUnavailable` from `grade_code`/`route`, never as a fake
    grade. Pass an explicit backend (a fake in tests, a loaded model in
    production) to skip loading.
    """

    model_id: str = DEFAULT_MODEL_ID
    backend: GraderBackend | None = None

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

    def grade_code(self, *, task: str, diff: str, tests: str = "") -> CodeReviewGrade:
        """Grade a diff. The caller compacts the diff first (see `grades`)."""
        return normalise_code_review(self._backend().grade_code(task=task, diff=diff, tests=tests))

    def route(self, request: str) -> RoutingGrade:
        """Route a coding task to a model tier and skill."""
        return normalise_routing(self._backend().route_model(request))

    # Note: the model card also documents `tool_call` and `agent_trace`
    # families (their labels live in `grades.py`, and `gate_trace` is pure
    # and tested). They are NOT wired here: the card shows a public call
    # shape only for `grade_code` and `route_model`, and wiring the other
    # two on a guessed signature would score against an untrained wording.
    # See docs/code-orchestrator.md "Not yet wired".
