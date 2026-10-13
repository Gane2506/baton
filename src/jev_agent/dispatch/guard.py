"""Validate dispatcher answers and fall back when the primary dispatcher fails (PLAN.md 4, 5).

The harness never trusts a dispatcher: a decision is used only if every answer is one of the
allowed values. Otherwise the fallback dispatcher answers, and the reason is kept so it can be
stored with the decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from jev_agent.core.models import Decision, DecisionRequest
from jev_agent.dispatch.base import Dispatcher, DispatchError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuardedDecision:
    decision: Decision
    fallback_reason: str | None  # why the fallback answered instead of the primary, if it did


class GuardedDispatcher:
    """Ask `primary`; on an error or an invalid answer, ask `fallback` instead."""

    def __init__(self, primary: Dispatcher, fallback: Dispatcher | None = None) -> None:
        self.primary = primary
        self.fallback = fallback

    async def decide(self, req: DecisionRequest) -> GuardedDecision:
        decision, problem = await _ask(self.primary, req)
        if decision is not None:
            return GuardedDecision(decision, None)
        reason = f"{self.primary.name}: {problem}"
        log.warning("dispatcher %s failed at %s: %s", self.primary.name, req.point, problem)
        if self.fallback is None:
            raise DispatchError(reason)
        decision, fallback_problem = await _ask(self.fallback, req)
        if decision is None:
            raise DispatchError(f"{reason}; fallback {self.fallback.name}: {fallback_problem}")
        return GuardedDecision(decision, reason)


async def _ask(dispatcher: Dispatcher, req: DecisionRequest) -> tuple[Decision | None, str]:
    """Return (decision, "") if it is valid, else (None, what went wrong)."""
    try:
        decision = await dispatcher.decide(req)
    except Exception as exc:  # any dispatcher failure must fall back, not crash the run
        return None, f"raised {type(exc).__name__}: {exc}"
    if decision.point != req.point:
        return None, f"answered point {decision.point}, expected {req.point}"
    invalid = decision.invalid_answers(req)
    if invalid:
        return None, "invalid answers: " + "; ".join(invalid)
    return decision, ""
