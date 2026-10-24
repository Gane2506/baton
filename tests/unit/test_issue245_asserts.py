"""Pins for the R4-A assert cleanup (issue #245): every production `assert` /
`AssertionError` in `src/` replaced with an explicit error (or, for three
narrowing-only asserts, restructured away). Each test proves the new error
type — never `AssertionError` — where the behaviour is reachable; the
defensive branches that are unreachable by construction are pinned with a
stub that breaks the invariant between the check and the read.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from jev_agent.app.context import AppContext
from jev_agent.app.errors import JevError
from jev_agent.app.runs import _learning as runs_learning
from jev_agent.cli.chat_cmds import _learning as chat_learning
from jev_agent.core.design_codegen import _section
from jev_agent.core.learner import learn, score_lessons
from jev_agent.core.learning import LearningError
from jev_agent.core.models import Decision, DecisionRequest, Idea, RunState
from jev_agent.core.provider_auth import (
    ClaudeSetupTokenFlow,
    CodexDeviceFlow,
    DeviceFlowError,
)
from jev_agent.core.recurrence import Schedule, ScheduleError, next_due, parse_schedule
from jev_agent.core.scheduler import (
    Scheduler,
    SchedulerLock,
    SchedulerLocked,
    SchedulerOptions,
)
from jev_agent.core.store import NotFoundError, QueueItem, RunRecord, Store, StoreError
from jev_agent.core.store.records import QualityReportRecord
from jev_agent.dashboard.security import DashboardSettings, SecurityMiddleware
from jev_agent.mcp_runs import _score_line
from jev_agent.quality.models import GateResult, ScoreBreakdown
from jev_agent.quality.report import _ship_score_summary
from jev_agent.servers.mcp import (
    MODERN_VERSION,
    UNSUPPORTED_VERSION,
    VERSION_META,
    McpServer,
    _RpcError,
)
from jev_agent.workers.api import ApiWorker, _Log, _RequestFailed
from jev_agent.workers.base import WorkerJob
from jev_agent.workers.openrouter_models import parse_model
from jev_agent.workers.subprocess_runner import _feed, _run

FIXTURES = Path(__file__).parent.parent / "fixtures" / "openrouter"


# ---- learning registry ----------------------------------------------------------


def test_learning_helpers_raise_jev_error_when_registry_missing(tmp_path: Path) -> None:
    """`app/runs.py` + `cli/chat_cmds.py`: a context with no learning registry
    raises `JevError` (the CLI prints it and exits 1) instead of asserting."""
    ctx = AppContext(
        root=tmp_path,
        config=MagicMock(),
        store=MagicMock(),
        pipeline=MagicMock(),
        has_workers=False,
        learning=None,
    )
    for helper in (runs_learning, chat_learning):
        with pytest.raises(JevError, match="learning is not available"):
            helper(ctx)


# ---- recurrence -----------------------------------------------------------------


def test_next_due_naive_croniter_result_raises_schedule_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`core/recurrence.py`: a naive datetime from croniter is a `ScheduleError`,
    not an assert — the caller (`scheduler`) logs it and skips that task."""

    class _NaiveCroniter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def get_next(self, _type: type[datetime]) -> datetime:
            return datetime(2026, 10, 25, 9, 30)  # naive

    monkeypatch.setattr("jev_agent.core.recurrence.croniter", _NaiveCroniter)
    schedule = Schedule(cron="30 9 * * *", kind="daily", source="daily 09:30")
    with pytest.raises(ScheduleError, match="naive datetime"):
        next_due(schedule, datetime(2026, 10, 24, 9, 0, tzinfo=UTC), ZoneInfo("UTC"))


# ---- scheduler ------------------------------------------------------------------


def test_scheduler_lock_write_without_lock_raises(tmp_path: Path) -> None:
    """`core/scheduler.py`: writing lock state without holding the lock raises
    `SchedulerLocked` instead of asserting."""
    lock = SchedulerLock(tmp_path / "scheduler.lock")
    with pytest.raises(SchedulerLocked, match="lock is not held"):
        lock._write()


