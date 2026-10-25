"""Dashboard run page: the event timeline, `jev bisect` and `jev events backfill`
(issue #254, workstream D).

The timeline reuses the CLI's one-line summaries (`core.events.summarize`), the
bisect form runs `core.bisect.bisect_run`, and the backfill button runs
`core.backfill.backfill_run` — all through the same store the CLI uses.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from starlette.testclient import TestClient

from jev_agent.app.context import state_db_path
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.models import Idea, Plan, RunState, TaskCard, TaskState
from jev_agent.core.store import Store
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.runner import RunAction
from jev_agent.dashboard.security import DashboardSettings
from jev_agent.quality.models import GateResult


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


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/chats").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str] | None = None) -> httpx.Response:
    return cast(
        httpx.Response,
        client.post(
            url,
            data={**(data or {}), "csrf_token": _csrf(client)},
            follow_redirects=False,
        ),
    )


def _seed_run(
    repo: Path,
    *,
    events: bool = True,
    blocked_gate: bool = False,
    failed_task: bool = False,
) -> str:
    """A run with a few events in the fixture repo's state DB."""
    with Store.open(state_db_path(repo), events_enabled=events) as store:
        run = store.create_run(Idea(text="add search", repo_path=repo))
        store.set_run_state(run.id, RunState.PLANNING, "seed")
        plan = Plan(
            summary="s",
            tasks=[TaskCard(id="T1", title="title T1", description="d", test_command="pytest")],
        )
        store.set_run_plan(run.id, plan, 1)
        store.upsert_tasks(run.id, plan)
        if blocked_gate:
            store.add_quality_report(
                run.id, GateResult(level="basic", scope="integration", blocked=True)
            )
        if failed_task:
            store.set_task_state(run.id, "T1", TaskState.FAILED)
        return run.id


def _timeline(client: TestClient, run_id: str) -> str:
    """The run page's event-timeline section."""
    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200, page.text
    assert 'id="event-timeline"' in page.text
    return page.text


def test_run_page_shows_event_timeline(client: TestClient, fixture_repo: Path) -> None:
    run_id = _seed_run(fixture_repo)
    text = _timeline(client, run_id)
    assert "run.created" in text
    assert "run.state_changed" in text
    assert "INTAKE → PLANNING" in text
    assert "plan set: round 1, 1 tasks" in text
    assert "tasks upserted: T1" in text
    assert "4 events" in text
    # The causal parent link: the tasks event names the plan event as its cause.
    assert 'href="#event-3"' in text
    # The bisect form is part of the timeline section.
    assert 'action="/runs/' in text and 'name="bisect"' in text


def test_run_page_timeline_empty_state_offers_backfill(
    client: TestClient, fixture_repo: Path
) -> None:
    run_id = _seed_run(fixture_repo, events=False)
    text = _timeline(client, run_id)
    assert "No events recorded for this run yet" in text
    assert f'action="/runs/{run_id}/events/backfill"' in text
    assert "run.created" not in text


def test_backfill_button_rebuilds_the_timeline(client: TestClient, fixture_repo: Path) -> None:
    run_id = _seed_run(fixture_repo, events=False)
    response = _post(client, f"/runs/{run_id}/events/backfill")
    assert response.status_code == 303
    assert response.headers["location"] == f"/runs/{run_id}"
    page = client.get(f"/runs/{run_id}", follow_redirects=True)
    assert "backfilled" in page.text and "synthetic events" in page.text
    assert "run.created" in page.text
    # Backfilled events are marked synthetic.
    assert "synthetic" in page.text
    # Backfill runs once: a second POST is refused, not duplicated.
    again = _post(client, f"/runs/{run_id}/events/backfill")
    assert again.status_code == 303
    page = client.get(f"/runs/{run_id}", follow_redirects=True)
    assert "already has events" in page.text
    with Store.open(state_db_path(fixture_repo)) as store:
        assert len(store.list_events(run_id)) == len(
            [e for e in store.list_events(run_id) if e.synthetic]
        )


