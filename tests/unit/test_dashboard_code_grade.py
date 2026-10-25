"""Code Grade page (`/code-grade`): grade a diff / route a task from the browser
(issue #254). Through Starlette's in-process test client; the model is never
loaded -- tests patch the `routes_code._orchestrator` seam with a fake backend,
the same pattern the code-orchestrator unit tests use.
"""

from __future__ import annotations

import re
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
from jev_agent.dashboard import routes_code
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
        routes_code,
        "_orchestrator",
        lambda settings: CodeOrchestrator(
            model_id=settings.model_id,
            max_diff_chars=settings.max_diff_chars,
            backend=backend,
        ),
    )
    return backend


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
    assert "jev-agent[code-orchestrator]" in page.text


def test_nav_links_code_grade(client: TestClient) -> None:
    assert 'href="/code-grade"' in client.get("/").text


def test_grade_shows_result(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _patch_orchestrator(monkeypatch, _FakeBackend("accept", 0.95))
    response = _post(client, "/code-grade/grade", {"task": "Fix x", "diff": DIFF, "tests": ""})
    assert response.status_code == 200
    assert "code_quality" in response.text
    assert "3/4" in response.text
    assert "accept" in response.text
    assert "human_review" not in response.text
    assert 'value="Fix x"' in response.text  # the entered task stays in the form
    assert backend.seen["task"] == "Fix x"
    assert backend.seen["diff"] == DIFF.strip()  # _text() strips the pasted value


def test_grade_low_confidence_shows_human_review(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_orchestrator(monkeypatch, _FakeBackend("accept", 0.4))
    response = _post(client, "/code-grade/grade", {"task": "Fix x", "diff": DIFF})
    assert response.status_code == 200
    assert "human_review" in response.text


def test_grade_unavailable_backend_shows_guidance_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_orchestrator(monkeypatch, _UnavailableBackend())
    response = _post(client, "/code-grade/grade", {"task": "Fix x", "diff": DIFF})
    assert response.status_code == 200
    assert "not installed" in response.text
    assert "jev-agent[code-orchestrator]" in response.text
    assert "Traceback" not in response.text


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
    assert response.status_code == 200
    assert "3/4" in response.text
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
    assert response.status_code == 200
    assert "frontier" in response.text
    assert "refactor" in response.text
    assert "high" in response.text  # suggested_effort for frontier
    assert backend.seen["request"] == "Refactor the parser"


def test_route_low_confidence_shows_no_hint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_orchestrator(monkeypatch, _FakeBackend(route_confidence=0.4))
    response = _post(client, "/code-grade/route", {"route_request": "Refactor the parser"})
    assert response.status_code == 200
    assert "not confident enough to route" in response.text
    assert "Routing hint" not in response.text
    assert "suggested_effort" not in response.text


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
    assert response.status_code == 200
    assert "not installed" in response.text
    assert "Traceback" not in response.text
