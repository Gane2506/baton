"""The dispatcher interface (PLAN.md section 5).

A dispatcher answers one `DecisionRequest` with one `Decision`. The harness validates every
answer against the request's allowed values and falls back to the rules dispatcher when a
dispatcher errors or answers outside the allowed set.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from jev_agent.core.models import Decision, DecisionRequest


class DispatchError(Exception):
    """A dispatcher could not produce a decision (network error, bad response, ...).

    `provider_failure` is the structured failover marker (issue #203, I9):
    True when this provider cannot serve the request right now (unreachable,
    timed out, rate-limited, auth/key problems, a non-JSON response) and the
    other leg of `MultiProviderJevDispatcher` might. Failover keys on this
    flag, never on matching the message text -- a reworded message used to
    silently disable failover. Failures that would hit both providers
    identically (oversized state, a rejected payload) leave it False.
    """

    def __init__(self, message: str = "", *, provider_failure: bool = False) -> None:
        super().__init__(message)
        self.provider_failure = provider_failure


@runtime_checkable
class Dispatcher(Protocol):
    name: str

    async def decide(self, req: DecisionRequest) -> Decision: ...
