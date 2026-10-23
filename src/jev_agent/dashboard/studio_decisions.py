"""Jev+Laya stack recommendations for the Design Studio wizard (issue #124, DS-03).

The wizard's `stack_options()` seam stays `(ordered list, recommended)`; this module
decides the recommended entry with a real Jev decision behind Laya consensus, and the
wizard caches one decision per session. Jev and Laya are used only through the existing
dispatch machinery — nothing here reimplements them.

When Jev cannot serve the decision (no API keys configured, a network or consensus
failure), the module falls back to the design kit's static default and says so: every
recommendation carries its `source` (`"jev"` or `"default"`) so the wizard page labels
it honestly. Nothing here logs or renders credentials; the dispatcher reads its key
from the environment itself.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from jev_agent.config.schema import JevSettings, LayaSettings
from jev_agent.core.design_kit import APP_TYPES, STACKS
from jev_agent.core.models import Decision, DecisionPoint, DecisionRequest
from jev_agent.dispatch.base import Dispatcher
from jev_agent.dispatch.consensus import ConsensusDispatcher
from jev_agent.dispatch.jev import MultiProviderJevDispatcher

__all__ = [
    "STACK_REC_KEY",
    "RecommendationSource",
    "StackRecommendation",
    "build_stack_request",
    "default_recommendation",
    "recommend_stack",
]

log = logging.getLogger(__name__)

#: The wizard-state key holding this session's cached recommendation.
STACK_REC_KEY = "stack_recommendation"

RecommendationSource = Literal["jev", "default"]
"""Where the recommendation came from: a Jev+Laya decision, or the static default."""

_FallbackReason = Literal["no_keys", "no_consensus", "not_asked"]

# D1 is intake ("is the idea clear enough to plan"): the closest pipeline point for a
# studio-side "which stack for this idea" decision. The request's state carries the
# actual question, so Jev sees the studio context rather than pipeline cards.
_STUDIO_POINT: DecisionPoint = "D1"


@dataclass(frozen=True)
class StackRecommendation:
    """One stack pick: the stack, where it came from, and why.

    `laya_agreed` is True only when the pick went through consensus (Laya ranked the
    same stack first with the required margin); `laya_margin` is Laya's lead in points
    on the 1-5 matrix scale, when the decision reported one.
    """

    stack: str
    source: RecommendationSource
    rationale: str
    laya_agreed: bool
    laya_margin: float | None

    def to_state(self) -> dict[str, str]:
        """Serialize for the wizard store (`dict[str, dict[str, str]]`)."""
        return {
            "stack": self.stack,
            "source": self.source,
            "rationale": self.rationale,
            "laya_agreed": "true" if self.laya_agreed else "false",
            "laya_margin": "" if self.laya_margin is None else f"{self.laya_margin:.2f}",
        }

    @classmethod
    def from_state(cls, data: Mapping[str, str] | None) -> StackRecommendation | None:
        """Read back `to_state()` output; None when the session has no (valid) cache."""
        if not data:
            return None
        stack = data.get("stack", "")
        source = data.get("source", "")
        if not stack or source not in ("jev", "default"):
            return None
        resolved: RecommendationSource = "jev" if source == "jev" else "default"
        margin: float | None = None
        raw_margin = data.get("laya_margin", "")
        if raw_margin:
            try:
                margin = float(raw_margin)
            except ValueError:
                return None
        return cls(
            stack=stack,
            source=resolved,
            rationale=data.get("rationale", ""),
            laya_agreed=data.get("laya_agreed", "") == "true",
            laya_margin=margin,
        )


def build_stack_request(
    purpose: str, app_type: str, options: list[str], vibe: str = ""
) -> DecisionRequest:
    """The Jev decision request: "which of these stacks for this product".

    `state` holds compact cards only (never transcripts); the single `stack` question
    lists the design kit's stacks for the app type as the allowed answers.
    """
    state: dict[str, Any] = {
        "product": purpose or "(unnamed product)",
        "app_type": app_type,
        "app_type_description": APP_TYPES[app_type],
        "vibe": vibe or "(no vibe keywords yet)",
        "decision": "recommend the stack this product should ship on",
    }
    return DecisionRequest(point=_STUDIO_POINT, state=state, questions={"stack": list(options)})


def default_recommendation(app_type: str, reason: _FallbackReason) -> StackRecommendation:
    """The design kit's static default, labelled honestly as a fallback."""
    stack = STACKS[app_type][0]
    label = APP_TYPES[app_type]
    rationales = {
        "no_keys": (
            f"The design kit's standard pick for {label} — "
            "Jev was unreachable (no API key configured)."
        ),
        "no_consensus": (
            "Jev and Laya could not agree on a stack — "
            f"using the design kit's standard pick for {label}."
        ),
        "not_asked": (
            f"The design kit's standard pick for {label} — "
            "the studio has not asked Jev in this session."
        ),
    }
    return StackRecommendation(
        stack=stack,
        source="default",
        rationale=rationales[reason],
        laya_agreed=False,
        laya_margin=None,
    )


