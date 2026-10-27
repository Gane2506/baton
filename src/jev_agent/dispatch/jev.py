"""Jev dispatcher: answers decision points with TypeSafe's Jev via the System One API.

See docs/verification.md section 1. The request is `POST {base_url}/systemone` with
`{state, model, questions}`; OpenRouter (`https://openrouter.ai/api/v1`) and native TypeSafe
(`https://api.typesafe.ai/v1`) accept the same shape. Every question is sent as a `choice`
whose `criteria` keys are exactly the allowed answers.

`JevDispatcher` never falls back by itself: any problem (network, HTTP error, bad or
low-confidence answer, oversized state) raises `DispatchError`, and the harness's guard falls
back to the rules dispatcher.

`MultiProviderJevDispatcher` removes the single-provider dependency: every request tries
the native TypeSafe API first and, only when that provider fails (unreachable, timed out,
rate-limited, auth or key problems), sends the same payload to Jev through OpenRouter.
Request problems that would fail on both providers (oversized state, a rejected payload)
raise at once. `Decision.details["provider"]` records which provider answered (`"native"`
or `"openrouter"`), so the run log shows who served each decision. After both providers
fail, the harness's guard still applies: the rules dispatcher decides instead.

With `[dispatcher.cost] prefer_cheaper_leg` (the default), the cheaper leg goes first
when both are reachable; cost changes only the order, never the availability — both legs
are still tried, and `Decision.details["routing_reason"]` records why.

The API key is read from the environment on each call and never
appears in messages or `Decision.details`. Settings come from `[dispatcher.jev]`
(`jev_agent.config.schema.JevSettings`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from jev_agent.config.schema import (
    NATIVE_JEV_BASE_URL,
    OPENROUTER_BASE_URL,
    CostSettings,
    JevSettings,
)
from jev_agent.core.models import Decision, DecisionPoint, DecisionRequest
from jev_agent.dispatch.base import DispatchError
from jev_agent.dispatch.requests import (
    D1_CLEAR,
    D3_VERDICT,
    D4_COMPLEXITY,
    D5_ACTION,
    D6_ACTION,
    D7_LEARN,
    model_fits,
)

__all__ = [
    "NATIVE_BASE_URL",
    "NATIVE_PROVIDER",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_PROVIDER",
    "JevDispatcher",
    "JevSettings",
    "MultiProviderJevDispatcher",
]

NATIVE_BASE_URL = NATIVE_JEV_BASE_URL
NATIVE_PROVIDER = "native"
OPENROUTER_PROVIDER = "openrouter"

# Key under which `post` reports which provider answered, so `decide` can record it
# in `Decision.details["provider"]`.
# It travels on a copy of the response body, never on shared state, so concurrent runs
# sharing one dispatcher cannot mix it up.
PROVIDER_KEY = "jev_provider"
# Key under which `post` reports why the legs ran in this order, when cost changed
# the default native-first order. Recorded in `Decision.details["routing_reason"]`.
ROUTING_REASON_KEY = "jev_routing_reason"

# Input price for Jev 1.13 on both OpenRouter and TypeSafe; output tokens are free. Used only
# when the response carries no `usage.cost` (native TypeSafe never does).
PRICE_PER_INPUT_TOKEN_USD = 0.042 / 1_000_000

# Jev's limit is 32k tokens for `state` plus the longest question. We estimate tokens as
# chars / 3, which over-counts for typical JSON, so the guard errs on the side of not sending.
MAX_PROMPT_TOKENS = 32_000
CHARS_PER_TOKEN = 3

RETRY_STATUSES = frozenset({429, 529})  # plus every 5xx
DEFAULT_BACKOFF_S = 1.0
MAX_BACKOFF_S = 10.0
_MAX_ERROR_CHARS = 300

Sleep = Callable[[float], Awaitable[None]]

log = logging.getLogger(__name__)


# One line per decision point, sent in `state.decision_point` so Jev knows where it is.
POINT_DESCRIPTIONS: dict[DecisionPoint, str] = {
    "D1": "Intake: is the idea clear enough to plan, and which planner worker, model and effort.",
    "D2": "A plan exists: pick the validator worker, model and effort to review it.",
    "D3": "The plan was validated: approve it, send it back for revision, or ask a human.",
    "D4": "A task is ready: pick its complexity tier and the coder worker, model and effort.",
    "D5": (
        "A task attempt finished tests/review (and the quality gate, if on): done, retry, "
        "escalate, reassign, or ask a human."
    ),
    "D6": (
        "All tasks were integrated and tested (and quality-gated, if on): ship, add a fix task, "
        "or ask a human."
    ),
    "D7": (
        "The run ended: distill lessons for future runs now (one extra model call), or skip; "
        "and which learner worker, model and effort."
    ),
    "D8": (
        "A task attempt passed its tests (and the quality gate, if on): pick the reviewer "
        "worker, model and effort to check its diff before the task can be done."
    ),
}

# Instructions per question name; (point, question) entries override the generic ones.
QUESTION_INSTRUCTIONS: dict[str, str] = {
    "worker": "Which worker should do this work? Choose a worker whose capabilities fit the role.",
    "model": (
        "Which model should the worker use? Match model strength to the difficulty of the "
        "work, prefer cheaper models for simple work, and stay well inside the remaining budget."
    ),
    "effort": (
        "How much reasoning effort should the worker spend? Low for mechanical changes, "
        "high only for hard work or work that already failed."
    ),
    "complexity": (
        "How complex is this task, judging by its description, files touched and acceptance "
        "criteria?"
    ),
    "clear": "Is the idea clear and specific enough to plan without asking the human first?",
    "verdict": "Given the validation result, what should happen to the plan?",
    "action": "Given the outcome so far, what should happen next?",
    "learn": "Is there something worth learning from this run for future runs?",
}

POINT_QUESTION_INSTRUCTIONS: dict[tuple[DecisionPoint, str], str] = {
    ("D1", "worker"): "Which worker should write the plan for this idea?",
    ("D1", "model"): "Which model should the planner use? Bigger, vaguer ideas need stronger.",
    ("D1", "effort"): "How much reasoning effort should the planner spend on this idea?",
    ("D2", "worker"): (
        "Which worker should validate the plan? Prefer a different vendor than the planner, "
        "so the review is independent."
    ),
    ("D2", "model"): "Which model should validate the plan? Prefer a different vendor's model.",
    ("D2", "effort"): "How much reasoning effort should the validator spend on this plan?",
    ("D3", "verdict"): (
        "Given the validator's verdict and issues, approve the plan, revise it, or escalate to "
        "a human. Escalate when rounds are nearly used up or issues need human judgement."
    ),
    ("D4", "worker"): (
        "Which coding worker should implement this task? "
        "Choose a worker whose capabilities fit the task."
    ),
    ("D5", "action"): (
        "Given the attempt's test and review results, what next? Retry for flaky or small "
        "failures, escalate after repeated failures, reassign if this worker seems stuck, and "
        "ask a human when attempts are nearly used up or the task is unclear. "
        "`worker_status` `error` means the worker could not run at all (CLI missing, not "
        "logged in, bad key): reassign at once (a human if `can_reassign` is false); `failure` "
        "means it ran but the work failed. "
        "`quality_failed` means the quality gate blocked the attempt (see "
        "`quality_findings`): never answer done then; retry first (the findings go to the "
        "coder), escalate if it blocks again."
    ),
    ("D6", "action"): (
        "Given the merge and test results after integration, is the result shippable, does it "
        "need a fix task, or does a human need to look? A fix task only helps when tests failed "
        "or the quality gate blocked (`quality_failed`), and `fix_rounds` is below "
        "`max_fix_rounds`; merge conflicts need a human. Never ship while `quality_failed`."
    ),
    ("D7", "learn"): (
        "Learn when the run needed extra work (any `signals`: revisions, retries, escalations, "
        "reassignments, fix tasks, failures, a human stop), or when it ended DONE while the "
        "profile has fewer than 3 lessons. Otherwise skip: a clean run teaches little."
    ),
    ("D7", "worker"): "Which worker should distill lessons from this run? It only reads.",
    ("D7", "model"): (
        "Which model should distill the lessons? A mid-sized model is usually enough; this "
        "is a short summarizing job."
    ),
    ("D7", "effort"): "How much reasoning effort should the learner spend? Usually low.",
    ("D8", "worker"): (
        "Which worker should review this attempt's diff? It only reads. Prefer a different "
        "vendor than the coder (`coder`, and `prefer_different_vendor_than` when set), so the "
        "review is independent."
    ),
    ("D8", "model"): (
        "Which model should review the diff? Match its strength to the size (`diff`) and "
        "complexity of the task; a large or subtle change needs a stronger model."
    ),
    ("D8", "effort"): (
        "How much reasoning effort should the reviewer spend? Low for a small mechanical diff, "
        "high for a large, cross-cutting or security-relevant one, or a later attempt."
    ),
}

# Appended to the worker, model and effort instructions at D4 when the code
# orchestrator's routing hint is in state (issue #203, I6). Before this, the
# hint sat in `state.code_routing` with no instruction ever naming it, so
# nothing directed Jev to use it: it was possibly inert. The hint is gated
# at the code gate's 0.7 bar before it reaches state (see
# `grades.routing_hint`), and Jev still chooses among its allowed answers --
# this tells it to prefer the hint when it fits, not to obey it blindly.
CODE_ROUTING_INSTRUCTION = (
    "The coding decision agent's routing hint is in state.code_routing "
    "(model_tier, skill, task_difficulty, suggested_effort): prefer it when "
    "it fits the allowed answers, but you still make this decision. The hint "
    "also carries the rating ledger's evidence for its tier "
    "(ledger_tier_grades, ledger_tier_mean_quality, and ledger_best_tier "
    "when another tier grades better): prefer tiers and skills with a strong "
    "ledger record when choosing among the allowed answers, and treat "
    '"no grades yet" as no evidence, not as a bad record.'
)
# Appended to the model (and worker) instructions when `state.models.prefer_free` is set.
PREFER_FREE_INSTRUCTION = (
    "Prefer free models unless the task is hard or earlier attempts failed; free models are "
    "rate-limited and run only on the api worker."
)
FREE_WORKER_INSTRUCTION = "Free models run only on the api worker: pick api to use one."
# Appended to the worker and model instructions when some allowed pair cannot run
# (`requests.model_fits`); the harness swaps such a model for the worker's first runnable one.
PAIRING_INSTRUCTION = (
    "Models named vendor/name run only on the api worker; other model names only on "
    "claude_code and codex."
)
# Criteria description for a free model, and for one whose structured-output support is unknown.
FREE_OPTION = "free (rate-limited)"
FREE_UNKNOWN_OPTION = "free (rate-limited); structured output support unknown"

GENERIC_INSTRUCTION = (
    "Choose the best answer to the question `{name}` for the decision in `decision_point`, "
    "using the rest of the state."
)

# Descriptions for well-known answers, per question. Unknown answers are sent with null.
COMMON_OPTION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "effort": {
        "low": "Quick, mechanical work; little reasoning needed.",
        "medium": "Ordinary work with some design decisions.",
        "high": "Hard or subtle work, or work that already failed.",
        "xhigh": "Very hard work; spend much more reasoning.",
        "max": "Hardest work only; maximum reasoning regardless of cost.",
    },
    "worker": {
        "claude_code": "Claude Code CLI agent; edits files and runs commands in a worktree.",
        "codex": "OpenAI Codex CLI agent; edits files and runs commands in a worktree.",
        "api": "Plain API model call; no file access, good for planning and reviewing text.",
    },
}

# Descriptions for answers that are specific to a decision point. The option lists in
# dispatch.requests are the source of truth, so each allowed answer gets a description.
POINT_OPTION_DESCRIPTIONS: dict[tuple[DecisionPoint, str], dict[str, str]] = {
    ("D1", "clear"): dict(
        zip(
            D1_CLEAR,
            (
                "Plan the idea now.",
                "Ask a human to clarify the idea before planning.",
            ),
            strict=True,
        )
    ),
    ("D3", "verdict"): dict(
        zip(
            D3_VERDICT,
            (
                "The plan is good enough to execute.",
                "Send the plan back to the planner with the validator's issues.",
                "A human should decide.",
            ),
            strict=True,
        )
    ),
    ("D4", "complexity"): dict(
        zip(
            D4_COMPLEXITY,
            (
                "A small, local change such as a rename, config tweak or one-line fix.",
                "A typical feature or fix touching a few files.",
                "Cross-cutting or subtle work: many files, concurrency, security, migrations.",
            ),
            strict=True,
        )
    ),
    ("D5", "action"): dict(
        zip(
            D5_ACTION,
            (
                "The task is complete and passed its checks.",
                "Try again with the same worker, model and effort.",
                "Try again with a stronger model or more effort.",
                "Try again with a different worker.",
                "Stop and ask a human.",
            ),
            strict=True,
        )
    ),
    ("D6", "action"): dict(
        zip(
            D6_ACTION,
            (
                "The integrated result is ready to hand over.",
                "Create a fix task for the failed tests or quality gate.",
                "Stop and ask a human.",
            ),
            strict=True,
        )
    ),
    ("D7", "learn"): dict(
        zip(
            D7_LEARN,
            (
                "Distill lessons from this run now (one extra model call).",
                "Nothing worth learning; save the call.",
            ),
            strict=True,
        )
    ),
}


def _option_description(
    point: DecisionPoint, name: str, option: str, free: set[str], unknown: set[str]
) -> str | None:
    """The description for one allowed answer, if known."""
    if name == "model" and option in free:
        return FREE_UNKNOWN_OPTION if option in unknown else FREE_OPTION
    descriptions = POINT_OPTION_DESCRIPTIONS.get(
        (point, name), COMMON_OPTION_DESCRIPTIONS.get(name, {})
    )
    return descriptions.get(option)


def _question_instruction(
    base: str,
    point: DecisionPoint,
    name: str,
    state: dict[str, Any],
    *,
    prefer: bool,
    free: set[str],
    mismatch: bool,
) -> str:
    """The prompt instructions for one decision question: the base instruction plus
    the applicable hints (free-model preference, worker/model pairing, D4 routing).
    Each hint is independent — the first two are name-disjoint, so no `elif` — and
    they append in a fixed order."""
    hints: list[str] = []
    if prefer and name == "model":
        hints.append(PREFER_FREE_INSTRUCTION)
    if free and name == "worker":
        hints.append(FREE_WORKER_INSTRUCTION)
    if mismatch and name in ("worker", "model"):
        hints.append(PAIRING_INSTRUCTION)
    if point == "D4" and "code_routing" in state and name in ("worker", "model", "effort"):
        hints.append(CODE_ROUTING_INSTRUCTION)
    return " ".join([base, *hints])


def _check_confidence(details: dict[str, dict[str, Any]], min_confidence: float) -> None:
    """Raise unless every answer meets the confidence bar."""
    low = [
        f"{q} ({d['confidence']:.2f})"
        for q, d in details.items()
        if d["confidence"] < min_confidence
    ]
    if low:
        raise DispatchError(f"jev: low confidence on {', '.join(low)}")


def _decision_details(
    direct: dict[str, str],
    details: dict[str, dict[str, Any]],
    usage: dict[str, Any] | None,
    provider: Any,
    routing_reason: Any,
) -> dict[str, Any]:
    """`Decision.details`: direct answers, per-question details and usage, plus the
    provider and routing reason when `MultiProviderJevDispatcher.post` set them."""
    decision_details: dict[str, Any] = {
        "direct": sorted(direct),
        "answers": details,
        "usage": usage,
    }
    if isinstance(provider, str):
        decision_details["provider"] = provider
    if isinstance(routing_reason, str):
        decision_details["routing_reason"] = routing_reason
    return decision_details


class JevDispatcher:
    """`Dispatcher` backed by Jev. Each `Decision.details` holds the questions answered locally
    (`direct`), per-question choice, confidence and probabilities (`answers`) and `usage`.
    """

    name = "jev"

    def __init__(
        self,
        settings: JevSettings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        price_per_input_token_usd: float | None = None,
    ) -> None:
        self.settings = settings or JevSettings()
        self._client = client
        self._sleep = sleep
        # The configured price for this leg's model ([dispatcher.cost]); the module
        # constant is only the default for callers that pass no price.
        self._price_per_input = (
            price_per_input_token_usd
            if price_per_input_token_usd is not None
            else PRICE_PER_INPUT_TOKEN_USD
        )

    def _input_price(self, provider: str | None) -> float:
        """USD per input token for the leg that answered (`provider`).

        The single-leg dispatcher charges one price for every call, so `provider` is
        intentionally unused here; `MultiProviderJevDispatcher` overrides this with
        per-leg prices from `[dispatcher.cost]`.
        """
        return self._price_per_input

    async def decide(self, req: DecisionRequest) -> Decision:
        started = time.monotonic()
        direct = {q: allowed[0] for q, allowed in req.questions.items() if len(allowed) == 1}
        asked = {q: allowed for q, allowed in req.questions.items() if len(allowed) > 1}
        if not asked:
            return Decision(
                point=req.point,
                answers=direct,
                dispatcher=self.name,
                cost_usd=0.0,
                latency_ms=0,
                details={"direct": sorted(direct), "answers": {}, "usage": None},
            )

        body = await self.post(self.build_payload(req.point, req.state, asked))
        provider = body.get(PROVIDER_KEY)
        answers, details = parse_answers(body, asked)
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else None

        _check_confidence(details, self.settings.min_confidence)

        model = body.get("model")
        return Decision(
            point=req.point,
            answers={**direct, **answers},
            dispatcher=self.name,
            model=model if isinstance(model, str) else self.settings.model,
            cost_usd=cost_from_usage(usage, self._input_price(provider)),
            latency_ms=int((time.monotonic() - started) * 1000),
            details=_decision_details(
                direct, details, usage, provider, body.get(ROUTING_REASON_KEY)
            ),
        )

    def build_payload(
        self, point: DecisionPoint, state: dict[str, Any], questions: dict[str, list[str]]
    ) -> dict[str, Any]:
        """Build a Jev request payload from state and allowed answer choices."""
        context = {"id": point, "description": POINT_DESCRIPTIONS[point]}
        full_state = {"decision_point": context}
        full_state.update({k: v for k, v in state.items() if k != "decision_point"})
        models = state.get("models")
        free_info = models if isinstance(models, dict) else {}
        free, unknown = (
            set(free_info.get("free", [])),
            set(free_info.get("structured_output_unknown", [])),
        )
        prefer = bool(free_info.get("prefer_free")) and bool(free)
        mismatch = any(
            not model_fits(worker, model)
            for worker in questions.get("worker", [])
            for model in questions.get("model", [])
        )

        return {
            "state": full_state,
            "model": self.settings.model,
            "questions": {
                name: {
                    "type": "choice",
                    "instructions": _question_instruction(
                        self.instruction_for(point, name),
                        point,
                        name,
                        state,
                        prefer=prefer,
                        free=free,
                        mismatch=mismatch,
                    ),
                    "criteria": {
                        option: _option_description(point, name, option, free, unknown)
                        for option in allowed
                    },
                }
                for name, allowed in questions.items()
            },
        }

    def instruction_for(self, point: DecisionPoint, question: str) -> str:
        """Return the most specific configured instruction for a question."""
        overrides = self.settings.instructions
        for text in (
            overrides.get(f"{point}.{question}"),
            overrides.get(question),
            POINT_QUESTION_INSTRUCTIONS.get((point, question)),
            QUESTION_INSTRUCTIONS.get(question),
        ):
            if text:
                return text
        return GENERIC_INSTRUCTION.format(name=question)

    async def post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one System One request (`{state, model, questions}`) and return the body."""
        key = os.environ.get(self.settings.api_key_env, "").strip()
        if not key:
            raise DispatchError(
                f"jev: API key env var {self.settings.api_key_env} is not set",
                provider_failure=True,
            )
        check_size(payload)
        return await self._post(payload, key)

    async def _post(self, payload: dict[str, Any], key: str) -> dict[str, Any]:
        """POST once, retrying once; `timeout_s` bounds the whole call, retry and backoff
        included (httpx's own timeout applies per phase: connect, write, read)."""
        url = self.settings.base_url.rstrip("/") + "/systemone"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with asyncio.timeout(self.settings.timeout_s):
                if self._client is not None:
                    return await self._post_with_retry(self._client, url, payload, headers, key)
                async with httpx.AsyncClient(timeout=self.settings.timeout_s) as client:
                    return await self._post_with_retry(client, url, payload, headers, key)
        except TimeoutError as exc:
            raise DispatchError(
                f"jev: request timed out after {self.settings.timeout_s:g}s",
                provider_failure=True,
            ) from exc

    async def _post_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        key: str,
    ) -> dict[str, Any]:
        """One retry in all: on 429/5xx (after `Retry-After`), or on a connect error (the
        request was never sent; after `DEFAULT_BACKOFF_S`)."""
        for attempt in (1, 2):
            resp = await _send_once(client, url, payload, headers, self.settings.timeout_s)
            if isinstance(resp, httpx.ConnectError):
                if attempt == 1:
                    await self._sleep(DEFAULT_BACKOFF_S)
                    continue
                raise DispatchError(
                    f"jev: network error ({type(resp).__name__})", provider_failure=True
                ) from resp
            body = _response_body(resp, attempt, self.settings.api_key_env, key)
            if body is None:
                await self._sleep(retry_delay(resp))
                continue
            return body
        raise DispatchError(  # pragma: no cover
            "jev: request failed after retries", provider_failure=True
        )