def test_backfill_requires_csrf(client: TestClient, fixture_repo: Path) -> None:
    run_id = _seed_run(fixture_repo, events=False)
    response = client.post(f"/runs/{run_id}/events/backfill", follow_redirects=False)
    assert response.status_code == 403


def test_backfill_unknown_run_redirects_to_404(client: TestClient) -> None:
    """An unknown run id is well-formed, so the action flashes the error and the
    redirect lands on the run page's own 404 (the dashboard's action convention)."""
    response = _post(client, "/runs/20261025-120000-abcd/events/backfill")
    assert response.status_code == 303
    assert response.headers["location"] == "/runs/20261025-120000-abcd"
    page = client.get("/runs/20261025-120000-abcd")
    assert page.status_code == 404
    assert "not found" in page.text


def test_bisect_gate_finds_the_blocked_gate(client: TestClient, fixture_repo: Path) -> None:
    run_id = _seed_run(fixture_repo, blocked_gate=True)
    page = client.get(f"/runs/{run_id}?bisect=gate")
    assert page.status_code == 200
    assert "First bad" in page.text
    assert "first blocked gate" in page.text
    assert "gate.checked" in page.text
    assert "first bad" in page.text  # the bad event's badge
    # The blame path is rendered as the chain table.
    assert 'id="bisect-' in page.text


def test_bisect_gate_with_no_blocked_gate_looks_fine(
    client: TestClient, fixture_repo: Path
) -> None:
    run_id = _seed_run(fixture_repo)
    page = client.get(f"/runs/{run_id}?bisect=gate")
    assert page.status_code == 200
    assert "No bad event found" in page.text
    assert "a blocked gate" in page.text


def test_bisect_task_finds_the_failed_task(client: TestClient, fixture_repo: Path) -> None:
    run_id = _seed_run(fixture_repo, failed_task=True)
    page = client.get(f"/runs/{run_id}?bisect=task&bisect_task=T1")
    assert page.status_code == 200
    assert "First bad" in page.text
    assert "T1: PENDING → FAILED" in page.text
    assert "task.state_changed" in page.text


def test_bisect_mark_reports_an_unknown_event(client: TestClient, fixture_repo: Path) -> None:
    run_id = _seed_run(fixture_repo)
    page = client.get(f"/runs/{run_id}?bisect=mark&bisect_mark=99")
    assert page.status_code == 200
    assert "no event #99 in this run" in page.text


def test_bisect_mark_must_be_a_number(client: TestClient, fixture_repo: Path) -> None:
    run_id = _seed_run(fixture_repo)
    page = client.get(f"/runs/{run_id}?bisect=mark&bisect_mark=soon")
    assert page.status_code == 200
    assert "mark needs an event number" in page.text


def test_bisect_unknown_criterion_is_an_error(client: TestClient, fixture_repo: Path) -> None:
    run_id = _seed_run(fixture_repo)
    page = client.get(f"/runs/{run_id}?bisect=vibes")
    assert page.status_code == 200
    assert "unknown bisect criterion" in page.text


def test_timeline_shows_the_live_fragment_without_events(
    client: TestClient, fixture_repo: Path
) -> None:
    """The 3-second live poll stays light: it never carries the timeline."""
    run_id = _seed_run(fixture_repo)
    fragment = client.get(f"/runs/{run_id}/live")
    assert fragment.status_code == 200
    assert "event-timeline" not in fragment.text
    assert "run.created" not in fragment.text


def test_events_disabled_repo_says_so(client: TestClient, fixture_repo: Path) -> None:
    """A repo with `[events] enabled = false` shows the disabled note — and no
    backfill button, since backfilling would write into a log that is off."""
    run_id = _seed_run(fixture_repo, events=False)
    (fixture_repo / "jev.toml").write_text("[events]\nenabled = false\n")
    text = _timeline(client, run_id)
    assert "Event log disabled" in text
    assert "events/backfill" not in text
    assert "run.created" not in text
