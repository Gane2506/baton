"""Code Grade page (`/code-grade`): grade a diff / route a task from the browser
(issue #254). Through Starlette's in-process test client; the model is never
loaded -- tests patch the `code_grade_runner._orchestrator` seam with a fake
backend, the same pattern the code-orchestrator unit tests use.

Grades run in the background (issue #267): a POST starts the job and
redirects to `/code-grade/status`, and the result waits on the live
fragment there -- so these tests poll `/code-grade/live` for the outcome
instead of reading it off the POST response.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.testclient import TestClient

from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.code_orchestrator import (
    CodeOrchestrator,
    CodeOrchestratorUnavailable,
)
from jev_agent.dashboard import code_grade_runner, routes_code
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings

DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


class _FakeBackend:
    """A backend returning canned verdicts, like the real package's."""

    def __init__(
        self,
        merge_action: str = "accept",
        confidence: float = 0.95,
        route_confidence: float = 0.8,
    ) -> None:
        self.merge_action = merge_action
        self.confidence = confidence
        self.route_confidence = route_confidence
        self.seen: dict[str, str] = {}

    def grade_code(self, *, task: str, diff: str, tests: str) -> object:
        self.seen = {"task": task, "diff": diff, "tests": tests}
        return {
            "code_quality": 3,
            "instruction_followed": "yes",
            "likely_correct": "yes",
            "merge_action": self.merge_action,
            "answer_confidence": {"merge_action": self.confidence},
        }

    def route_model(self, request: str) -> object:
        self.seen = {"request": request}
        return {
            "model_tier": "frontier",
            "task_difficulty": 2,
            "skill": "refactor",
            "answer_confidence": {"model_tier": self.route_confidence},
        }


class _UnavailableBackend(_FakeBackend):
    """A backend that is missing: every call raises Unavailable."""

    def grade_code(self, *, task: str, diff: str, tests: str) -> object:
        raise CodeOrchestratorUnavailable(
            "the Coding Decision Agent is not installed: install `laya`"
        )

    def route_model(self, request: str) -> object:
        raise CodeOrchestratorUnavailable(
            "the Coding Decision Agent is not installed: install `laya`"
        )


class _NoBackend:
    """An orchestrator stand-in reporting no backend at all (page status)."""

    available = False


@pytest.fixture(autouse=True)
def _fake_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


@pytest.fixture
def client(fixture_repo: Path) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        yield client


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/code-grade").text)
    assert match
    return match.group(1)


