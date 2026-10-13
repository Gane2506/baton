"""Jev dispatcher: answers decision points with TypeSafe's Jev via the System One API.

See docs/verification.md section 1. The request is `POST {base_url}/systemone` with
`{state, model, questions}`; OpenRouter (`https://openrouter.ai/api/v1`) and native TypeSafe
(`https://api.typesafe.ai/v1`) accept the same shape. Every question is sent as a `choice`
whose `criteria` keys are exactly the allowed answers.

This module never falls back by itself: any problem (network, HTTP error, bad or
low-confidence answer, oversized state) raises `DispatchError`, and the harness's guard falls
back to the rules dispatcher. The API key is read from the environment on each call and never
appears in messages or `Decision.details`. Settings come from `[dispatcher.jev]`
(`jev_agent.config.schema.JevSettings`).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from jev_agent.config.schema import NATIVE_JEV_BASE_URL, OPENROUTER_BASE_URL, JevSettings
from jev_agent.core.models import Decision, DecisionPoint, DecisionRequest
from jev_agent.dispatch.base import DispatchError
from jev_agent.dispatch.requests import model_fits

__all__ = ["NATIVE_BASE_URL", "OPENROUTER_BASE_URL", "JevDispatcher", "JevSettings"]

NATIVE_BASE_URL = NATIVE_JEV_BASE_URL

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
}

# Instructions per question name; (point, question) entries override the generic ones.
QUESTION_INSTRUCTIONS: dict[str, str] = {
    "worker": (
        "Which worker should do this work? Prefer workers whose quota is ok and avoid ones "
        "that already failed this work in `prior_attempts`."
    ),
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
        "Which coding worker should implement this task? Avoid workers whose quota is low."
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
}

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
OPTION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "effort": {
        "low": "Quick, mechanical work; little reasoning needed.",
        "medium": "Ordinary work with some design decisions.",
        "high": "Hard or subtle work, or work that already failed.",
        "xhigh": "Very hard work; spend much more reasoning.",
        "max": "Hardest work only; maximum reasoning regardless of cost.",
    },
    "complexity": {
        "trivial": "A small, local change such as a rename, config tweak or one-line fix.",
        "standard": "A typical feature or fix touching a few files.",
        "hard": "Cross-cutting or subtle work: many files, concurrency, security, migrations.",
    },
    "worker": {
        "claude_code": "Claude Code CLI agent; edits files and runs commands in a worktree.",
        "codex": "OpenAI Codex CLI agent; edits files and runs commands in a worktree.",
        "api": "Plain API model call; no file access, good for planning and reviewing text.",
    },
    "clear": {
        "yes": "The idea is clear enough to plan.",
        "no": "The idea is too vague; ask the human first.",
    },
    "verdict": {
        "approve": "The plan is good enough to execute.",
        "revise": "Send the plan back to the planner with the validator's issues.",
        "human": "A human should decide.",
        "escalate": "A human should decide.",
    },
    "learn": {
        "learn": "Distill lessons from this run now (one extra model call).",
        "skip": "Nothing worth learning; save the call.",
    },
    "action": {
        "done": "The task is complete and passed its checks.",
        "retry": "Try again with the same worker, model and effort.",
        "escalate": "Try again with a stronger model or more effort.",
        "reassign": "Try again with a different worker.",
        "human": "Stop and ask a human.",
        "ship": "The integrated result is ready to hand over.",
        "fix": "Create a fix task for the failures.",
        "fix_task": "Create a fix task for the failures.",
    },
}


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
    ) -> None:
        self.settings = settings or JevSettings()
        self._client = client
        self._sleep = sleep

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

        key = os.environ.get(self.settings.api_key_env, "").strip()
        if not key:
            raise DispatchError(f"jev: API key env var {self.settings.api_key_env} is not set")
        payload = self.build_payload(req.point, req.state, asked)
        check_size(payload)
        body = await self._post(payload, key)
        answers, details = parse_answers(body, asked)
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else None

        low = [
            f"{q} ({d['confidence']:.2f})"
            for q, d in details.items()
            if d["confidence"] < self.settings.min_confidence
        ]
        if low:
            raise DispatchError(f"jev: low confidence on {', '.join(low)}")

        model = body.get("model")
        return Decision(
            point=req.point,
            answers={**direct, **answers},
            dispatcher=self.name,
            model=model if isinstance(model, str) else self.settings.model,
            cost_usd=cost_from_usage(usage),
            latency_ms=int((time.monotonic() - started) * 1000),
            details={"direct": sorted(direct), "answers": details, "usage": usage},
        )

    def build_payload(
        self, point: DecisionPoint, state: dict[str, Any], questions: dict[str, list[str]]
    ) -> dict[str, Any]:
        context = {"id": point, "description": POINT_DESCRIPTIONS[point]}
        full_state = {"decision_point": context}
        full_state.update({k: v for k, v in state.items() if k != "decision_point"})
        free_info = state.get("models") if isinstance(state.get("models"), dict) else {}
        assert isinstance(free_info, dict)
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

        def describe(name: str, option: str) -> str | None:
            if name == "model" and option in free:
                return FREE_UNKNOWN_OPTION if option in unknown else FREE_OPTION
            return OPTION_DESCRIPTIONS.get(name, {}).get(option)

        def instruction(name: str) -> str:
            text = self.instruction_for(point, name)
            if prefer and name == "model":
                text = f"{text} {PREFER_FREE_INSTRUCTION}"
            elif free and name == "worker":
                text = f"{text} {FREE_WORKER_INSTRUCTION}"
            if mismatch and name in ("worker", "model"):
                text = f"{text} {PAIRING_INSTRUCTION}"
            return text

        return {
            "state": full_state,
            "model": self.settings.model,
            "questions": {
                name: {
                    "type": "choice",
                    "instructions": instruction(name),
                    "criteria": {option: describe(name, option) for option in allowed},
                }
                for name, allowed in questions.items()
            },
        }

    def instruction_for(self, point: DecisionPoint, question: str) -> str:
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

    async def _post(self, payload: dict[str, Any], key: str) -> dict[str, Any]:
        url = self.settings.base_url.rstrip("/") + "/systemone"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if self._client is not None:
            return await self._post_with_retry(self._client, url, payload, headers, key)
        async with httpx.AsyncClient(timeout=self.settings.timeout_s) as client:
            return await self._post_with_retry(client, url, payload, headers, key)

    async def _post_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        key: str,
    ) -> dict[str, Any]:
        for attempt in (1, 2):
            try:
                resp = await client.post(
                    url, json=payload, headers=headers, timeout=self.settings.timeout_s
                )
            except httpx.TimeoutException as exc:
                raise DispatchError(
                    f"jev: request timed out after {self.settings.timeout_s:g}s"
                ) from exc
            except httpx.HTTPError as exc:
                raise DispatchError(f"jev: network error ({type(exc).__name__})") from exc

            status = resp.status_code
            if status == 200:
                try:
                    body = resp.json()
                except ValueError as exc:
                    raise DispatchError("jev: response is not JSON") from exc
                if not isinstance(body, dict):
                    raise DispatchError("jev: response is not a JSON object")
                return body
            retryable = status in RETRY_STATUSES or status >= 500
            if retryable and attempt == 1:
                await self._sleep(retry_delay(resp))
                continue
            raise DispatchError(http_error_message(resp, self.settings.api_key_env, key))
        raise AssertionError("unreachable")  # pragma: no cover


def check_size(payload: dict[str, Any]) -> None:
    """Raise if `state` plus the longest question likely exceeds Jev's 32k-token budget."""
    state_chars = len(json.dumps(payload["state"], ensure_ascii=False))
    longest_question = max(
        len(json.dumps(q, ensure_ascii=False)) for q in payload["questions"].values()
    )
    estimated = (state_chars + longest_question) // CHARS_PER_TOKEN
    if estimated > MAX_PROMPT_TOKENS:
        raise DispatchError(
            f"jev: state too large (~{estimated} tokens estimated, limit {MAX_PROMPT_TOKENS})"
        )


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
        answer = raw.get(name)
        if not isinstance(answer, dict) or "choice" not in answer:
            raise DispatchError(f"jev: no answer for {name}")
        choice = answer["choice"]
        if not isinstance(choice, str) or choice not in allowed:
            raise DispatchError(f"jev: answer for {name} ({str(choice)[:50]!r}) is not allowed")
        confidence = answer.get("confidence")
        if not isinstance(confidence, int | float) or isinstance(confidence, bool):
            raise DispatchError(f"jev: answer for {name} has no confidence")
        probabilities = answer.get("probabilities")
        answers[name] = choice
        details[name] = {
            "choice": choice,
            "confidence": float(confidence),
            "probabilities": probabilities if isinstance(probabilities, dict) else {},
        }
    return answers, details


def cost_from_usage(usage: dict[str, Any] | None) -> float | None:
    """`usage.cost` when present (OpenRouter), else input tokens times the list price."""
    if not usage:
        return None
    cost = usage.get("cost")
    if isinstance(cost, int | float) and not isinstance(cost, bool):
        return float(cost)
    tokens = usage.get("input_tokens")
    if isinstance(tokens, int) and not isinstance(tokens, bool):
        return tokens * PRICE_PER_INPUT_TOKEN_USD
    return None


def retry_delay(resp: httpx.Response) -> float:
    """Seconds to wait before the single retry: `Retry-After` if numeric, capped."""
    header = resp.headers.get("retry-after")
    try:
        delay = float(header) if header is not None else DEFAULT_BACKOFF_S
    except ValueError:
        delay = DEFAULT_BACKOFF_S
    return min(max(delay, 0.0), MAX_BACKOFF_S)


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
    if isinstance(body, dict):
        error = body.get("error", body.get("detail", body))
        if isinstance(error, dict) and "message" in error:
            error = error["message"]
        body = error
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    text = " ".join(text.split())
    if len(text) > _MAX_ERROR_CHARS:
        text = text[:_MAX_ERROR_CHARS] + "..."
    return text or "(empty body)"