async def _send_once(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_s: float,
) -> httpx.Response | httpx.ConnectError:
    """One POST: the response, or the connect error (the request was never sent, so
    the caller may retry). Timeouts and other transport errors raise at once."""
    try:
        return await client.post(url, json=payload, headers=headers, timeout=timeout_s)
    except httpx.TimeoutException as exc:
        raise DispatchError(
            f"jev: request timed out after {timeout_s:g}s",
            provider_failure=True,
        ) from exc
    except httpx.ConnectError as exc:
        return exc
    except httpx.HTTPError as exc:
        raise DispatchError(
            f"jev: network error ({type(exc).__name__})", provider_failure=True
        ) from exc


def _response_body(
    resp: httpx.Response, attempt: int, key_env: str, key: str
) -> dict[str, Any] | None:
    """The parsed body for a 200, None when the caller should retry, or raise for a
    final failure (non-JSON, non-object, or a non-retryable / second-attempt status)."""
    status = resp.status_code
    if status == 200:
        return _parse_body(resp)
    retryable = status in RETRY_STATUSES or status >= 500
    if retryable and attempt == 1:
        return None
    raise DispatchError(
        http_error_message(resp, key_env, key),
        provider_failure=status in (401, 403) or retryable,
    )


def _parse_body(resp: httpx.Response) -> dict[str, Any]:
    """A 200 response's body as a JSON object, or raise when it is not one."""
    try:
        body = resp.json()
    except ValueError as exc:
        raise DispatchError("jev: response is not JSON", provider_failure=True) from exc
    if not isinstance(body, dict):
        raise DispatchError("jev: response is not a JSON object", provider_failure=True)
    return body


