"""`ApiWorker`: runs planner, validator and reviewer roles on a model behind an OpenAI-compatible
chat completions API, by default OpenRouter (PLAN.md section 6, docs/verification.md section 4).

The worker never edits files, so it refuses the coder role. It never raises for API failures:
every outcome maps to a `WorkerResult.status` via `classify_api_error` (or the timeout and
network branches in `ApiWorker.run`). The API key is read from the environment at call time and
never written to the log.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from jev_agent.config.schema import WORKER_EFFORTS
from jev_agent.core.models import WorkerResult, WorkerStatus
from jev_agent.runlog.redact import REDACTED, redact
from jev_agent.workers.base import WorkerJob, WorkerOutput
from jev_agent.workers.openrouter_models import is_free_slug
from jev_agent.workers.structured import (
    parse_json_content,
    strict_json_schema,
    structured_summary,
)
from jev_agent.workers.tools import with_redact_names

PROJECT_TITLE = "Baton Agent Orchestrator"
PROJECT_URL = "https://github.com/Gane2506/baton"

EFFORTS = WORKER_EFFORTS["api"]  # other levels are ignored (with a note)
RETRY_STATUSES = frozenset({429, 503})
MAX_RETRY_AFTER_S = 10.0
DEFAULT_BACKOFF_S = 2.0
SUMMARY_CHARS = 500
# Error bodies are logged for debugging, but a provider may return megabytes of
# HTML: cap what the log keeps (issue #204).
ERROR_BODY_LOG_CHARS = 2000


def _capped_body(text: str) -> str:
    """`text` clipped to what the log keeps, with a marker when anything was cut."""
    if len(text) <= ERROR_BODY_LOG_CHARS:
        return text
    cut = f"\n[error body truncated: kept {ERROR_BODY_LOG_CHARS} chars]"
    return text[:ERROR_BODY_LOG_CHARS] + cut


# Words in a 429 body that mean "you are out of something" rather than "slow down".
_QUOTA_WORDS = re.compile(
    r"quota|credit|insufficient|exhaust|spend limit|budget|key limit|per[- ]day", re.I
)
# 403 bodies with one of these `error.metadata.limit_source` values are budget blocks.
_BUDGET_LIMIT_SOURCES = frozenset(
    {"openrouter_guardrail_budget", "openrouter_workspace_budget", "openrouter_key_limit"}
)

Sleep = Callable[[float], Awaitable[None]]


class _RequestFailed(Exception):
    """Internal: the request ended without a usable completion."""

    def __init__(self, status: WorkerStatus, summary: str) -> None:
        super().__init__(summary)
        self.status = status
        self.summary = summary


def classify_api_error(status_code: int, body: str, *, free_model: bool = False) -> WorkerStatus:
    """Map an HTTP error response to a worker status.

    - 402 (insufficient credits) -> quota_exhausted.
    - 429 on a free model (`free_model`) -> quota_exhausted: OpenRouter's free-model limits
      (requests per minute and per day) answer a bare "Rate limit exceeded" with
      `metadata.error_type = "rate_limit_exceeded"` and no `limit_source`
      (docs/verification.md section 7), and the fix is another model, not a wait.
    - Other 429 -> quota_exhausted if the body has a `limit_source` or talks about quota,
      credits, a spend/key limit or a per-day limit; otherwise a plain rate limit -> failure
      (transient; `ApiWorker` has already retried once).
    - 403 with a budget `limit_source` -> quota_exhausted; any other 401/403 -> error.
    - 408 -> timeout. 5xx -> failure. Anything else (400, 404, ...) -> error.
    """
    if status_code == 402:
        return "quota_exhausted"
    if status_code == 429:
        if free_model:
            return "quota_exhausted"
        exhausted = _limit_source(body) is not None or _QUOTA_WORDS.search(body) is not None
        return "quota_exhausted" if exhausted else "failure"
    if status_code == 403 and _limit_source(body) in _BUDGET_LIMIT_SOURCES:
        return "quota_exhausted"
    if status_code == 408:
        return "timeout"
    if status_code >= 500:
        return "failure"
    return "error"


class ApiWorker:
    """A worker that calls `POST {base_url}/chat/completions` (OpenAI-compatible)."""

    name = "api"

    def __init__(
        self,
        base_url: str,
        api_key_env: str,
        default_model: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        free_models: Collection[str] = (),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Free model ids from the model catalog, beyond the `:free` slugs known by name.
        self.free_models = frozenset(free_models)
        self.api_key_env = api_key_env
        self.default_model = default_model
        self._client = client
        self._transport = transport
        self._sleep = sleep

    def is_free(self, model: Any) -> bool:
        return isinstance(model, str) and (model in self.free_models or is_free_slug(model))

    def build_request_body(self, job: WorkerJob, model: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": job.prompt}],
        }
        if job.effort in EFFORTS:
            body["reasoning"] = {"effort": job.effort}
        if job.output_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": job.output_schema.__name__,
                    "strict": True,
                    "schema": strict_json_schema(job.output_schema),
                },
            }
            body["provider"] = {"require_parameters": True}
        return body

    async def run(self, job: WorkerJob) -> WorkerOutput:
        # The log redacts against `with_redact_names`, like the CLI workers, so benign-named
        # MCP secret values are covered when a provider echoes them in an error body.
        log = _Log(job.log_path, with_redact_names(dict(os.environ), job.tools))
        model = job.model or self.default_model
        effort = f"{job.effort}"
        if job.effort is not None and job.effort not in EFFORTS:
            effort += " (not supported; ignored)"
        log.add(
            "request",
            f"worker: {self.name}\nrole: {job.role}\nmodel: {model}\neffort: {effort}\n"
            f"schema: {job.output_schema.__name__ if job.output_schema else None}\n"
            f"url: {self.base_url}/chat/completions\ntimeout_s: {job.timeout_s}",
        )
        try:
            if job.role == "coder":
                raise _RequestFailed(
                    "error", "the api worker cannot edit files, so it cannot run the coder role"
                )
            if not model:
                raise _RequestFailed("error", "no model given for the api worker")
            api_key = os.environ.get(self.api_key_env, "")
            if not api_key:
                raise _RequestFailed(
                    "error", f"API key environment variable {self.api_key_env} is not set"
                )
            log.secrets.append(api_key)
            body = self.build_request_body(job, model)
            log.add("prompt", job.prompt)
            data = await self._post_with_retry(job, body, api_key, log)
            output = self._finish(job, data, log)
            output.result.summary = log.scrub(output.result.summary)
            return output
        except _RequestFailed as exc:
            log.add("result", f"status: {exc.status}\nsummary: {exc.summary}")
            return WorkerOutput(result=self._result(job, exc.status, log.scrub(exc.summary)))
        finally:
            # Workers never raise for agent failures (base.py): a log file that
            # cannot be written (read-only log dir, disk full) must not turn a
            # finished job into a crash (issue #204).
            with contextlib.suppress(OSError):
                log.write()

    async def _post_with_retry(
        self, job: WorkerJob, body: dict[str, Any], api_key: str, log: _Log
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": PROJECT_URL,
            "X-OpenRouter-Title": PROJECT_TITLE,
        }
        url = f"{self.base_url}/chat/completions"
        try:
            async with asyncio.timeout(job.timeout_s):
                for attempt in (1, 2):
                    response = await self._post(url, body, headers, job.timeout_s)
                    log.add(f"response (attempt {attempt})", f"HTTP {response.status_code}")
                    if response.is_success:
                        return self._json(response, log)
                    free = self.is_free(body.get("model"))
                    status = classify_api_error(
                        response.status_code, response.text, free_model=free
                    )
                    log.add("error body", _capped_body(response.text))
                    delay = _retry_delay(response)
                    retryable = response.status_code in RETRY_STATUSES and status == "failure"
                    if attempt == 1 and retryable and delay is not None:
                        log.add("retry", f"sleeping {delay:.1f}s before one retry")
                        await self._sleep(delay)
                        continue
                    summary = _error_summary(response)
                    if free and response.status_code == 429:
                        summary = f"free-model rate limit: {summary}"
                    raise _RequestFailed(status, summary)
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise _RequestFailed(
                "timeout", f"request timed out after {job.timeout_s}s ({type(exc).__name__})"
            ) from exc
        except httpx.HTTPError as exc:
            # A dropped or refused connection is transient: this attempt failed. Anything else
            # (proxy, URL scheme, redirects, our own request) is configuration: an error.
            transient = isinstance(exc, httpx.NetworkError | httpx.RemoteProtocolError)
            kind: WorkerStatus = "failure" if transient else "error"
            raise _RequestFailed(kind, f"network error: {type(exc).__name__}: {exc}") from exc
        raise _RequestFailed("error", "api request failed after retries")  # pragma: no cover

    async def _post(
        self, url: str, body: dict[str, Any], headers: dict[str, str], timeout_s: int
    ) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(url, json=body, headers=headers, timeout=timeout_s)
        async with httpx.AsyncClient(transport=self._transport, follow_redirects=True) as client:
            return await client.post(url, json=body, headers=headers, timeout=timeout_s)

    @staticmethod
    def _json(response: httpx.Response, log: _Log) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            log.add("raw response", _capped_body(response.text))
            raise _RequestFailed("error", "response body is not JSON") from exc
        if not isinstance(data, dict):
            raise _RequestFailed("error", "response body is not a JSON object")
        return data

    def _finish(self, job: WorkerJob, data: dict[str, Any], log: _Log) -> WorkerOutput:
        cost, tokens = _usage(data)
        log.add("usage", json.dumps(data.get("usage"), indent=2))
        content = _content(data)
        log.add("content", content if content is not None else "(none)")

        def fail(status: WorkerStatus, summary: str) -> WorkerOutput:
            log.add("result", f"status: {status}\nsummary: {summary}")
            return WorkerOutput(result=self._result(job, status, summary, cost, tokens))

        if "error" in data and content is None:
            return fail("error", f"API returned an error: {_message(data)}")
        if not content:
            return fail("error", "response has no message content")
        if job.output_schema is None:
            log.add("result", "status: success")
            return WorkerOutput(result=self._result(job, "success", content, cost, tokens))
        try:
            parsed = job.output_schema.model_validate(parse_json_content(content))
        except json.JSONDecodeError as exc:
            return fail("error", f"output is not valid JSON: {exc}")
        except ValidationError as exc:
            count = exc.error_count()
            return fail(
                "error", f"output does not match {job.output_schema.__name__}: {count} error(s)"
            )
        summary = structured_summary(parsed)
        log.add("result", f"status: success\nsummary: {summary}")
        return WorkerOutput(
            result=self._result(job, "success", summary, cost, tokens), parsed=parsed
        )

    @staticmethod
    def _result(
        job: WorkerJob,
        status: WorkerStatus,
        summary: str,
        cost: float | None = None,
        tokens: dict[str, int] | None = None,
    ) -> WorkerResult:
        return WorkerResult(
            status=status,
            summary=redact(summary)[:SUMMARY_CHARS],  # redact first: a cut can split a secret
            cost_usd=cost,
            tokens=tokens,
            raw_log_path=job.log_path,
        )


def _usage(data: dict[str, Any]) -> tuple[float | None, dict[str, int] | None]:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None, None
    cost = usage.get("cost")
    tokens = {
        key.removesuffix("_tokens"): value
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance(value := usage.get(key), int)
    }
    return (float(cost) if isinstance(cost, int | float) else None), (tokens or None)


def _content(data: dict[str, Any]) -> str | None:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) else None


def _message(data: dict[str, Any]) -> str:
    error = data.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return str(error["message"])
    return "unknown error"


def _error_json(body: str) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _limit_source(body: str) -> str | None:
    error = _error_json(body).get("error")
    if isinstance(error, dict) and isinstance(error.get("metadata"), dict):
        source = error["metadata"].get("limit_source")
        return source if isinstance(source, str) else None
    return None


def _error_summary(response: httpx.Response) -> str:
    message = _message(_error_json(response.text))
    if message == "unknown error":
        message = response.text[:200] or response.reason_phrase
    return f"HTTP {response.status_code}: {message}"


def _retry_delay(response: httpx.Response, *, now: datetime | None = None) -> float | None:
    """Seconds to wait before retrying, or None if `Retry-After` asks for more than 10s.

    `Retry-After` is a delay in seconds or an HTTP date (RFC 9110): a date is
    honoured the same way — a date within the 10s cap waits until then, a date in
    the past means retry now, a date further out means no retry (issue #204).
    An unparseable value also means no retry, rather than a guessed delay.
    """
    header = response.headers.get("retry-after")
    if header is None:
        return DEFAULT_BACKOFF_S
    try:
        delay = float(header)
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        delay = (when - (now or datetime.now(UTC))).total_seconds()
    return max(delay, 0.0) if delay <= MAX_RETRY_AFTER_S else None


class _Log:
    """Collects log sections and writes them, redacted, to the job's log file."""

    def __init__(self, path: Path, env: Mapping[str, str] | None = None) -> None:
        self.path = path
        self.sections: list[str] = []
        # Literal values (the API key) to scrub before writing. `redact` only knows env vars whose
        # names look secret (KEY, TOKEN, ...), and `api_key_env` may be named anything.
        self.secrets: list[str] = []
        # The environment `redact()` reads: callers pass `with_redact_names(...)` so
        # benign-named MCP secret values are covered too.
        self._env = dict(os.environ if env is None else env)

    def add(self, title: str, text: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.sections.append(f"=== {title} [{stamp}] ===\n{text.rstrip()}\n")

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.scrub("\n".join(self.sections)), encoding="utf-8")

    def scrub(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        return redact(text, self._env)
