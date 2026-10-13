"""Jev dispatcher: total timeout around the call and one retry on connect errors (issue #36)."""

import asyncio
import time
from typing import Any

import httpx
import pytest

from jev_agent.core.models import DecisionRequest
from jev_agent.dispatch.base import DispatchError
from jev_agent.dispatch.guard import GuardedDispatcher
from jev_agent.dispatch.jev import JevDispatcher, JevSettings
from jev_agent.dispatch.rules import RulesDispatcher

KEY = "sk-or-test-SECRET-1234567890"
OK_BODY: dict[str, Any] = {
    "answers": {"verdict": {"type": "choice", "choice": "approve", "confidence": 0.9}}
}


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)


def _d3() -> DecisionRequest:
    return DecisionRequest(
        point="D3",
        state={"validation": {"verdict": "approve", "issue_count": 0}},
        questions={"verdict": ["approve", "revise", "escalate_human"]},
    )


def _jev(handler: Any, settings: JevSettings | None = None) -> tuple[JevDispatcher, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JevDispatcher(settings, client=client, sleep=fake_sleep), sleeps


async def test_connect_error_is_retried_once_then_succeeds() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("dns failure")
        return httpx.Response(200, json=OK_BODY)

    jev, sleeps = _jev(handler)
    decision = await jev.decide(_d3())
    assert decision.answers == {"verdict": "approve"}
    assert len(calls) == 2
    assert sleeps == [1.0]


async def test_connect_error_twice_raises_network_error() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("refused")

    jev, _ = _jev(handler)
    with pytest.raises(DispatchError, match=r"network error \(ConnectError\)"):
        await jev.decide(_d3())
    assert len(calls) == 2


async def test_connect_error_then_overload_shares_the_single_retry() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(529, text="busy")

    jev, _ = _jev(handler)
    with pytest.raises(DispatchError, match="HTTP 529"):
        await jev.decide(_d3())
    assert len(calls) == 2


async def test_read_errors_are_not_retried() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadError("reset")  # the server may already have processed the request

    jev, _ = _jev(handler)
    with pytest.raises(DispatchError, match="network error"):
        await jev.decide(_d3())
    assert len(calls) == 1


async def test_slow_server_is_bounded_by_the_total_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=OK_BODY)

    jev, _ = _jev(handler, JevSettings(timeout_s=0.2))
    started = time.monotonic()
    with pytest.raises(DispatchError, match=r"timed out after 0.2s"):
        await jev.decide(_d3())
    assert time.monotonic() - started < 2

    jev, _ = _jev(handler, JevSettings(timeout_s=0.2))
    guarded = await GuardedDispatcher(jev, RulesDispatcher()).decide(_d3())
    assert guarded.decision.dispatcher == "rules"
    assert guarded.fallback_reason is not None and "timed out" in guarded.fallback_reason


async def test_retry_backoff_counts_toward_the_total_timeout() -> None:
    """A real backoff before the retry cannot stretch the call past timeout_s."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(529, text="busy", headers={"retry-after": "5"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    jev = JevDispatcher(JevSettings(timeout_s=0.2), client=client)  # real asyncio.sleep
    started = time.monotonic()
    with pytest.raises(DispatchError, match="timed out"):
        await jev.decide(_d3())
    assert time.monotonic() - started < 2
    assert len(calls) == 1
