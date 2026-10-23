"""DS-03: the studio's Jev+Laya stack recommendation (issue #124).

The decision service is tested with a fake dispatcher behind the `Dispatcher`
protocol — no network, no keys. A separate integration block in
`test_dashboard_design_studio.py` covers the wizard wiring.
"""

from __future__ import annotations

import pytest

from jev_agent.core.design_kit import APP_TYPES, STACKS
from jev_agent.core.models import Decision, DecisionRequest
from jev_agent.dashboard import studio_decisions
from jev_agent.dashboard.design_studio import stack_options
from jev_agent.dashboard.studio_decisions import (
    StackRecommendation,
    build_stack_request,
    default_recommendation,
    recommend_stack,
)
from jev_agent.dispatch.base import DispatchError


class _FakeDispatcher:
    """A `Dispatcher` whose `decide` returns a canned decision or raises."""

    name = "fake"

    def __init__(self, decision: Decision | None = None, error: Exception | None = None) -> None:
        self.decision = decision
        self.error = error
        self.seen: list[DecisionRequest] = []

    async def decide(self, req: DecisionRequest) -> Decision:
        self.seen.append(req)
        if self.error is not None:
            raise self.error
        assert self.decision is not None
        return self.decision


def _settled(choice: str) -> Decision:
    """A consensus decision that settled on `choice` with Laya's agreement."""
    return Decision(
        point="D1",
        answers={"stack": choice},
        dispatcher="consensus",
        model="jev-latest",
        details={
            "answers": {
                "stack": {
                    "choice": choice,
                    "confidence": 0.9,
                    "probabilities": {choice: 0.9},
                }
            },
            "laya": {
                "stack": {
                    "choice": choice,
                    "totals": {choice: 4.2},
                    "margin": 0.6,
                }
            },
        },
    )


# ---- the decision request ---------------------------------------------------------------


def test_build_stack_request_shape() -> None:
    options = list(STACKS["mobile"])
    req = build_stack_request("Acme Analytics", "mobile", options, vibe="warm")
    assert req.point == "D1"
    assert req.questions == {"stack": options}
    assert req.state["product"] == "Acme Analytics"
    assert req.state["app_type"] == "mobile"
    assert req.state["vibe"] == "warm"
    decision = Decision(point="D1", answers={"stack": options[0]}, dispatcher="fake")
    assert decision.invalid_answers(req) == []


# ---- the happy path ---------------------------------------------------------------------


async def test_recommend_stack_uses_jev_and_laya() -> None:
    fake = _FakeDispatcher(decision=_settled("Flutter"))
    rec = await recommend_stack("Acme Analytics", "mobile", dispatcher=fake)
    assert rec.source == "jev"
    assert rec.stack == "Flutter"
    assert rec.laya_agreed is True
    assert rec.laya_margin == pytest.approx(0.6)
    assert "Jev picked Flutter (90% confidence)" in rec.rationale
    assert "Laya" in rec.rationale and "0.6-point lead" in rec.rationale
    # The dispatcher saw exactly one request, with the kit's stacks as the answers.
    assert len(fake.seen) == 1
    assert fake.seen[0].questions == {"stack": list(STACKS["mobile"])}


# ---- the fallback paths -----------------------------------------------------------------


async def test_recommend_stack_falls_back_when_dispatcher_raises() -> None:
    fake = _FakeDispatcher(error=DispatchError("the network is down"))
    rec = await recommend_stack("Acme Analytics", "mobile", dispatcher=fake)
    assert rec.source == "default"
    assert rec.stack == STACKS["mobile"][0]
    assert rec.laya_agreed is False
    assert rec.laya_margin is None
    assert "could not agree" in rec.rationale


async def test_recommend_stack_falls_back_without_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The conftest already strips both keys; belt and braces for the no-keys path.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    rec = await recommend_stack("Acme Analytics", "dashboard")
    assert rec.source == "default"
    assert rec.stack == STACKS["dashboard"][0]
    assert "no API key" in rec.rationale


async def test_recommend_stack_rejects_an_out_of_range_answer() -> None:
    decision = Decision(point="D1", answers={"stack": "COBOL"}, dispatcher="consensus", details={})
    rec = await recommend_stack(
        "Acme Analytics", "mobile", dispatcher=_FakeDispatcher(decision=decision)
    )
    assert rec.source == "default"
    assert rec.stack == STACKS["mobile"][0]


async def test_recommend_stack_unknown_app_type() -> None:
    with pytest.raises(KeyError):
        await recommend_stack("Acme", "spaceship")


def test_default_recommendation_reasons() -> None:
    for reason in ("no_keys", "no_consensus", "not_asked"):
        rec = default_recommendation("website", reason)
        assert rec.source == "default"
        assert rec.stack == STACKS["website"][0]
        assert APP_TYPES["website"] in rec.rationale


# ---- the cached shape -------------------------------------------------------------------


def test_recommendation_state_roundtrip() -> None:
    rec = StackRecommendation(
        stack="Flutter",
        source="jev",
        rationale="Jev picked Flutter; Laya agreed.",
        laya_agreed=True,
        laya_margin=0.6,
    )
    back = StackRecommendation.from_state(rec.to_state())
    assert back == rec


def test_recommendation_from_state_rejects_garbage() -> None:
    assert StackRecommendation.from_state(None) is None
    assert StackRecommendation.from_state({}) is None
    assert StackRecommendation.from_state({"stack": "Flutter"}) is None
    assert StackRecommendation.from_state({"stack": "Flutter", "source": "chatgpt"}) is None
    assert (
        StackRecommendation.from_state(
            {"stack": "Flutter", "source": "jev", "laya_margin": "nonsense"}
        )
        is None
    )


# ---- the stack_options seam ---------------------------------------------------------------


def test_stack_options_jev_pick_comes_first() -> None:
    options, recommended = stack_options("mobile", "Flutter")
    assert recommended == "Flutter"
    assert options[0] == "Flutter"
    assert sorted(options) == sorted(STACKS["mobile"])


def test_stack_options_ignores_an_unknown_recommendation() -> None:
    options, recommended = stack_options("mobile", "COBOL")
    assert (options, recommended) == stack_options("mobile")
    assert recommended == STACKS["mobile"][0]


def test_stack_options_still_defaults_without_a_pick() -> None:
    # The DS-01 contract: no cached pick, the static default is recommended first.
    options, recommended = stack_options("pwa")
    assert recommended == options[0] == STACKS["pwa"][0]
    assert studio_decisions.STACK_REC_KEY == "stack_recommendation"
