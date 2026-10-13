import pytest

from jev_agent.core.models import Decision, DecisionRequest
from jev_agent.dispatch.base import DispatchError
from jev_agent.dispatch.guard import GuardedDispatcher
from jev_agent.dispatch.rules import RulesDispatcher

REQ = DecisionRequest(
    point="D6",
    state={"merged": True, "tests": "passed"},
    questions={"action": ["ship", "fix_task", "human"]},
)


class Stub:
    def __init__(
        self,
        answers: dict[str, str] | None = None,
        point: str = "D6",
        error: Exception | None = None,
    ):
        self.name = "stub"
        self.answers = answers or {}
        self.point = point
        self.error = error

    async def decide(self, req: DecisionRequest) -> Decision:
        if self.error is not None:
            raise self.error
        return Decision(point=self.point, answers=self.answers, dispatcher=self.name)  # type: ignore[arg-type]


async def test_valid_primary_answer_is_used() -> None:
    guarded = await GuardedDispatcher(Stub({"action": "human"}), RulesDispatcher()).decide(REQ)
    assert guarded.decision.answers == {"action": "human"}
    assert guarded.fallback_reason is None


@pytest.mark.parametrize(
    ("primary", "reason"),
    [
        (Stub({"action": "deploy"}), "'deploy' not in"),
        (Stub({}), "action: missing"),
        (Stub({"action": "ship"}, point="D5"), "expected D6"),
        (Stub(error=DispatchError("timeout")), "DispatchError: timeout"),
        (Stub(error=ValueError("bad json")), "ValueError: bad json"),
    ],
)
async def test_fallback_on_error_or_invalid_answer(primary: Stub, reason: str) -> None:
    guarded = await GuardedDispatcher(primary, RulesDispatcher()).decide(REQ)
    assert guarded.decision.dispatcher == "rules"
    assert guarded.decision.answers == {"action": "ship"}
    assert guarded.fallback_reason is not None
    assert guarded.fallback_reason.startswith("stub: ")
    assert reason in guarded.fallback_reason


async def test_no_fallback_raises() -> None:
    with pytest.raises(DispatchError, match="stub"):
        await GuardedDispatcher(Stub({"action": "x"})).decide(REQ)


async def test_invalid_fallback_raises() -> None:
    with pytest.raises(DispatchError, match="fallback stub"):
        await GuardedDispatcher(Stub({"action": "x"}), Stub({"action": "y"})).decide(REQ)
