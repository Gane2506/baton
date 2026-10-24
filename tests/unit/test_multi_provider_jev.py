"""Native TypeSafe first, Jev via OpenRouter on provider failure.

The dispatcher must never be single-dependency: `MultiProviderJevDispatcher` tries the
native TypeSafe API first and only sends the same payload through OpenRouter when the
native provider fails (unreachable, timed out, rate-limited, auth/key problems).
`Decision.details["provider"]` records who served each decision.
"""

import json
from pathlib import Path

import httpx
import pytest

from jev_agent.config.schema import JevSettings
from jev_agent.core.models import DecisionRequest
from jev_agent.dispatch.base import Dispatcher, DispatchError
from jev_agent.dispatch.jev import (
    NATIVE_PROVIDER,
    OPENROUTER_PROVIDER,
    MultiProviderJevDispatcher,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "jev"
NATIVE_URL = "https://api.typesafe.ai/v1/systemone"
OPENROUTER_URL = "https://openrouter.ai/api/v1/systemone"


def _d4() -> DecisionRequest:
    return DecisionRequest(
        point="D4",
        state={"task": {"id": "T3", "title": "Add rate limiting", "criteria_count": 3}},
        questions={
            "complexity": ["trivial", "standard", "hard"],
            "worker": ["claude_code", "codex", "api"],
            "model": ["anthropic/claude-sonnet-4.6", "openai/gpt-5.1-codex"],
            "effort": ["low", "medium", "high"],
        },
    )


class Recorder:
    """A MockTransport handler that replays responses and records requests."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def _ok() -> httpx.Response:
    return httpx.Response(
        200, json=json.loads((FIXTURES / "d4_openrouter_success.json").read_text())
    )


def _failover(recorder: Recorder) -> tuple[MultiProviderJevDispatcher, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    dispatcher = MultiProviderJevDispatcher(
        JevSettings.for_native(), JevSettings(), client=client, sleep=fake_sleep
    )
    return dispatcher, sleeps


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-typesafe-test-1234567890")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1234567890")


def test_implements_dispatcher_protocol() -> None:
    dispatcher = MultiProviderJevDispatcher(JevSettings.for_native(), JevSettings())
    assert isinstance(dispatcher, Dispatcher)
    assert dispatcher.name == "jev"


async def test_native_serves_first() -> None:
    rec = Recorder(_ok())
    dispatcher, _ = _failover(rec)
    decision = await dispatcher.decide(_d4())

    assert rec.urls() == [NATIVE_URL]
    assert json.loads(rec.requests[0].content)["model"] == "jev-latest"
    assert decision.details["provider"] == NATIVE_PROVIDER
    assert decision.dispatcher == "jev"


async def test_failover_to_openrouter_on_connect_error() -> None:
    rec = Recorder(httpx.ConnectError("boom"), httpx.ConnectError("boom"), _ok())
    dispatcher, sleeps = _failover(rec)
    decision = await dispatcher.decide(_d4())

    # Native gets its one retry, then the same payload goes through OpenRouter.
    assert rec.urls() == [NATIVE_URL, NATIVE_URL, OPENROUTER_URL]
    assert sleeps == [1.0]  # only the native retry sleeps; the switch itself does not
    assert json.loads(rec.requests[2].content)["model"] == "~typesafe/jev-latest"
    assert decision.details["provider"] == OPENROUTER_PROVIDER


async def test_failover_after_native_retry_exhausted() -> None:
    bad = httpx.Response(500, json={"error": "unavailable"})
    rec = Recorder(bad, bad, _ok())
    dispatcher, _ = _failover(rec)
    decision = await dispatcher.decide(_d4())

    assert rec.urls() == [NATIVE_URL, NATIVE_URL, OPENROUTER_URL]
    assert decision.details["provider"] == OPENROUTER_PROVIDER


async def test_no_failover_on_oversized_state() -> None:
    rec = Recorder(_ok())
    dispatcher, _ = _failover(rec)
    payload = {"state": {"pad": "x" * 100_000}, "questions": {"q": {"type": "choice"}}}

    with pytest.raises(DispatchError, match="state too large"):
        await dispatcher.post(payload)
    assert rec.requests == []


async def test_no_failover_on_rejected_payload() -> None:
    body = json.loads((FIXTURES / "error_422.json").read_text())
    rec = Recorder(httpx.Response(422, json=body), _ok())
    dispatcher, _ = _failover(rec)

    with pytest.raises(DispatchError, match="request rejected"):
        await dispatcher.decide(_d4())
    assert rec.urls() == [NATIVE_URL]  # the payload would fail on OpenRouter too


async def test_both_providers_failing_raises() -> None:
    rec = Recorder(
        httpx.ConnectError("native down"),
        httpx.ConnectError("native down"),
        httpx.ConnectError("openrouter down"),
        httpx.ConnectError("openrouter down"),
    )
    dispatcher, _ = _failover(rec)

    with pytest.raises(DispatchError, match="network error"):
        await dispatcher.decide(_d4())
    assert rec.urls() == [NATIVE_URL, NATIVE_URL, OPENROUTER_URL, OPENROUTER_URL]


async def test_missing_native_key_uses_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(_ok())
    dispatcher, _ = _failover(rec)
    monkeypatch.delenv("TYPESAFE_API_KEY")
    decision = await dispatcher.decide(_d4())

    assert rec.urls() == [OPENROUTER_URL]
    assert decision.details["provider"] == OPENROUTER_PROVIDER


async def test_missing_both_keys_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    rec = Recorder()
    dispatcher, _ = _failover(rec)

    with pytest.raises(DispatchError, match="OPENROUTER_API_KEY is not set"):
        await dispatcher.decide(_d4())
    assert rec.requests == []


def test_failover_keys_on_the_structured_marker_not_the_message() -> None:
    """I9: rewording a provider-failure message must not disable failover,
    and a message that merely mentions one must not enable it."""
    from jev_agent.dispatch.jev import _provider_failed

    assert _provider_failed(DispatchError("totally reworded outage", provider_failure=True))
    assert not _provider_failed(
        DispatchError("jev: network error (but this is not a provider failure)")
    )
    assert not _provider_failed(DispatchError("jev: request rejected (HTTP 422): nope"))
    assert _provider_failed(DispatchError("jev: auth failed (HTTP 401)", provider_failure=True))


async def test_auth_failure_fails_over() -> None:
    rec = Recorder(httpx.Response(401, json={"error": {"message": "bad key"}}), _ok())
    dispatcher, _ = _failover(rec)
    decision = await dispatcher.decide(_d4())

    assert rec.urls() == [NATIVE_URL, OPENROUTER_URL]
    assert decision.details["provider"] == OPENROUTER_PROVIDER
