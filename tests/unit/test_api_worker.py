import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from jev_agent.core.models import Plan, Role, ValidationResult
from jev_agent.workers.api import (
    ERROR_BODY_LOG_CHARS,
    PROJECT_TITLE,
    PROJECT_URL,
    ApiWorker,
    _retry_delay,
    classify_api_error,
)
from jev_agent.workers.base import Worker, WorkerJob
from jev_agent.workers.structured import parse_json_content, strict_json_schema

FIXTURES = Path(__file__).parent.parent / "fixtures" / "openrouter"
KEY_ENV = "JEV_TEST_OPENROUTER_KEY"
FAKE_KEY = "sk-or-v1-0123456789abcdef-fake-test-key"
BASE_URL = "https://openrouter.test/api/v1"

Handler = Callable[[httpx.Request], httpx.Response]


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def _response(status: int, fixture: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=_fixture(fixture), headers=headers)


class Recorder:
    """A MockTransport handler that replays responses in order and records requests."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def body(self, index: int = 0) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.requests[index].content)
        return data


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)


def _worker(handler: Handler, sleeps: Sleeps | None = None) -> ApiWorker:
    return ApiWorker(
        BASE_URL,
        KEY_ENV,
        default_model="openai/gpt-5-mini",
        transport=httpx.MockTransport(handler),
        sleep=sleeps or Sleeps(),
    )


def _job(tmp_path: Path, role: Role = "planner", **kwargs: Any) -> WorkerJob:
    return WorkerJob(
        role=role,
        prompt="Plan the idea: add search.",
        workdir=tmp_path,
        log_path=tmp_path / "logs" / "api.log",
        **kwargs,
    )


def test_is_a_worker() -> None:
    assert isinstance(ApiWorker(BASE_URL, KEY_ENV), Worker)
    assert ApiWorker(BASE_URL, KEY_ENV).name == "api"


# --- request body ---------------------------------------------------------------------------


async def test_request_body_with_schema_and_effort(tmp_path: Path) -> None:
    rec = Recorder(_response(200, "success_plan.json"))
    job = _job(tmp_path, model="anthropic/claude-sonnet-4.5", effort="high", output_schema=Plan)
    await _worker(rec).run(job)

    request = rec.requests[0]
    assert str(request.url) == f"{BASE_URL}/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert request.headers["HTTP-Referer"] == PROJECT_URL
    assert request.headers["X-OpenRouter-Title"] == PROJECT_TITLE
    body = rec.body()
    assert body["model"] == "anthropic/claude-sonnet-4.5"
    assert body["messages"] == [{"role": "user", "content": "Plan the idea: add search."}]
    assert body["reasoning"] == {"effort": "high"}
    assert body["provider"] == {"require_parameters": True}
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "Plan"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == strict_json_schema(Plan)


async def test_request_body_plain_uses_default_model(tmp_path: Path) -> None:
    rec = Recorder(_response(200, "success_plan.json"))
    await _worker(rec).run(_job(tmp_path, role="reviewer"))
    body = rec.body()
    assert body["model"] == "openai/gpt-5-mini"
    assert "response_format" not in body
    assert "provider" not in body
    assert "reasoning" not in body


async def test_unknown_effort_is_not_sent(tmp_path: Path) -> None:
    rec = Recorder(_response(200, "success_plan.json"))
    await _worker(rec).run(_job(tmp_path, effort="turbo"))
    assert "reasoning" not in rec.body()


def test_strict_schema_closes_every_object() -> None:
    schema = strict_json_schema(Plan)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"summary", "tasks", "risks", "needs_design"}
    task = schema["$defs"]["TaskCard"]
    assert task["additionalProperties"] is False
    assert set(task["required"]) == set(task["properties"])
    assert "default" not in json.dumps(schema).replace('"default_', "")


def test_parse_json_content_tolerates_fences() -> None:
    assert parse_json_content('{"a": 1}') == {"a": 1}
    assert parse_json_content('text\n```json\n{"a": 1}\n```\nmore') == {"a": 1}
    with pytest.raises(json.JSONDecodeError):
        parse_json_content("no json here")


# --- success --------------------------------------------------------------------------------


async def test_success_parses_plan_and_captures_cost(tmp_path: Path) -> None:
    rec = Recorder(_response(200, "success_plan.json"))
    out = await _worker(rec).run(_job(tmp_path, output_schema=Plan))

    assert out.result.status == "success"
    assert isinstance(out.parsed, Plan)
    assert [t.id for t in out.parsed.tasks] == ["T1", "T2", "T3", "T4"]
    assert out.result.summary == "plan with 4 tasks"
    assert out.result.cost_usd == pytest.approx(0.00123)
    assert out.result.tokens == {"prompt": 812, "completion": 240, "total": 1052}
    assert out.result.raw_log_path == tmp_path / "logs" / "api.log"


async def test_success_fenced_json(tmp_path: Path) -> None:
    rec = Recorder(_response(200, "success_fenced_validation.json"))
    out = await _worker(rec).run(_job(tmp_path, role="validator", output_schema=ValidationResult))
    assert out.result.status == "success"
    assert isinstance(out.parsed, ValidationResult)
    assert out.parsed.verdict == "revise"
    assert out.result.summary == "verdict: revise (3 issues)"


async def test_success_free_text_summary_is_truncated(tmp_path: Path) -> None:
    data = json.loads(_fixture("success_plan.json"))
    data["choices"][0]["message"]["content"] = "x" * 2000
    rec = Recorder(httpx.Response(200, json=data))
    out = await _worker(rec).run(_job(tmp_path, role="reviewer"))
    assert out.result.status == "success"
    assert out.parsed is None
    assert out.result.summary == "x" * 500


async def test_schema_invalid_is_error_with_raw_content_logged(tmp_path: Path) -> None:
    rec = Recorder(_response(200, "schema_invalid.json"))
    job = _job(tmp_path, role="validator", output_schema=ValidationResult)
    out = await _worker(rec).run(job)
    assert out.result.status == "error"
    assert out.parsed is None
    assert "ValidationResult" in out.result.summary
    assert out.result.cost_usd == pytest.approx(0.0002)
    assert '"verdict": "maybe"' in job.log_path.read_text()


async def test_non_json_content_is_error(tmp_path: Path) -> None:
    data = json.loads(_fixture("success_plan.json"))
    data["choices"][0]["message"]["content"] = "I think the plan is fine."
    rec = Recorder(httpx.Response(200, json=data))
    out = await _worker(rec).run(_job(tmp_path, output_schema=Plan))
    assert out.result.status == "error"
    assert "not valid JSON" in out.result.summary


async def test_missing_content_is_error(tmp_path: Path) -> None:
    rec = Recorder(httpx.Response(200, json={"choices": []}))
    out = await _worker(rec).run(_job(tmp_path))
    assert out.result.status == "error"


# --- error classification -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "fixture", "expected"),
    [
        (402, "error_402.json", "quota_exhausted"),
        (429, "error_429.json", "failure"),
        (429, "error_429_quota.json", "quota_exhausted"),
        (401, "error_401.json", "error"),
        (403, "error_401.json", "error"),
        (403, "error_403_budget.json", "quota_exhausted"),
        (500, "error_500.json", "failure"),
        (503, "error_503.json", "failure"),
    ],
)
def test_classify_api_error(status: int, fixture: str, expected: str) -> None:
    assert classify_api_error(status, _fixture(fixture)) == expected


def test_classify_daily_free_limit_as_quota() -> None:
    body = '{"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day."}}'
    assert classify_api_error(429, body) == "quota_exhausted"


def test_classify_free_model_guardrail_404_as_quota() -> None:
    # The 2026-10-25 parked run: HTTP 404 "0 endpoints available matching your guardrail
    # restrictions … Free model training violation". The remedy is another model, so it
    # counts as quota_exhausted and the fallback ladder engages (issue #327).
    body = (
        '{"error": {"code": 404, "message": "0 endpoints available matching your guardrail '
        'restrictions. Free model training violation."}}'
    )
    assert classify_api_error(404, body, free_model=True) == "quota_exhausted"


def test_classify_other_404s_stay_error() -> None:
    assert classify_api_error(404, "model not found") == "error"
    assert classify_api_error(404, "model not found", free_model=True) == "error"
    body = (
        '{"error": {"code": 404, "message": "0 endpoints available matching your guardrail '
        'restrictions. Free model training violation."}}'
    )
    assert classify_api_error(404, body, free_model=False) == "error"


def test_classify_other_codes() -> None:
    assert classify_api_error(400, "bad request") == "error"
    assert classify_api_error(408, "") == "timeout"
    assert classify_api_error(502, "not json") == "failure"


@pytest.mark.parametrize(
    ("status", "fixture", "expected"),
    [
        (402, "error_402.json", "quota_exhausted"),
        (401, "error_401.json", "error"),
        (500, "error_500.json", "failure"),
        (403, "error_403_budget.json", "quota_exhausted"),
    ],
)
async def test_run_maps_http_errors(
    tmp_path: Path, status: int, fixture: str, expected: str
) -> None:
    rec = Recorder(_response(status, fixture))
    out = await _worker(rec).run(_job(tmp_path))
    assert out.result.status == expected
    assert out.result.summary.startswith(f"HTTP {status}: ")
    assert len(rec.requests) == 1  # no retry


async def test_run_free_model_guardrail_404_is_quota_exhausted_without_retry(
    tmp_path: Path,
) -> None:
    body = (
        '{"error": {"code": 404, "message": "0 endpoints available matching your guardrail '
        'restrictions. Free model training violation."}}'
    )
    rec = Recorder(httpx.Response(404, content=body))
    out = await _worker(rec).run(_job(tmp_path, model="nvidia/nemotron-3-super-120b-a12b:free"))
    assert out.result.status == "quota_exhausted"
    assert "data-policy block" in out.result.summary
    assert len(rec.requests) == 1  # no retry: the ladder, not this worker, moves on


# --- no-training requests -----------------------------------------------------------------------


def test_no_training_sends_data_collection_deny_on_free_only(tmp_path: Path) -> None:
    worker = ApiWorker(BASE_URL, KEY_ENV)
    free = _job(tmp_path, no_training=True)
    body = worker.build_request_body(free, "nvidia/nemotron-3-super-120b-a12b:free")
    assert body["provider"] == {"data_collection": "deny"}
    paid = worker.build_request_body(free, "openai/gpt-5-mini")
    assert "provider" not in paid  # the paid fallback stays unrestricted
    plain = _job(tmp_path)
    assert "provider" not in worker.build_request_body(
        plain, "nvidia/nemotron-3-super-120b-a12b:free"
    )


def test_no_training_keeps_require_parameters(tmp_path: Path) -> None:
    worker = ApiWorker(BASE_URL, KEY_ENV)
    job = _job(tmp_path, no_training=True, output_schema=Plan)
    body = worker.build_request_body(job, "nvidia/nemotron-3-super-120b-a12b:free")
    assert body["provider"] == {"require_parameters": True, "data_collection": "deny"}


# --- retry ----------------------------------------------------------------------------------


async def test_retry_on_429_honours_retry_after(tmp_path: Path) -> None:
    rec = Recorder(
        _response(429, "error_429.json", {"Retry-After": "3"}),
        _response(200, "success_plan.json"),
    )
    sleeps = Sleeps()
    out = await _worker(rec, sleeps).run(_job(tmp_path, output_schema=Plan))
    assert out.result.status == "success"
    assert sleeps.calls == [3.0]
    assert len(rec.requests) == 2


async def test_retry_only_once(tmp_path: Path) -> None:
    rec = Recorder(_response(503, "error_503.json"), _response(503, "error_503.json"))
    sleeps = Sleeps()
    out = await _worker(rec, sleeps).run(_job(tmp_path))
    assert out.result.status == "failure"
    assert sleeps.calls == [2.0]
    assert len(rec.requests) == 2


async def test_no_retry_when_retry_after_too_long(tmp_path: Path) -> None:
    rec = Recorder(_response(429, "error_429.json", {"Retry-After": "60"}))
    sleeps = Sleeps()
    out = await _worker(rec, sleeps).run(_job(tmp_path))
    assert out.result.status == "failure"
    assert sleeps.calls == []


def test_retry_delay_parses_http_date() -> None:
    """`Retry-After` may be an HTTP date, not just seconds (issue #204)."""
    now = datetime.now(UTC)
    future = now + timedelta(seconds=5)
    delay = _retry_delay(
        httpx.Response(429, headers={"retry-after": format_datetime(future, usegmt=True)}),
        now=now,
    )
    assert delay is not None and 4.0 < delay <= 5.0
    past = now - timedelta(seconds=60)
    assert (
        _retry_delay(
            httpx.Response(429, headers={"retry-after": format_datetime(past, usegmt=True)}),
            now=now,
        )
        == 0.0
    )
    far = now + timedelta(hours=1)
    assert (
        _retry_delay(
            httpx.Response(429, headers={"retry-after": format_datetime(far, usegmt=True)}),
            now=now,
        )
        is None
    )
    assert _retry_delay(httpx.Response(429, headers={"retry-after": "tomorrow"}), now=now) is None
    assert _retry_delay(httpx.Response(429, headers={"retry-after": "3"}), now=now) == 3.0
    assert _retry_delay(httpx.Response(429), now=now) == 2.0


async def test_retry_on_429_honours_http_date_retry_after(tmp_path: Path) -> None:
    future = datetime.now(UTC) + timedelta(seconds=3)
    rec = Recorder(
        _response(429, "error_429.json", {"Retry-After": format_datetime(future, usegmt=True)}),
        _response(200, "success_plan.json"),
    )
    sleeps = Sleeps()
    out = await _worker(rec, sleeps).run(_job(tmp_path, output_schema=Plan))
    assert out.result.status == "success"
    assert len(sleeps.calls) == 1 and 1.5 <= sleeps.calls[0] <= 3.0
    assert len(rec.requests) == 2


async def test_error_body_in_log_is_capped(tmp_path: Path) -> None:
    """A provider's error body is logged for debugging, but not unbounded (issue #204)."""
    body = "y" * (ERROR_BODY_LOG_CHARS + 5000)
    rec = Recorder(httpx.Response(500, content=body))
    job = _job(tmp_path)
    out = await _worker(rec).run(job)
    assert out.result.status == "failure"
    log = job.log_path.read_text()
    assert body not in log
    assert "[error body truncated" in log


async def test_log_write_failure_does_not_raise(tmp_path: Path) -> None:
    """Workers never raise: an unwritable log directory must not fail the job."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the log directory should be")
    job = _job(tmp_path, output_schema=Plan)
    job = job.model_copy(update={"log_path": blocker / "api.log"})
    rec = Recorder(_response(200, "success_plan.json"))
    out = await _worker(rec).run(job)
    assert out.result.status == "success"


async def test_no_retry_on_quota_429(tmp_path: Path) -> None:
    rec = Recorder(_response(429, "error_429_quota.json", {"Retry-After": "1"}))
    sleeps = Sleeps()
    out = await _worker(rec, sleeps).run(_job(tmp_path))
    assert out.result.status == "quota_exhausted"
    assert sleeps.calls == []


# --- timeouts, network, key, role -----------------------------------------------------------


async def test_timeout(tmp_path: Path) -> None:
    rec = Recorder(httpx.ReadTimeout("timed out"))
    out = await _worker(rec).run(_job(tmp_path, timeout_s=5))
    assert out.result.status == "timeout"
    assert "5s" in out.result.summary


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        # Transient transport problems: the attempt failed, trying again can help.
        (httpx.ConnectError("connection refused"), "failure"),
        (httpx.ReadError("connection reset"), "failure"),
        (httpx.WriteError("broken pipe"), "failure"),
        (httpx.RemoteProtocolError("server disconnected"), "failure"),
        # Configuration problems: the worker cannot run as configured.
        (httpx.ProxyError("407 proxy authentication required"), "error"),
        (httpx.UnsupportedProtocol("unknown scheme"), "error"),
        (httpx.LocalProtocolError("bad header"), "error"),
        (httpx.TooManyRedirects("too many redirects"), "error"),
    ],
)
async def test_network_error(tmp_path: Path, exc: Exception, status: str) -> None:
    out = await _worker(Recorder(exc)).run(_job(tmp_path))
    assert out.result.status == status
    assert "network error" in out.result.summary
    assert type(exc).__name__ in out.result.summary


async def test_connect_timeout_stays_a_timeout(tmp_path: Path) -> None:
    out = await _worker(Recorder(httpx.ConnectTimeout("timed out"))).run(_job(tmp_path))
    assert out.result.status == "timeout"


async def test_missing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY_ENV)
    rec = Recorder()
    job = _job(tmp_path)
    out = await _worker(rec).run(job)
    assert out.result.status == "error"
    assert KEY_ENV in out.result.summary
    assert rec.requests == []
    assert job.log_path.exists()


async def test_missing_model(tmp_path: Path) -> None:
    worker = ApiWorker(BASE_URL, KEY_ENV, transport=httpx.MockTransport(Recorder()))
    out = await worker.run(_job(tmp_path))
    assert out.result.status == "error"
    assert "model" in out.result.summary


async def test_coder_is_rejected(tmp_path: Path) -> None:
    rec = Recorder()
    out = await _worker(rec).run(_job(tmp_path, role="coder"))
    assert out.result.status == "error"
    assert "coder" in out.result.summary
    assert rec.requests == []


async def test_injected_client_is_used(tmp_path: Path) -> None:
    rec = Recorder(_response(200, "success_plan.json"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as client:
        worker = ApiWorker(BASE_URL, KEY_ENV, "m", client=client)
        out = await worker.run(_job(tmp_path))
        assert not client.is_closed
    assert out.result.status == "success"
    assert len(rec.requests) == 1


# --- logging --------------------------------------------------------------------------------


async def test_log_has_metadata_and_never_the_key(tmp_path: Path) -> None:
    data = json.loads(_fixture("success_plan.json"))
    # Even if the model echoes the key back, it must not reach the log or the summary.
    data["choices"][0]["message"]["content"] = f"key is {FAKE_KEY} and Bearer {FAKE_KEY}"
    rec = Recorder(_response(503, "error_503.json"), httpx.Response(200, json=data))
    job = WorkerJob(
        role="reviewer",
        prompt=f"review this; token {FAKE_KEY}",
        workdir=tmp_path,
        log_path=tmp_path / "nested" / "dir" / "api.log",
        effort="low",
    )
    out = await _worker(rec).run(job)
    log = job.log_path.read_text()
    assert FAKE_KEY not in log
    assert FAKE_KEY not in out.result.summary
    assert "Authorization" not in log
    assert "role: reviewer" in log
    assert "model: openai/gpt-5-mini" in log
    assert "effort: low" in log
    assert "HTTP 503" in log
    assert "HTTP 200" in log
    assert '"cost": 0.00123' in log
    assert "[REDACTED]" in log


async def test_log_redacts_unknown_key_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    odd_key = "plainkeyvalue-ABCDEFGH"
    monkeypatch.setenv(KEY_ENV, odd_key)
    data = json.loads(_fixture("success_plan.json"))
    data["choices"][0]["message"]["content"] = f"echo {odd_key}"
    rec = Recorder(httpx.Response(200, json=data))
    job = _job(tmp_path, role="reviewer")
    out = await _worker(rec).run(job)
    assert odd_key not in job.log_path.read_text()
    assert odd_key not in out.result.summary


async def test_log_redacts_other_secret_env_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = "another-secret-value-123"
    monkeypatch.setenv("SOME_SERVICE_TOKEN", other)
    data = json.loads(_fixture("success_plan.json"))
    data["choices"][0]["message"]["content"] = f"leaked {other}"
    rec = Recorder(httpx.Response(200, json=data))
    job = _job(tmp_path, role="reviewer")
    out = await _worker(rec).run(job)
    assert other not in job.log_path.read_text()
    assert other not in out.result.summary


async def test_error_summary_scrubs_the_key(tmp_path: Path) -> None:
    rec = Recorder(httpx.Response(400, json={"error": {"message": f"bad key {FAKE_KEY}"}}))
    out = await _worker(rec).run(_job(tmp_path))
    assert out.result.status == "error"
    assert FAKE_KEY not in out.result.summary