class MultiProviderJevDispatcher(JevDispatcher):
    """Jev with no single-provider dependency: native TypeSafe and OpenRouter back each other.

    Every `post` tries one leg first and, only when that provider fails to serve the
    request (unreachable, timed out, rate-limited, auth or key problems), sends the same
    payload to the other leg. Failures that would hit both providers identically
    (oversized state, a rejected payload) raise at once instead.

    Which leg goes first is cost-aware: with `[dispatcher.cost] prefer_cheaper_leg` (the
    default) the cheaper leg leads when both are reachable. Cost never removes a leg and
    never lowers the quality bar — the confidence gate and the guard's rules fallback
    apply whichever leg answers. Every cost-influenced ordering is info-logged with its
    reason and recorded in `Decision.details["routing_reason"]`.

    `Decision.details["provider"]` records who answered, and every
    switch is warning-logged.
    """

    def __init__(
        self,
        native: JevSettings | None = None,
        openrouter: JevSettings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        cost: CostSettings | None = None,
    ) -> None:
        native_settings = native or JevSettings.for_native()
        self._cost = cost if cost is not None else CostSettings()
        self._leg_prices = {
            NATIVE_PROVIDER: self._cost.native_price_per_million / 1_000_000,
            OPENROUTER_PROVIDER: self._cost.openrouter_price_per_million / 1_000_000,
        }
        # `settings` is the native one, so `build_payload` and the confidence gate behave
        # exactly as a native `JevDispatcher`; the OpenRouter leg rebuilds only the model.
        super().__init__(
            settings=native_settings,
            client=client,
            sleep=sleep,
            price_per_input_token_usd=self._leg_prices[NATIVE_PROVIDER],
        )
        self._native = JevDispatcher(
            native_settings,
            client=client,
            sleep=sleep,
            price_per_input_token_usd=self._leg_prices[NATIVE_PROVIDER],
        )
        self._openrouter = JevDispatcher(
            openrouter or JevSettings(),
            client=client,
            sleep=sleep,
            price_per_input_token_usd=self._leg_prices[OPENROUTER_PROVIDER],
        )

    def _input_price(self, provider: str | None) -> float:
        """USD per input token for the leg that answered (`provider`)."""
        if provider in self._leg_prices:
            return self._leg_prices[provider]
        return self._price_per_input

    def _ordered_legs(self) -> list[tuple[str, JevDispatcher]]:
        """Legs to try, in order. The cheaper keyed leg goes first when configured.

        Keyless legs sort last, so a missing key fails over exactly as before; on equal
        prices the sort is stable and native stays first. The chain itself — both legs,
        then the harness guard — never changes.
        """
        legs = [
            (NATIVE_PROVIDER, self._native, self._leg_prices[NATIVE_PROVIDER]),
            (OPENROUTER_PROVIDER, self._openrouter, self._leg_prices[OPENROUTER_PROVIDER]),
        ]
        if not self._cost.prefer_cheaper_leg:
            return [(name, dispatcher) for name, dispatcher, _ in legs]
        keyed = [(name, d, price) for name, d, price in legs if _leg_key_set(d)]
        keyless = [(name, d) for name, d, _ in legs if not _leg_key_set(d)]
        keyed.sort(key=lambda item: item[2])
        return [(name, d) for name, d, _ in keyed] + keyless

    def _routing_reason(self, ordered: list[tuple[str, JevDispatcher]]) -> str | None:
        """Why the legs run in this order, when cost changed the default native-first."""
        if not self._cost.prefer_cheaper_leg or ordered[0][0] == NATIVE_PROVIDER:
            return None
        first, second = ordered[0][0], ordered[1][0]
        cheap = self._leg_prices[first] * 1_000_000
        pricey = self._leg_prices[second] * 1_000_000
        return (
            f"cheaper leg first: {first} ${cheap:g}/1M input tokens "
            f"< {second} ${pricey:g}/1M ([dispatcher.cost])"
        )

    async def post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one System One request: the preferred leg first, the other on failure."""
        ordered = self._ordered_legs()
        reason = self._routing_reason(ordered)
        if reason is not None:
            log.info("jev: cost-aware routing: %s", reason)
        last_error: DispatchError | None = None
        for provider, dispatcher in ordered:
            # The OpenRouter leg answers as its own model; the native leg keeps the
            # payload's (native) model.
            leg_payload = (
                payload
                if provider == NATIVE_PROVIDER
                else {**payload, "model": dispatcher.settings.model}
            )
            try:
                return _tagged(await dispatcher.post(leg_payload), provider, reason)
            except DispatchError as exc:
                if not _provider_failed(exc):
                    raise
                log.warning("jev: %s leg failed (%s); trying the other leg", provider, exc)
                last_error = exc
        # Both legs failed with provider errors: the last one carries the meaning, and
        # the harness guard turns it into the rules fallback (or a human stop).
        if last_error is None:
            # Unreachable: `_ordered_legs` always returns both legs, and each leg
            # either answers or raises. A plain error (not `assert`) so a `-O`
            # run fails the same way instead of raising `None`.
            raise DispatchError("jev: no dispatcher leg produced an answer")
        raise last_error


def _tagged(
    body: dict[str, Any], provider: str, routing_reason: str | None = None
) -> dict[str, Any]:
    """A copy of the response body marked with the provider that answered it."""
    marked = dict(body)
    marked[PROVIDER_KEY] = provider
    if routing_reason is not None:
        marked[ROUTING_REASON_KEY] = routing_reason
    return marked


def _leg_key_set(dispatcher: JevDispatcher) -> bool:
    """Whether the leg's API key env var is set (no key: it fails over, as before)."""
    return bool(os.environ.get(dispatcher.settings.api_key_env, "").strip())


