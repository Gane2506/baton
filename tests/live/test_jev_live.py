"""Live smoke test for the Jev dispatcher. Never runs in CI.

Run with: JEV_LIVE_TESTS=1 OPENROUTER_API_KEY=... uv run pytest tests/live/test_jev_live.py
or, for native TypeSafe: JEV_LIVE_TESTS=1 JEV_LIVE_NATIVE=1 TYPESAFE_API_KEY=... (same command).
Costs a fraction of a cent.
"""

import os

import pytest

from jev_agent.core.models import DecisionRequest
from jev_agent.dispatch.jev import JevDispatcher, JevSettings

_LIVE = os.environ.get("JEV_LIVE_TESTS") == "1"
_SETTINGS = JevSettings.for_native() if os.environ.get("JEV_LIVE_NATIVE") == "1" else JevSettings()

pytestmark = pytest.mark.skipif(
    not _LIVE or not os.environ.get(_SETTINGS.api_key_env),
    reason=f"set JEV_LIVE_TESTS=1 and {_SETTINGS.api_key_env} to run live Jev tests",
)


async def test_live_d4_decision() -> None:
    req = DecisionRequest(
        point="D4",
        state={
            "task": {
                "id": "T1",
                "title": "Fix a typo in the README",
                "files_hint": ["README.md"],
                "criteria_count": 1,
            },
            "quota": {"claude_code": "ok", "codex": "ok"},
            "prior_attempts": 0,
        },
        questions={
            "complexity": ["trivial", "standard", "hard"],
            "effort": ["low", "medium", "high"],
        },
    )
    jev = JevDispatcher(JevSettings(**{**_SETTINGS.model_dump(), "min_confidence": 0.0}))
    decision = await jev.decide(req)
    assert decision.invalid_answers(req) == []
    assert decision.model
    assert decision.cost_usd is not None
    assert decision.details["answers"]
