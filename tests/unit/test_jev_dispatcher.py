import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from jev_agent.config.schema import JevConfig
from jev_agent.core.assign import Assignment, fit_free
from jev_agent.core.models import DecisionPoint, DecisionRequest
from jev_agent.dispatch.base import Dispatcher, DispatchError
from jev_agent.dispatch.jev import (
    CODE_ROUTING_INSTRUCTION,
    GENERIC_INSTRUCTION,
    PAIRING_INSTRUCTION,
    PRICE_PER_INPUT_TOKEN_USD,
    JevDispatcher,
    JevSettings,
    parse_answers,
)
from jev_agent.dispatch.requests import (
    D1_CLEAR,
    D3_VERDICT,
    D4_COMPLEXITY,
    D5_ACTION,
    D6_ACTION,
    D7_LEARN,
    RoleOptions,
    model_fits,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "jev"
KEY = "sk-or-test-SECRET-1234567890"
MODELS = ["anthropic/claude-sonnet-4.6", "openai/gpt-5.1-codex"]


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _d4() -> DecisionRequest:
    return DecisionRequest(
        point="D4",
        state={
            "task": {"id": "T3", "title": "Add rate limiting to /api/search", "criteria_count": 3},
            "quota": {"claude_code": "ok", "codex": "ok"},
            "prior_attempts": 0,
        },
        questions={
            "complexity": ["trivial", "standard", "hard"],
            "worker": ["claude_code", "codex", "api"],
            "model": MODELS,
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

    def payload(self, index: int = 0) -> Any:
        return json.loads(self.requests[index].content)


def _ok(name: str) -> httpx.Response:
    return httpx.Response(200, json=_fixture(name))


def _dispatcher(
    recorder: Recorder, settings: JevSettings | None = None
) -> tuple[JevDispatcher, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return JevDispatcher(settings, client=client, sleep=fake_sleep), sleeps


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


def test_implements_dispatcher_protocol() -> None:
    assert isinstance(JevDispatcher(), Dispatcher)
    assert JevDispatcher.name == "jev"


async def test_payload_shape() -> None:
    rec = Recorder(_ok("d4_openrouter_success.json"))
    jev, _ = _dispatcher(rec)
    await jev.decide(_d4())

    request = rec.requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/systemone"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = rec.payload()
    assert body["model"] == "~typesafe/jev-latest"
    assert body["state"]["decision_point"]["id"] == "D4"
    assert body["state"]["decision_point"]["description"]
    assert body["state"]["task"]["id"] == "T3"
    assert set(body["questions"]) == {"complexity", "worker", "model", "effort"}
    for question in body["questions"].values():
        assert question["type"] == "choice"
        assert question["instructions"]
    effort = body["questions"]["effort"]["criteria"]
    assert list(effort) == ["low", "medium", "high"]
    assert all(isinstance(v, str) for v in effort.values())
    assert body["questions"]["model"]["criteria"] == dict.fromkeys(MODELS)


def test_d4_instructions_name_code_routing_only_when_present() -> None:
    """I6: the routing hint is inert unless D4's instructions name it."""
    questions = {"worker": ["claude_code"], "model": ["m"], "effort": ["low"]}
    without = JevDispatcher().build_payload("D4", {}, questions)["questions"]
    for question in without.values():
        assert CODE_ROUTING_INSTRUCTION not in question["instructions"]
    hint = {
        "model_tier": "frontier",
        "skill": "refactor",
        "task_difficulty": "2",
        "suggested_effort": "high",
        "confidence": "0.90",
    }
    with_hint = JevDispatcher().build_payload("D4", {"code_routing": hint}, questions)["questions"]
    for name in ("worker", "model", "effort"):
        assert CODE_ROUTING_INSTRUCTION in with_hint[name]["instructions"]
    # Phase 1 (issue #329): the instruction also names the ledger evidence
    # the hint carries, so Jev knows to prefer well-graded tiers.
    assert "ledger" in CODE_ROUTING_INSTRUCTION
    # And a non-D4 point never gets it, even with the hint in state.
    other = JevDispatcher().build_payload("D2", {"code_routing": hint}, questions)["questions"]
    for question in other.values():
        assert CODE_ROUTING_INSTRUCTION not in question["instructions"]


def test_worker_instructions_do_not_reference_unsent_state() -> None:
    """Ensure worker instructions do not mention state absent from the request contract."""
    dispatcher = JevDispatcher()
    generic = dispatcher.instruction_for("D1", "worker")
    point_specific = dispatcher.build_payload("D4", {}, {"worker": ["claude_code"]})["questions"][
        "worker"
    ]["instructions"]

    for instructions in (generic, point_specific):
        assert "quota" not in instructions
        assert "prior_attempts" not in instructions


@pytest.mark.parametrize(
    ("point", "question", "allowed", "expected"),
    [
        (
            "D1",
            "clear",
            D1_CLEAR,
            {
                "plan": "Plan the idea now.",
                "ask_human": "Ask a human to clarify the idea before planning.",
            },
        ),
        (
            "D3",
            "verdict",
            D3_VERDICT,
            {
                "approve": "The plan is good enough to execute.",
                "revise": "Send the plan back to the planner with the validator's issues.",
                "escalate_human": "A human should decide.",
            },
        ),
        (
            "D4",
            "complexity",
            D4_COMPLEXITY,
            {
                "trivial": "A small, local change such as a rename, config tweak or one-line fix.",
                "standard": "A typical feature or fix touching a few files.",
                "hard": (
                    "Cross-cutting or subtle work: many files, concurrency, security, migrations."
                ),
            },
        ),
        (
            "D5",
            "action",
            D5_ACTION,
            {
                "done": "The task is complete and passed its checks.",
                "retry": "Try again with the same worker, model and effort.",
                "escalate": "Try again with a stronger model or more effort.",
                "reassign": "Try again with a different worker.",
                "human": "Stop and ask a human.",
            },
        ),
        (
            "D6",
            "action",
            D6_ACTION,
            {
                "ship": "The integrated result is ready to hand over.",
                "fix_task": "Create a fix task for the failed tests or quality gate.",
                "human": "Stop and ask a human.",
            },
        ),
        (
            "D7",
            "learn",
            D7_LEARN,
            {
                "learn": "Distill lessons from this run now (one extra model call).",
                "skip": "Nothing worth learning; save the call.",
            },
        ),
    ],
)
def test_every_decision_answer_has_a_description(
    point: DecisionPoint,
    question: str,
    allowed: list[str],
    expected: dict[str, str],
) -> None:
    """Ensure every request-contract answer keeps its specific description."""
    body = JevDispatcher().build_payload(point, {}, {question: allowed})
    criteria = body["questions"][question]["criteria"]
    assert list(criteria) == allowed
    assert criteria == expected


@pytest.mark.parametrize(
    "confidence", [float("nan"), float("inf"), float("-inf"), -0.01, 1.01, 10**309]
)
def test_confidence_must_be_finite_and_in_range(confidence: float) -> None:
    """Reject NaN, infinities, and values outside the inclusive confidence range."""
    body = _fixture("d4_openrouter_success.json")
    body["answers"]["worker"]["confidence"] = confidence
    with pytest.raises(DispatchError, match="invalid confidence"):
        parse_answers(body, _d4().questions)


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_confidence_endpoints_are_accepted(confidence: float) -> None:
    """Preserve both inclusive endpoints of the accepted confidence range."""
    body = _fixture("d4_openrouter_success.json")
    body["answers"]["worker"]["confidence"] = confidence
    _, details = parse_answers(body, _d4().questions)
    assert details["worker"]["confidence"] == confidence


async def test_instruction_overrides_and_generic_fallback() -> None:
    settings = JevSettings(instructions={"D4.effort": "custom effort", "worker": "custom worker"})
    rec = Recorder(_ok("d4_openrouter_success.json"))
    jev, _ = _dispatcher(rec, settings)
    await jev.decide(_d4())
    questions = rec.payload()["questions"]
    assert questions["effort"]["instructions"] == "custom effort"
    # The pairing rule is a fact about the allowed answers, appended even to an override.
    assert questions["worker"]["instructions"] == f"custom worker {PAIRING_INSTRUCTION}"
    assert jev.instruction_for("D2", "mystery") == GENERIC_INSTRUCTION.format(name="mystery")


async def test_parses_answers_model_and_cost_from_usage() -> None:
    rec = Recorder(_ok("d4_openrouter_success.json"))
    jev, _ = _dispatcher(rec)
    decision = await jev.decide(_d4())
    assert decision.dispatcher == "jev"
    assert decision.point == "D4"
    assert decision.answers == {
        "complexity": "standard",
        "worker": "claude_code",
        "model": "anthropic/claude-sonnet-4.6",
        "effort": "medium",
    }
    assert decision.model == "typesafe/jev-1.13-20260917"
    assert decision.cost_usd == pytest.approx(0.0000257)
    assert decision.latency_ms >= 0
    assert decision.invalid_answers(_d4()) == []
    answers = decision.details["answers"]
    assert answers["worker"]["confidence"] == pytest.approx(0.72)
    assert answers["effort"]["probabilities"]["medium"] == pytest.approx(0.7)
    assert decision.details["usage"]["cost"] == pytest.approx(0.0000257)
    assert not hasattr(jev, "last_details")


async def test_native_settings_and_cost_estimated_from_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    rec = Recorder(_ok("d3_native_success.json"))
    jev, _ = _dispatcher(rec, JevSettings.for_native())
    req = DecisionRequest(
        point="D3",
        state={"validation": {"verdict": "revise", "issues": 2}},
        questions={"verdict": ["approve", "revise", "human"]},
    )
    decision = await jev.decide(req)
    assert str(rec.requests[0].url) == "https://api.typesafe.ai/v1/systemone"
    assert rec.payload()["model"] == "jev-latest"
    assert decision.answers == {"verdict": "revise"}
    assert decision.model == "jev-1.13.0"
    assert decision.cost_usd == pytest.approx(1000 * PRICE_PER_INPUT_TOKEN_USD)


async def test_single_option_questions_skip_http() -> None:
    rec = Recorder()
    jev, _ = _dispatcher(rec)
    req = DecisionRequest(point="D2", state={}, questions={"worker": ["codex"], "effort": ["low"]})
    decision = await jev.decide(req)
    assert rec.requests == []
    assert decision.answers == {"worker": "codex", "effort": "low"}
    assert decision.cost_usd == 0.0
    assert decision.details == {"direct": ["effort", "worker"], "answers": {}, "usage": None}


async def test_single_option_questions_are_not_sent() -> None:
    rec = Recorder(_ok("d4_openrouter_success.json"))
    jev, _ = _dispatcher(rec)
    req = _d4()
    req.questions["worker"] = ["claude_code"]
    decision = await jev.decide(req)
    assert "worker" not in rec.payload()["questions"]
    assert decision.answers["worker"] == "claude_code"
    assert decision.details["direct"] == ["worker"]
    assert "worker" not in decision.details["answers"]


async def test_single_option_shortcut_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    jev, _ = _dispatcher(Recorder())
    req = DecisionRequest(point="D1", state={}, questions={"clear": ["yes"]})
    assert (await jev.decide(req)).answers == {"clear": "yes"}


async def test_invalid_choice_raises() -> None:
    jev, _ = _dispatcher(Recorder(_ok("d4_invalid_choice.json")))
    with pytest.raises(DispatchError, match="worker"):
        await jev.decide(_d4())


async def test_missing_answer_raises() -> None:
    body = _fixture("d4_openrouter_success.json")
    del body["answers"]["effort"]
    jev, _ = _dispatcher(Recorder(httpx.Response(200, json=body)))
    with pytest.raises(DispatchError, match="no answer for effort"):
        await jev.decide(_d4())


async def test_low_confidence_raises_with_the_confidences() -> None:
    jev, _ = _dispatcher(Recorder(_ok("d4_low_confidence.json")))
    with pytest.raises(DispatchError, match=r"low confidence on worker \(0.50\), effort \(0.41\)"):
        await jev.decide(_d4())


async def test_min_confidence_is_configurable() -> None:
    jev, _ = _dispatcher(Recorder(_ok("d4_low_confidence.json")), JevSettings(min_confidence=0.4))
    assert (await jev.decide(_d4())).answers["effort"] == "high"


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors(status: int) -> None:
    rec = Recorder(httpx.Response(status, json={"error": {"message": f"bad key {KEY}"}}))
    jev, sleeps = _dispatcher(rec)
    with pytest.raises(DispatchError, match="auth") as info:
        await jev.decide(_d4())
    assert KEY not in str(info.value)
    assert len(rec.requests) == 1
    assert sleeps == []


async def test_422_reports_server_message() -> None:
    jev, _ = _dispatcher(Recorder(httpx.Response(422, json=_fixture("error_422.json"))))
    with pytest.raises(DispatchError, match=r"422.*must contain at least 2 options"):
        await jev.decide(_d4())


async def test_error_detail_is_truncated_and_redacted() -> None:
    long = f"{KEY} " + "x" * 2000
    jev, _ = _dispatcher(Recorder(httpx.Response(400, text=long)))
    with pytest.raises(DispatchError) as info:
        await jev.decide(_d4())
    message = str(info.value)
    assert KEY not in message
    assert len(message) < 400


async def test_429_retries_once_then_succeeds() -> None:
    rec = Recorder(
        httpx.Response(429, json=_fixture("error_429.json"), headers={"retry-after": "2"}),
        _ok("d4_openrouter_success.json"),
    )
    jev, sleeps = _dispatcher(rec)
    decision = await jev.decide(_d4())
    assert decision.answers["worker"] == "claude_code"
    assert len(rec.requests) == 2
    assert sleeps == [2.0]


@pytest.mark.parametrize("status", [529, 503])
async def test_overload_fails_after_one_retry(status: int) -> None:
    rec = Recorder(httpx.Response(status, text="busy"), httpx.Response(status, text="busy"))
    jev, sleeps = _dispatcher(rec)
    with pytest.raises(DispatchError, match=f"HTTP {status}"):
        await jev.decide(_d4())
    assert len(rec.requests) == 2
    assert sleeps == [1.0]


async def test_timeout_raises() -> None:
    jev, _ = _dispatcher(Recorder(httpx.ReadTimeout("slow")))
    with pytest.raises(DispatchError, match="timed out"):
        await jev.decide(_d4())


async def test_network_error_raises() -> None:
    # A connect error is retried once (the request was never sent); the second one raises.
    rec = Recorder(httpx.ConnectError("refused"), httpx.ConnectError("refused"))
    jev, sleeps = _dispatcher(rec)
    with pytest.raises(DispatchError, match="network error"):
        await jev.decide(_d4())
    assert len(rec.requests) == 2
    assert sleeps == [1.0]


async def test_post_with_retry_retries_once_then_succeeds() -> None:
    # The retry loop itself (issue #242): a retryable response, then a 200, returns the body.
    rec = Recorder(
        httpx.Response(429, json=_fixture("error_429.json"), headers={"retry-after": "2"}),
        _ok("d4_openrouter_success.json"),
    )
    jev, sleeps = _dispatcher(rec)
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec))
    body = await jev._post_with_retry(client, "https://example.invalid/systemone", {}, {}, KEY)
    assert body == _fixture("d4_openrouter_success.json")
    assert len(rec.requests) == 2
    assert sleeps == [2.0]


async def test_retry_loop_fallthrough_raises_dispatch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the retry loop ever exhausts without a body or a raise (issue #242), the
    # fall-through must be a structured DispatchError, never an AssertionError.
    monkeypatch.setattr("jev_agent.dispatch.jev._response_body", lambda *_: None)
    rec = Recorder(_ok("d4_openrouter_success.json"), _ok("d4_openrouter_success.json"))
    jev, sleeps = _dispatcher(rec)
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec))
    with pytest.raises(DispatchError, match="failed after retries") as info:
        await jev._post_with_retry(client, "https://example.invalid/systemone", {}, {}, KEY)
    assert info.value.provider_failure is True
    assert len(rec.requests) == 2
    assert sleeps == [1.0, 1.0]


async def test_non_json_response_raises() -> None:
    jev, _ = _dispatcher(Recorder(httpx.Response(200, text="<html>")))
    with pytest.raises(DispatchError, match="not JSON"):
        await jev.decide(_d4())


async def test_missing_key_names_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    rec = Recorder()
    jev, _ = _dispatcher(rec)
    with pytest.raises(DispatchError, match="OPENROUTER_API_KEY"):
        await jev.decide(_d4())
    assert rec.requests == []


async def test_oversize_state_is_not_sent() -> None:
    rec = Recorder()
    jev, _ = _dispatcher(rec)
    req = _d4()
    req.state["notes"] = "x" * 100_000
    with pytest.raises(DispatchError, match="state too large"):
        await jev.decide(req)
    assert rec.requests == []


async def test_key_never_in_any_error_message() -> None:
    cases: list[list[httpx.Response | Exception]] = [
        [httpx.Response(401, text=KEY)],
        [httpx.Response(422, json={"error": {"message": f"echo {KEY}"}})],
        [httpx.Response(500, text=KEY), httpx.Response(500, text=KEY)],
        [httpx.ReadTimeout(KEY)],
        [httpx.ConnectError(KEY), httpx.ConnectError(KEY)],
    ]
    for responses in cases:
        rec = Recorder(*responses)
        jev, _ = _dispatcher(rec)
        with pytest.raises(DispatchError) as info:
            await jev.decide(_d4())
        assert KEY not in str(info.value)


def test_settings_come_from_dispatcher_config() -> None:
    config = JevConfig.model_validate(
        {"dispatcher": {"jev": {"model": "typesafe/jev-1.13", "min_confidence": 0.7}}}
    )
    settings = config.dispatcher.jev
    assert isinstance(settings, JevSettings)
    assert JevDispatcher(settings).settings.model == "typesafe/jev-1.13"
    assert settings.min_confidence == 0.7
    assert settings.base_url == "https://openrouter.ai/api/v1"


# ---- runnable (worker, model) pairs (issue #33) -------------------------------------------------


@pytest.mark.parametrize(
    ("models", "pair"),
    [
        # Jev answers worker and model separately: claude_code with an OpenRouter slug.
        (MODELS, ("claude_code", None)),
        (["sonnet", *MODELS], ("claude_code", "sonnet")),
    ],
)
async def test_mismatched_answers_become_a_runnable_pair(
    models: list[str], pair: tuple[str, str | None]
) -> None:
    rec = Recorder(_ok("d4_openrouter_success.json"))
    jev, _ = _dispatcher(rec)
    decision = await jev.decide(_d4())
    assert (decision.answers["worker"], decision.answers["model"]) == (
        "claude_code",
        "anthropic/claude-sonnet-4.6",
    )
    options = RoleOptions("coder", ["claude_code", "codex", "api"], models, ["medium"])
    fitted = fit_free(Assignment.from_answers(decision.answers), options)
    assert (fitted.worker, fitted.model) == pair
    assert model_fits(fitted.worker, fitted.model)


async def test_api_answer_with_a_cli_alias_becomes_a_runnable_pair() -> None:
    options = RoleOptions("planner", ["claude_code", "api"], ["sonnet", "openai/gpt-5"], ["low"])
    answers = {"worker": "api", "model": "sonnet", "effort": "low"}
    fitted = fit_free(Assignment.from_answers(answers), options)
    assert (fitted.worker, fitted.model) == ("api", "openai/gpt-5")


def test_payload_states_the_pairing_rule_only_when_pairs_can_mismatch() -> None:
    body = JevDispatcher().build_payload("D4", _d4().state, _d4().questions)
    assert PAIRING_INSTRUCTION in body["questions"]["worker"]["instructions"]
    assert PAIRING_INSTRUCTION in body["questions"]["model"]["instructions"]
    questions = {**_d4().questions, "worker": ["api"]}
    body = JevDispatcher().build_payload("D4", _d4().state, questions)
    assert PAIRING_INSTRUCTION not in body["questions"]["worker"]["instructions"]
    assert PAIRING_INSTRUCTION not in body["questions"]["model"]["instructions"]


def test_d5_instruction_separates_error_from_failure() -> None:
    text = JevDispatcher().instruction_for("D5", "action")
    assert "`error`" in text and "reassign" in text