def _provider_failed(exc: DispatchError) -> bool:
    """Whether the provider failed in a way the other leg might survive.

    Structured, not textual (issue #203, I9): `DispatchError.provider_failure`
    is set at the raise site in this module, so rewording a message cannot
    silently disable failover, and a message that merely *mentions* a
    provider failure cannot accidentally enable it. Errors raised elsewhere
    (oversized state, a 422 rejection, bad answers) leave the flag False
    and propagate without a retry on the other leg.
    """
    return bool(getattr(exc, "provider_failure", False))


def check_size(payload: dict[str, Any]) -> None:
    """Raise if `state` plus the longest question likely exceeds Jev's 32k-token budget.

    Raises `DispatchError` (never `ValueError`) so `post()`'s public contract holds.
    """
    if not payload["questions"]:
        raise DispatchError("jev: no questions")
    state_chars = len(json.dumps(payload["state"], ensure_ascii=False))
    longest_question = max(
        len(json.dumps(q, ensure_ascii=False)) for q in payload["questions"].values()
    )
    estimated = (state_chars + longest_question) // CHARS_PER_TOKEN
    if estimated > MAX_PROMPT_TOKENS:
        raise DispatchError(
            f"jev: state too large (~{estimated} tokens estimated, limit {MAX_PROMPT_TOKENS})"
        )