async def _noop_start(item: QueueItem) -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        id="noop",
        idea=Idea(text=item.idea, repo_path=Path(".")),
        state=RunState.DONE,
        plan=None,
        plan_round=0,
        spend_usd=0.0,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / ".jev" / "state.db") as s:
        yield s


def test_previous_still_active_without_active_item_raises(store: Store) -> None:
    """`core/scheduler.py`: `_previous_still_active` on a task with no active
    item raises `RuntimeError` (the caller guards this, so it is the loud
    failure for a future caller that forgets the guard)."""
    task = store.add_recurring_task(
        "hourly check",
        parse_schedule("hourly"),
        "UTC",
        datetime(2026, 10, 24, 9, 0, tzinfo=UTC),
    )
    assert task.active_item_id is None
    sched = Scheduler(store, _noop_start, SchedulerOptions(), sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="has no active queue item"):
        sched._previous_still_active(task)


# ---- provider auth watchers -------------------------------------------------------


def test_codex_watcher_reports_dead_process_as_device_flow_error() -> None:
    """`core/provider_auth.py` (Codex): a watcher whose process vanished sets a
    failed status with an actionable detail instead of an empty AssertionError."""
    flow = CodexDeviceFlow()
    flow._process = SimpleNamespace(stdout=None)
    flow._watch()
    status = flow.status()
    assert status.state == "failed"
    assert "output could be read" in status.detail
    assert "Connections page" in status.detail


def test_claude_watcher_reports_dead_process_as_device_flow_error() -> None:
    """`core/provider_auth.py` (Claude setup-token): same as the Codex watcher."""
    flow = ClaudeSetupTokenFlow()
    flow._process = SimpleNamespace(stdout=None)
    flow._watch()
    status = flow.status()
    assert status.state == "failed"
    assert "output could be read" in status.detail
    assert "Connections page" in status.detail


def test_device_flow_error_is_a_jev_error() -> None:
    assert issubclass(DeviceFlowError, JevError)


# ---- learner ----------------------------------------------------------------------


def _run_without_profile(tmp_path: Path) -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        id="r1",
        idea=Idea(text="add search", repo_path=tmp_path),
        state=RunState.DONE,
        plan=None,
        plan_round=0,
        spend_usd=0.0,
        created_at=now,
        updated_at=now,
        learning_profile=None,
    )


async def test_learn_raises_learning_error_without_profile(tmp_path: Path) -> None:
    """`core/learner.py::learn`: a run with no learning profile raises
    `LearningError` (the pipeline only calls it for runs with a profile)."""
    with pytest.raises(LearningError, match="has no learning profile"):
        await learn(MagicMock(), _run_without_profile(tmp_path))


def test_score_lessons_raises_learning_error_without_profile(tmp_path: Path) -> None:
    """`core/learner.py::score_lessons`: same precondition, same error."""
    with pytest.raises(LearningError, match="has no learning profile"):
        score_lessons(MagicMock(), _run_without_profile(tmp_path), MagicMock())


# ---- design codegen (narrowing restructure) ---------------------------------------


def test_section_prefers_rich_design_tokens() -> None:
    """`core/design_codegen.py::_section`: the rich `design_tokens` section wins
    over the legacy flat key (the restructure that dropped the assert)."""
    tokens: dict[str, object] = {
        "design_tokens": {"colors": {"primary": "#111111"}},
        "colors": {"primary": "#222222"},
    }
    assert _section(tokens, "colors") == {"primary": "#111111"}
    assert _section({"colors": {"primary": "#222222"}}, "colors") == {"primary": "#222222"}
    assert _section({}, "colors") == {}


# ---- store inserts ------------------------------------------------------------------


class _NoIdCursor:
    """A cursor whose `lastrowid` is None, delegating everything else."""

    def __init__(self, cur: sqlite3.Cursor) -> None:
        self._cur = cur

    @property
    def lastrowid(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cur, name)