def _default_dispatcher() -> ConsensusDispatcher | None:
    """Jev behind Laya consensus, or None when no API key is configured.

    Checking the key env vars first keeps the wizard fast offline: with no keys the
    multi-provider dispatcher would only fail after trying both legs.
    """
    native = JevSettings.for_native()
    openrouter = JevSettings()
    if (
        not os.environ.get(native.api_key_env, "").strip()
        and not os.environ.get(openrouter.api_key_env, "").strip()
    ):
        return None
    jev = MultiProviderJevDispatcher(native=native, openrouter=openrouter)
    return ConsensusDispatcher(jev, LayaSettings())


def _jev_rationale(choice: str, decision: Decision) -> str:
    """One line on why Jev+Laya settled on `choice`, from the decision's details."""
    details = decision.details if isinstance(decision.details, dict) else {}
    jev_vote = details.get("answers")
    confidence: float | None = None
    if isinstance(jev_vote, dict):
        vote = jev_vote.get("stack")
        if isinstance(vote, dict) and isinstance(vote.get("confidence"), (int, float)):
            confidence = float(vote["confidence"])
    laya_vote = details.get("laya")
    margin: float | None = None
    if isinstance(laya_vote, dict):
        layas = laya_vote.get("stack")
        if isinstance(layas, dict) and isinstance(layas.get("margin"), (int, float)):
            margin = float(layas["margin"])
    head = f"Jev picked {choice}"
    if confidence is not None:
        head += f" ({confidence:.0%} confidence)"
    tail = "Laya's decision matrix agreed"
    if margin is not None:
        tail += f" with a {margin:.1f}-point lead"
    return f"{head}; {tail}."


def _from_decision(
    decision: Decision, options: list[str], fallback: StackRecommendation
) -> StackRecommendation:
    """A settled consensus decision as a recommendation.

    Anything unexpected — an answer outside the allowed stacks — keeps the fallback:
    the wizard must never offer a stack the design kit does not list.
    """
    choice = decision.answers.get("stack")
    if not isinstance(choice, str) or choice not in options:
        log.warning("studio stack decision answered outside the allowed stacks; using default")
        return fallback
    details = decision.details if isinstance(decision.details, dict) else {}
    margin: float | None = None
    laya_vote = details.get("laya")
    if isinstance(laya_vote, dict):
        layas = laya_vote.get("stack")
        if isinstance(layas, dict) and isinstance(layas.get("margin"), (int, float)):
            margin = float(layas["margin"])
    return StackRecommendation(
        stack=choice,
        source="jev",
        rationale=_jev_rationale(choice, decision),
        laya_agreed=True,  # consensus only returns settled questions
        laya_margin=margin,
    )


async def recommend_stack(
    purpose: str,
    app_type: str,
    *,
    vibe: str = "",
    dispatcher: Dispatcher | None = None,
) -> StackRecommendation:
    """Recommend one of `STACKS[app_type]` via Jev behind Laya consensus.

    The decision is asked once per wizard session by the caller and cached in the
    wizard state; this function itself is stateless. On any AI failure — no keys,
    network error, no consensus, an out-of-range answer — it returns the design
    kit's static default labelled `"default"`, so the wizard never hard-fails for
    lack of AI. Raises KeyError for an unknown app type, like `stack_options`.
    """
    options = list(STACKS[app_type])
    request = build_stack_request(purpose, app_type, options, vibe)
    decide_with = dispatcher if dispatcher is not None else _default_dispatcher()
    if decide_with is None:
        return default_recommendation(app_type, "no_keys")
    fallback = default_recommendation(app_type, "no_consensus")
    try:
        decision = await decide_with.decide(request)
    except Exception as exc:  # the wizard must never hard-fail for lack of AI
        log.warning("studio stack decision failed; using the static default: %s", exc)
        return fallback
    return _from_decision(decision, options, fallback)
