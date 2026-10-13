"""The dispatcher interface (PLAN.md section 5).

A dispatcher answers one `DecisionRequest` with one `Decision`. The harness validates every
answer against the request's allowed values and falls back to the rules dispatcher when a
dispatcher errors or answers outside the allowed set.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from jev_agent.core.models import Decision, DecisionRequest


class DispatchError(Exception):
    """A dispatcher could not produce a decision (network error, bad response, ...)."""


@runtime_checkable
class Dispatcher(Protocol):
    name: str

    async def decide(self, req: DecisionRequest) -> Decision: ...