class _NoIdDb:
    """A connection wrapper: INSERTs report no id, everything else is real."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor | _NoIdCursor:
        cur = self._conn.execute(sql, params)
        if sql.strip().upper().startswith("INSERT"):
            return _NoIdCursor(cur)
        return cur


@contextmanager
def _tx_with_no_id(store: Store) -> Iterator[Any]:
    yield _NoIdDb(store._conn)


def _run_with_store(store: Store, tmp_path: Path) -> RunRecord:
    return store.create_run(Idea(text="add search", repo_path=tmp_path))


def test_store_attempt_insert_without_id_raises_store_error(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`core/store/attempts.py`: an INSERT that returns no id raises
    `StoreError` (cf. `core/store/queue.py:70`)."""
    run = _run_with_store(store, tmp_path)
    monkeypatch.setattr(store, "_tx", lambda: _tx_with_no_id(store))
    with pytest.raises(StoreError, match="attempt insert did not return an id"):
        store.start_attempt(run.id, None, 1, "planner", "fake")


def test_store_decision_insert_without_id_raises_store_error(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`core/store/decisions.py`: same, for decisions."""
    run = _run_with_store(store, tmp_path)
    req = DecisionRequest(point="D1", state={"idea": "x"}, questions={"clear": ["yes", "no"]})
    dec = Decision(point="D1", answers={"clear": "yes"}, dispatcher="jev")
    monkeypatch.setattr(store, "_tx", lambda: _tx_with_no_id(store))
    with pytest.raises(StoreError, match="decision insert did not return an id"):
        store.record_decision(run.id, None, req, dec)


def _gate() -> GateResult:
    return GateResult(
        level="basic",
        scope="integration",
        score=ScoreBreakdown(
            criteria={"correctness": 5},
            weights={"correctness": 1.0},
            total=100,
            threshold=80,
            passed=True,
        ),
    )


def test_store_quality_insert_without_id_raises_store_error(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`core/store/quality.py`: same, for quality reports."""
    run = _run_with_store(store, tmp_path)
    monkeypatch.setattr(store, "_tx", lambda: _tx_with_no_id(store))
    with pytest.raises(StoreError, match="quality report insert did not return an id"):
        store.add_quality_report(run.id, _gate())


def test_store_shipped_pr_insert_without_id_raises_store_error(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`core/store/shipped.py`: same, for shipped PRs."""
    run = _run_with_store(store, tmp_path)
    monkeypatch.setattr(store, "_tx", lambda: _tx_with_no_id(store))
    with pytest.raises(StoreError, match="shipped PR insert did not return an id"):
        store.add_shipped_pr(
            run.id,
            remote="origin",
            repo="a/b",
            branch="jev/x",
            base="main",
            number=3,
            url="https://github.com/a/b/pull/3",
            title="x",
            draft=True,
        )


class _NoRowCursor:
    """A cursor whose `fetchone` is None, delegating everything else."""

    def __init__(self, cur: sqlite3.Cursor) -> None:
        self._cur = cur

    def fetchone(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cur, name)


class _NoRowDb:
    """A connection wrapper: the shipped-PR re-read finds no row."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor | _NoRowCursor:
        cur = self._conn.execute(sql, params)
        if sql.strip().upper().startswith("SELECT") and "FROM shipped_prs" in sql:
            return _NoRowCursor(cur)
        return cur


@contextmanager
def _tx_with_no_row(store: Store) -> Iterator[Any]:
    yield _NoRowDb(store._conn)


def test_update_shipped_pr_missing_row_raises_not_found(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`core/store/shipped.py`: a row that vanishes between UPDATE and re-read
    raises `NotFoundError`, matching the rowcount check just above it."""
    run = _run_with_store(store, tmp_path)
    pr = store.add_shipped_pr(
        run.id,
        remote="origin",
        repo="a/b",
        branch="jev/x",
        base="main",
        number=3,
        url="https://github.com/a/b/pull/3",
        title="x",
        draft=True,
    )
    monkeypatch.setattr(store, "_tx", lambda: _tx_with_no_row(store))
    with pytest.raises(NotFoundError, match=f"shipped PR {pr.id} not found"):
        store.update_shipped_pr(
            pr.id,
            state="merged",
            draft=False,
            merged_at=None,
            closed_at=None,
            review_comments=0,
            reviews=0,
            ci_status="success",
            commits_after_open=0,
            additions=0,
            deletions=0,
            changed_files=0,
            reverted=False,
            raw=None,
        )


# ---- dashboard security -------------------------------------------------------------


async def test_security_middleware_session_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dashboard/security.py`: when no session cookie exists and no session id
    can be minted, the middleware raises `RuntimeError` (never an assert)."""
    monkeypatch.setattr(secrets, "token_urlsafe", lambda n: None)
    settings = DashboardSettings(root=tmp_path)
    middleware = SecurityMiddleware(cast(Any, lambda scope, receive, send: None), settings)

    async def _next(request: Request) -> Response:
        return PlainTextResponse("ok")

    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1")],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8765),
    }
    with pytest.raises(RuntimeError, match="could not create a dashboard session"):
        await middleware.dispatch(Request(scope), _next)


# ---- ship score lines -------------------------------------------------------------------


class _FlakyScore:
    """A gate result whose `score` passes the `is not None` filter, then reads
    as None — the only way the defensive branch after the filter can fire."""

    scope = "integration"

    def __init__(self, score: ScoreBreakdown) -> None:
        self._score = score
        self._reads = 0

    @property
    def score(self) -> ScoreBreakdown | None:
        self._reads += 1
        return self._score if self._reads == 1 else None


def _report(record_result: Any) -> QualityReportRecord:
    now = datetime.now(UTC)
    return QualityReportRecord(
        id=1, run_id="r1", task_id=None, result=record_result, report_path=None, created_at=now
    )


def test_score_line_reports_passing_ship_score(tmp_path: Path) -> None:
    """`mcp_runs.py::_score_line`: the normal path is unchanged."""
    run = _run_without_profile(tmp_path)
    line = _score_line(run, [_report(_gate())])
    assert line == "quality: ship score 100/100 (threshold 80): passed the ship gate"


def test_score_line_score_vanishing_raises_value_error(tmp_path: Path) -> None:
    """`mcp_runs.py`: a score that vanishes after the filter raises
    `ValueError`, never `AssertionError`."""
    score = _gate().score
    assert score is not None
    flaky = _FlakyScore(score)
    reports = cast(list[QualityReportRecord], [SimpleNamespace(result=flaky)])
    with pytest.raises(ValueError, match="has no ship score"):
        _score_line(_run_without_profile(tmp_path), reports)


def test_ship_score_summary_reports_passing_gate() -> None:
    """`quality/report.py::_ship_score_summary`: the normal path is unchanged."""
    lines = _ship_score_summary([_gate()])
    assert lines == [
        "Ship score (integration): **100/100** (threshold 80): passed the ship gate.",
        "",
    ]


def test_ship_score_summary_score_vanishing_raises_value_error() -> None:
    """`quality/report.py`: same defensive branch, same `ValueError`."""
    score = _gate().score
    assert score is not None
    flaky = _FlakyScore(score)
    with pytest.raises(ValueError, match="has no ship score"):
        _ship_score_summary([cast(GateResult, flaky)])


# ---- MCP server version gate (narrowing restructure) --------------------------------------


def test_mcp_dispatch_rejects_unsupported_version() -> None:
    """`servers/mcp.py::dispatch`: the version check still runs after the
    narrowing restructure (no assert, same `_RpcError`)."""
    server = McpServer(name="test", version="0", tools=[])
    with pytest.raises(_RpcError) as info:
        server.dispatch("initialize", {"_meta": {VERSION_META: "2099-01-01"}})
    assert info.value.code == UNSUPPORTED_VERSION


def test_mcp_dispatch_marks_modern_results_complete() -> None:
    """`servers/mcp.py::dispatch`: the `modern` flag the restructure touched
    still wraps modern results with `resultType: complete` (and leaves
    legacy calls unwrapped)."""
    server = McpServer(name="test", version="0", tools=[])
    assert server.dispatch("ping", {"_meta": {VERSION_META: MODERN_VERSION}}) == {
        "resultType": "complete"
    }
    assert server.dispatch("ping", {}) == {}


# ---- OpenRouter catalog (narrowing restructure) -------------------------------------------


def test_parse_model_ignores_non_dict_pricing() -> None:
    """`workers/openrouter_models.py::parse_model`: a non-dict `pricing` is
    treated as no pricing (the restructure that dropped the assert)."""
    model = parse_model({"id": "x/model", "pricing": "not-a-dict"})
    assert model is not None
    assert model.prompt_price is None and model.completion_price is None
    priced = parse_model({"id": "x/model", "pricing": {"prompt": "0.1", "completion": "0.2"}})
    assert priced is not None
    assert priced.prompt_price == 0.1 and priced.completion_price == 0.2


# ---- API worker retry fall-through -----------------------------------------------------------


def _response(status: int, fixture: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=(FIXTURES / fixture).read_text(), headers=headers)


class _Recorder:
    """A MockTransport handler replaying responses in order."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


class _Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _api_worker(recorder: _Recorder, sleeps: _Sleeps) -> ApiWorker:
    return ApiWorker(
        "https://openrouter.test/api/v1",
        "JEV_TEST_OPENROUTER_KEY",
        default_model="openai/gpt-5-mini",
        transport=httpx.MockTransport(recorder),
        sleep=sleeps,
    )


async def test_api_retry_then_success_returns_body(tmp_path: Path) -> None:
    """`workers/api.py::_post_with_retry`: 429 then 200 returns the body."""
    rec = _Recorder(
        _response(429, "error_429.json", {"Retry-After": "1"}),
        _response(200, "success_plan.json"),
    )
    worker = _api_worker(rec, _Sleeps())
    job = WorkerJob(role="planner", prompt="p", workdir=tmp_path, log_path=tmp_path / "api.log")
    body = await worker._post_with_retry(
        job, {"model": "openai/gpt-5-mini", "messages": []}, "k", _Log(tmp_path / "api.log")
    )
    assert body == json.loads((FIXTURES / "success_plan.json").read_text())
    assert len(rec.requests) == 2


async def test_api_double_failure_raises_request_failed(tmp_path: Path) -> None:
    """`workers/api.py`: two retryable failures raise `_RequestFailed` —
    the retry fall-through's error type (cf. PR #243) — never `AssertionError`."""
    rec = _Recorder(
        _response(429, "error_429.json", {"Retry-After": "1"}),
        _response(429, "error_429.json", {"Retry-After": "1"}),
    )
    worker = _api_worker(rec, _Sleeps())
    job = WorkerJob(role="planner", prompt="p", workdir=tmp_path, log_path=tmp_path / "api.log")
    with pytest.raises(_RequestFailed, match="HTTP 429"):
        await worker._post_with_retry(
            job, {"model": "openai/gpt-5-mini", "messages": []}, "k", _Log(tmp_path / "api.log")
        )
    assert len(rec.requests) == 2


# ---- subprocess runner -------------------------------------------------------------------------


async def test_feed_without_stdin_raises() -> None:
    """`workers/subprocess_runner.py::_feed`: no stdin pipe raises
    `RuntimeError` instead of asserting."""
    proc = cast(asyncio.subprocess.Process, SimpleNamespace(stdin=None))
    with pytest.raises(RuntimeError, match="stdin was not created"):
        await _feed(proc, "hello")


async def test_run_without_pipes_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`workers/subprocess_runner.py::_run`: a child spawned without pipes
    raises `RuntimeError` instead of asserting."""

    async def _fake_create(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(stdout=None, stderr=None, stdin=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    with pytest.raises(RuntimeError, match="pipes were not created"):
        await _run(["echo", "hi"], tmp_path, {}, 5.0, None, time.monotonic())