def _json_number(value: Any) -> float | None:
    """A JSON number as a float, or None for bools and non-numbers."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def parse_answers(
    body: dict[str, Any], questions: dict[str, list[str]]
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Return (answers, details) or raise if any answer is missing or not allowed."""
    raw = body.get("answers")
    if not isinstance(raw, dict):
        raise DispatchError("jev: response has no answers")
    answers: dict[str, str] = {}
    details: dict[str, dict[str, Any]] = {}
    for name, allowed in questions.items():
        answers[name], details[name] = _parse_answer(name, allowed, raw)
    return answers, details


def _parse_answer(name: str, allowed: list[str], raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """One answer's choice and detail, or raise when it is missing or not allowed."""
    answer = raw.get(name)
    if not isinstance(answer, dict) or "choice" not in answer:
        raise DispatchError(f"jev: no answer for {name}")
    choice = answer["choice"]
    if not isinstance(choice, str) or choice not in allowed:
        raise DispatchError(f"jev: answer for {name} ({str(choice)[:50]!r}) is not allowed")
    confidence = _answer_confidence(name, answer)
    probabilities = answer.get("probabilities")
    return choice, {
        "choice": choice,
        "confidence": confidence,
        "probabilities": probabilities if isinstance(probabilities, dict) else {},
    }


def _answer_confidence(name: str, answer: dict[str, Any]) -> float:
    """A 0-1 confidence for one answer, or raise when it is missing or invalid."""
    confidence = answer.get("confidence")
    if not isinstance(confidence, int | float) or isinstance(confidence, bool):
        raise DispatchError(f"jev: answer for {name} has no confidence")
    try:
        value = float(confidence)
    except OverflowError:
        raise DispatchError(f"jev: answer for {name} has invalid confidence") from None
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise DispatchError(f"jev: answer for {name} has invalid confidence")
    return value


def cost_from_usage(
    usage: dict[str, Any] | None,
    price_per_input_token_usd: float = PRICE_PER_INPUT_TOKEN_USD,
) -> float | None:
    """`usage.cost` when present (OpenRouter), else input tokens times the given price."""
    if not usage:
        return None
    cost = _json_number(usage.get("cost"))
    if cost is not None:
        return cost
    tokens = usage.get("input_tokens")
    if isinstance(tokens, int) and not isinstance(tokens, bool):
        return tokens * price_per_input_token_usd
    return None


def retry_delay(resp: httpx.Response) -> float:
    """Seconds to wait before the single retry, capped at `MAX_BACKOFF_S`.

    `Retry-After` is seconds when numeric, or an HTTP-date (RFC 9110 §13.1.1) converted
    to a delay from now — a date in the past means no wait. Unparseable or missing
    headers fall back to `DEFAULT_BACKOFF_S`.
    """
    header = resp.headers.get("retry-after")
    delay = DEFAULT_BACKOFF_S
    if header is not None:
        try:
            delay = float(header)
        except ValueError:
            when = _retry_after_date(header)
            if when is not None:
                delay = (when - datetime.now(UTC)).total_seconds()
    return min(max(delay, 0.0), MAX_BACKOFF_S)


def _retry_after_date(header: str) -> datetime | None:
    """Parse an HTTP-date `Retry-After` value, or None when it is not a valid date."""
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        # A bare HTTP-date is GMT by definition (RFC 9110 §5.6.7).
        when = when.replace(tzinfo=UTC)
    return when


def http_error_message(resp: httpx.Response, key_env: str, key: str) -> str:
    status = resp.status_code
    if status in (401, 403):
        return f"jev: auth failed (HTTP {status}); check {key_env}"
    detail = _error_detail(resp)
    if key:
        detail = detail.replace(key, "***")
    if status == 422:
        return f"jev: request rejected (HTTP 422): {detail}"
    if status in RETRY_STATUSES or status >= 500:
        return f"jev: unavailable after retry (HTTP {status}): {detail}"
    return f"jev: HTTP {status}: {detail}"


def _error_detail(resp: httpx.Response) -> str:
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text
    text = _error_text(body)
    if len(text) > _MAX_ERROR_CHARS:
        text = text[:_MAX_ERROR_CHARS] + "..."
    return text or "(empty body)"


def _error_text(body: Any) -> str:
    """One line of error text from a parsed error body: the `error`/`detail` message
    when present, else the body itself, whitespace-collapsed."""
    if isinstance(body, dict):
        error = body.get("error", body.get("detail", body))
        if isinstance(error, dict) and "message" in error:
            error = error["message"]
        body = error
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return " ".join(text.split())