def _post(
    client: TestClient,
    url: str,
    data: dict[str, str],
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> httpx.Response:
    kwargs: dict[str, Any] = {
        "data": {**data, "csrf_token": _csrf(client)},
        "follow_redirects": False,
    }
    if files is not None:
        kwargs["files"] = files
    return cast(httpx.Response, client.post(url, **kwargs))


def _patch_orchestrator(
    monkeypatch: pytest.MonkeyPatch, backend: object
) -> _FakeBackend | _UnavailableBackend:
    """Route the page's orchestrator at the fake backend; returns the backend."""
    assert isinstance(backend, (_FakeBackend, _UnavailableBackend))
    monkeypatch.setattr(
        code_grade_runner,
        "_orchestrator",
        lambda settings: CodeOrchestrator(
            model_id=settings.model_id,
            max_diff_chars=settings.max_diff_chars,
            backend=backend,
        ),
    )
    return backend


def _live_until(client: TestClient, needle: str, timeout_s: float = 10.0) -> str:
    """Poll `/code-grade/live` until the fragment contains `needle`."""
    deadline = time.monotonic() + timeout_s
    text = ""
    while time.monotonic() < deadline:
        text = client.get("/code-grade/live").text
        if needle in text:
            return text
        time.sleep(0.05)
    return text


def test_code_grade_page_renders(client: TestClient) -> None:
    page = client.get("/code-grade")
    assert page.status_code == 200
    assert "<h1>Code Grade</h1>" in page.text
    assert "Grade a diff" in page.text
    assert "Route a coding task" in page.text
    assert "S4MPL3BI4S/Coding_Decision_Agent" in page.text


def test_code_grade_page_shows_unavailable_guidance(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_code, "_orchestrator", lambda settings: _NoBackend())
    page = client.get("/code-grade")
    assert page.status_code == 200
    assert "not installed" in page.text
    assert "jev-agent[code-orchestrator]" not in page.text
    assert "Install backend" in page.text


def test_nav_links_code_grade(client: TestClient) -> None:
    assert 'href="/code-grade"' in client.get("/").text


def test_grade_redirects_to_status_and_result_waits_there(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The POST returns at once (303 to the status page): it must not hold
    # the request for the minutes a live grade takes.
    backend = _patch_orchestrator(monkeypatch, _FakeBackend("accept", 0.95))
    response = _post(client, "/code-grade/grade", {"task": "Fix x", "diff": DIFF, "tests": ""})
    assert response.status_code == 303
    assert response.headers["location"] == "/code-grade/status"
    status_page = client.get("/code-grade/status")
    assert status_page.status_code == 200
    assert "Code Grade status" in status_page.text
    live = _live_until(client, "3/4")
    assert "code_quality" in live
    assert "3/4" in live
    assert "accept" in live
    assert "human_review" not in live
    assert backend.seen["task"] == "Fix x"
    assert backend.seen["diff"] == DIFF.strip()  # _text() strips the pasted value


def test_grade_low_confidence_shows_human_review(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_orchestrator(monkeypatch, _FakeBackend("accept", 0.4))
    response = _post(client, "/code-grade/grade", {"task": "Fix x", "diff": DIFF})
    assert response.status_code == 303
    live = _live_until(client, "human_review")
    assert "human_review" in live
    assert "a human must decide" in live


def test_grade_unavailable_backend_shows_guidance_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_orchestrator(monkeypatch, _UnavailableBackend())
    response = _post(client, "/code-grade/grade", {"task": "Fix x", "diff": DIFF})
    assert response.status_code == 303
    live = _live_until(client, "not installed")
    assert "not installed" in live
    assert "jev-agent[code-orchestrator]" not in live
    assert "Code Grade page" in live
    assert "Traceback" not in live


def test_grade_requires_a_task(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_orchestrator(monkeypatch, _FakeBackend())
    response = _post(client, "/code-grade/grade", {"task": "", "diff": DIFF})
    assert response.status_code == 200
    assert "describe the task first" in response.text


def test_grade_requires_a_diff(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_orchestrator(monkeypatch, _FakeBackend())
    response = _post(client, "/code-grade/grade", {"task": "Fix x", "diff": ""})
    assert response.status_code == 200
    assert "paste a diff or upload a diff file" in response.text


def test_grade_accepts_an_uploaded_diff_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _patch_orchestrator(monkeypatch, _FakeBackend("accept", 0.95))
    response = _post(
        client,
        "/code-grade/grade",
        {"task": "Fix x", "diff": ""},
        files={"diff_file": ("change.diff", DIFF.encode(), "text/plain")},
    )
    assert response.status_code == 303
    assert "3/4" in _live_until(client, "3/4")
    assert backend.seen["diff"] == DIFF


def test_grade_rejects_a_non_utf8_diff_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_orchestrator(monkeypatch, _FakeBackend())
    response = _post(
        client,
        "/code-grade/grade",
        {"task": "Fix x", "diff": ""},
        files={"diff_file": ("change.diff", b"\xff\xfe\x00bad", "text/plain")},
    )
    assert response.status_code == 200
    assert "not UTF-8 text" in response.text


def test_route_shows_hint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _patch_orchestrator(monkeypatch, _FakeBackend(route_confidence=0.8))
    response = _post(client, "/code-grade/route", {"route_request": "Refactor the parser"})
    assert response.status_code == 303
    live = _live_until(client, "frontier")
    assert "frontier" in live
    assert "refactor" in live
    assert "high" in live  # suggested_effort for frontier
    assert backend.seen["request"] == "Refactor the parser"


def test_route_low_confidence_shows_no_hint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_orchestrator(monkeypatch, _FakeBackend(route_confidence=0.4))
    response = _post(client, "/code-grade/route", {"route_request": "Refactor the parser"})
    assert response.status_code == 303
    live = _live_until(client, "not confident enough to route")
    assert "not confident enough to route" in live
    assert "Routing hint" not in live
    assert "suggested_effort" not in live


def test_route_requires_a_task(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_orchestrator(monkeypatch, _FakeBackend())
    response = _post(client, "/code-grade/route", {"route_request": ""})
    assert response.status_code == 200
    assert "describe the coding task to route" in response.text


def test_route_unavailable_backend_shows_guidance_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_orchestrator(monkeypatch, _UnavailableBackend())
    response = _post(client, "/code-grade/route", {"route_request": "Refactor the parser"})
    assert response.status_code == 303
    live = _live_until(client, "not installed")
    assert "not installed" in live
    assert "Traceback" not in live


def test_status_page_idle_state(client: TestClient) -> None:
    page = client.get("/code-grade/status")
    assert page.status_code == 200
    assert "No grade has run yet" in page.text


def _blocking_client(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, threading.Event, threading.Event]:
    """A client whose grade executor waits until the test releases it."""
    _patch_orchestrator(monkeypatch, _FakeBackend("accept", 0.95))
    started = threading.Event()
    release = threading.Event()

    def executor(kind: str, settings: Any, inputs: dict[str, str]) -> dict[str, Any]:
        started.set()
        assert release.wait(timeout=10)
        if kind == "grade":
            return code_grade_runner.grade(
                settings, inputs["task"], inputs["diff"], inputs["tests"]
            )
        return code_grade_runner.route(settings, inputs["route_request"])

    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
        code_grade_executor=executor,
    )
    return TestClient(app), started, release


def test_second_grade_while_running_is_refused(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, started, release = _blocking_client(fixture_repo, monkeypatch)
    with client:
        assert (
            _post(client, "/code-grade/grade", {"task": "Fix x", "diff": DIFF}).status_code == 303
        )
        assert started.wait(timeout=5)
        # The running job is linked from the form page, and a second grade
        # is refused instead of contending with the first for CPU/memory.
        assert "watch its progress" in client.get("/code-grade").text
        refused = _post(client, "/code-grade/grade", {"task": "Fix y", "diff": DIFF})
        assert refused.status_code == 303
        assert refused.headers["location"] == "/code-grade"
        assert "already running" in client.get("/code-grade").text
        release.set()
        assert "3/4" in _live_until(client, "3/4")


def test_cancel_discards_the_result(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, started, release = _blocking_client(fixture_repo, monkeypatch)
    with client:
        assert (
            _post(client, "/code-grade/grade", {"task": "Fix x", "diff": DIFF}).status_code == 303
        )
        assert started.wait(timeout=5)
        cancelled = _post(client, "/code-grade/cancel", {})
        assert cancelled.status_code == 303
        assert cancelled.headers["location"] == "/code-grade/status"
        assert "Cancelled" in _live_until(client, "Cancelled")
        # The released grade finishes in the background, but its outcome
        # belongs to a cancelled job: the status page never shows it.
        release.set()
        time.sleep(0.5)
        assert "3/4" not in client.get("/code-grade/live").text
        assert "Cancelled" in client.get("/code-grade/live").text
