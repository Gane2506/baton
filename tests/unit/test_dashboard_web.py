"""Dashboard feature tests: browser chat, board actions, current-build KPIs (no network).

The chat backends, the run launcher and the workers are all fakes here: `JEV_FAKE_WORKERS=1`
swaps in the echo chat backend and a fake registry, and the launcher's executor is a
`Calls` recorder, so nothing touches a model, a CLI or GitHub.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from starlette.testclient import TestClient

from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.models import RunState
from jev_agent.core.store import Store
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.runner import RunAction
from jev_agent.dashboard.security import DashboardSettings


@pytest.fixture(autouse=True)
def _fake_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


class Calls:
    """A fake run executor: records calls and answers at once (no workers run)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, RunAction]] = []

    def __call__(self, root: Path, run_id: str, action: RunAction) -> str:
        self.calls.append((root, run_id, action))
        return "run DONE"


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def client(fixture_repo: Path, calls: Calls) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
        executor=calls,
    )
    with TestClient(app) as client:
        yield client


def _dashboard(client: TestClient) -> Any:
    return cast(Any, client.app).state.dashboard


def _store(repo: Path) -> Store:
    return Store.open(repo / ".jev" / "state.db")


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/chats").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str] | None = None) -> httpx.Response:
    response: httpx.Response = client.post(
        url,
        data={**(data or {}), "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    return response


def _new_chat(client: TestClient) -> str:
    response = _post(client, "/chats/new", {"worker": "api", "learning": "", "budget_usd": "1.00"})
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[1]


def _say(client: TestClient, chat_id: str, text: str) -> None:
    response = _post(client, f"/chats/{chat_id}/send", {"text": text})
    assert response.status_code == 303
    job = _dashboard(client).chat_runner.wait(chat_id)
    assert job is not None and job.state == "finished" and "replied" in job.message


def _brief(client: TestClient, chat_id: str) -> None:
    response = _post(client, f"/chats/{chat_id}/brief")
    assert response.status_code == 303
    job = _dashboard(client).chat_runner.wait(chat_id)
    assert job is not None and job.state == "finished" and "brief" in job.message


def test_sidebar_offers_a_new_chat_and_the_page_has_a_form(client: TestClient) -> None:
    page = client.get("/")
    assert 'class="sidebar-new"' in page.text and "New chat" in page.text
    chats = client.get("/chats")
    assert chats.status_code == 200 and 'action="/chats/new"' in chats.text


def test_create_and_continue_a_chat(client: TestClient) -> None:
    chat_id = _new_chat(client)
    assert chat_id.startswith("c-")
    assert chat_id in client.get("/chats").text

    _say(client, chat_id, "hello from the web")
    page = client.get(f"/chats/{chat_id}")
    assert "(fake model) You said: hello from the web" in page.text
    live = client.get(f"/chats/{chat_id}/live")
    assert live.status_code == 200 and "(fake model)" in live.text


def test_chat_brief_then_plan_starts_a_run(
    client: TestClient, calls: Calls, fixture_repo: Path
) -> None:
    chat_id = _new_chat(client)
    _say(client, chat_id, "add word counts")
    _brief(client, chat_id)
    assert "Fake brief" in client.get(f"/chats/{chat_id}").text

    started = _post(client, f"/chats/{chat_id}/start", {"action": "plan"})
    assert started.status_code == 303 and started.headers["location"].startswith("/runs/")
    run_id = started.headers["location"].rsplit("/", 1)[1]
    assert calls.calls and calls.calls[-1][1] == run_id and calls.calls[-1][2] == "start"
    with _store(fixture_repo) as store:
        run = store.get_run(run_id)
    assert run.plan_only is True and run.state is RunState.INTAKE
    assert f'href="/runs/{run_id}"' in client.get(f"/chats/{chat_id}").text


def test_chat_queue_adds_the_brief(client: TestClient, fixture_repo: Path) -> None:
    chat_id = _new_chat(client)
    _say(client, chat_id, "ship it")
    _brief(client, chat_id)
    queued = _post(client, f"/chats/{chat_id}/queue")
    assert queued.status_code == 303
    with _store(fixture_repo) as store:
        items = store.list_queue()
    assert items and "Fake brief" in items[-1].idea


def test_chat_actions_refuse_without_a_brief(client: TestClient) -> None:
    chat_id = _new_chat(client)
    assert _post(client, f"/chats/{chat_id}/start", {"action": "run"}).status_code == 303
    page = client.get(f"/chats/{chat_id}")
    assert "no brief yet" in page.text


def test_board_adds_and_removes_queue_items(client: TestClient, fixture_repo: Path) -> None:
    page = client.get("/")
    assert 'action="/queue/add"' in page.text and 'formaction="/runs/new"' in page.text

    added = _post(client, "/queue/add", {"idea": "board item", "priority": "high"})
    assert added.status_code == 303
    page = client.get("/")
    assert "board item" in page.text
    with _store(fixture_repo) as store:
        item = store.list_queue()[0]
    assert f'action="/queue/{item.id}/remove"' in page.text

    assert _post(client, f"/queue/{item.id}/remove").status_code == 303
    with _store(fixture_repo) as store:
        assert store.list_queue() == []


def test_run_now_starts_a_run(client: TestClient, calls: Calls, fixture_repo: Path) -> None:
    response = _post(client, "/runs/new", {"idea": "run me now", "plan_only": "on"})
    assert response.status_code == 303 and response.headers["location"].startswith("/runs/")
    run_id = response.headers["location"].rsplit("/", 1)[1]
    with _store(fixture_repo) as store:
        run = store.get_run(run_id)
    assert run.plan_only is True and run.state is RunState.INTAKE
    assert calls.calls == [(fixture_repo, run_id, "start")]


def test_kpis_page_shows_the_current_build(client: TestClient) -> None:
    page = client.get("/kpis")
    assert page.status_code == 200
    assert "This repo" in page.text and "Median cost / run" in page.text
    assert "No runs yet." in page.text
    assert "No shipped pull requests yet" in page.text  # the PR section keeps its empty state
