"""Dashboard, phase 4 (issue #368): the run-creation picker on the board,
and the workflow-profile field on the recurring-add form.

These are the browser surfaces for the preset picker (design section
13.1) and the parent-required recurring-add field; the resolver and
snapshot tests live in test_workflow_phase4.py.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from jev_agent.cli.app import app as cli_app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.store import Store
from jev_agent.core.store.records import RecurringTask
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings

cli = CliRunner()


@pytest.fixture
def client(fixture_repo: Path) -> Iterator[TestClient]:
    (fixture_repo / "jev.toml").write_text('[dispatcher]\nprimary = "jev"\n')
    app = create_app(DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})))
    with TestClient(app) as test_client:
        yield test_client


def _csrf(test_client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', test_client.get("/queue").text)
    assert match
    return match.group(1)


def _post(test_client: TestClient, url: str, data: dict[str, str]) -> httpx.Response:
    from typing import cast

    return cast(
        httpx.Response,
        test_client.post(
            url, data={"csrf_token": _csrf(test_client), **data}, follow_redirects=False
        ),
    )


def _recurring_tasks(repo: Path) -> list[RecurringTask]:
    with Store.open(repo / ".jev" / "state.db") as store:
        return store.list_recurring_tasks()


def test_board_picker_shows_presets_behaviour_and_priority(client: TestClient) -> None:
    """Section 13.1: the run-creation form carries the preset picker with
    `default` pre-selected, the documented behaviour shown before
    selection, the assurance preview, the unpriced-role notice, and the
    Accuracy priority row."""
    page = client.get("/")
    assert page.status_code == 200
    text = page.text
    assert 'name="workflow_profile"' in text
    assert 'value="default" selected' in text
    assert 'value="preset:efficiency"' in text
    assert 'value="preset:accuracy"' in text
    assert "plan at most 2" in text
    assert "No role has a configured model price" in text
    assert "quality first, speed second" in text
    assert "Assurance preview:" in text


def test_queue_recurring_add_stores_workflow_profile(
    client: TestClient, fixture_repo: Path
) -> None:
    """Parent-required phase-4 addition: the recurring-add form accepts a
    workflow profile, stored unpinned (pinning happens at each enqueue)."""
    response = _post(
        client,
        "/queue/recurring/add",
        {
            "brief": "weekly tidy",
            "schedule": "weekly",
            "priority": "normal",
            "workflow_profile": "preset:efficiency",
        },
    )
    assert response.status_code in (302, 303)
    tasks = _recurring_tasks(fixture_repo)
    assert len(tasks) == 1
    assert tasks[0].workflow_profile == "preset:efficiency"
    assert "preset:efficiency" in client.get("/queue").text


def test_queue_recurring_add_default_means_no_profile(
    client: TestClient, fixture_repo: Path
) -> None:
    _post(
        client,
        "/queue/recurring/add",
        {
            "brief": "weekly tidy",
            "schedule": "weekly",
            "priority": "normal",
            "workflow_profile": "default",
        },
    )
    tasks = _recurring_tasks(fixture_repo)
    assert len(tasks) == 1
    assert tasks[0].workflow_profile is None


def test_queue_recurring_add_unknown_profile_refused(
    client: TestClient, fixture_repo: Path
) -> None:
    """A reference that does not resolve now refuses at add time instead of
    queueing a task that can never run."""
    _post(
        client,
        "/queue/recurring/add",
        {
            "brief": "weekly tidy",
            "schedule": "weekly",
            "priority": "normal",
            "workflow_profile": "repo:gone",
        },
    )
    assert _recurring_tasks(fixture_repo) == []
    assert "no longer resolves" in client.get("/queue").text


def test_cli_run_help_lists_workflow_profile_flag() -> None:
    """Parent-required phase-4 addition: `jev run --workflow-profile` exists
    and is routed through the existing create_run funnel (see run_cmds).
    With color forced (FORCE_COLOR=1, as in CI), rich styles the option
    name in segments, so compare against the visible text, not the raw
    output (the same pattern as the partial-coverage CLI test)."""
    result = cli.invoke(cli_app, ["run", "--help"])
    assert result.exit_code == 0, result.output
    visible = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--workflow-profile" in visible
    assert "preset:efficiency" in visible


def test_cli_run_with_workflow_profile_flag(
    monkeypatch: pytest.MonkeyPatch, fixture_repo: Path
) -> None:
    """The flag routes through the existing create_run funnel (no separate
    path): a fake-worker run created with the flag records the preset id,
    release, and snapshot caps on the run record."""
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    (fixture_repo / "jev.toml").write_text('[dispatcher]\nprimary = "rules"\n')
    result = cli.invoke(
        cli_app,
        [
            "run",
            "add a module",
            "--workflow-profile",
            "preset:efficiency",
            "--repo",
            str(fixture_repo),
        ],
    )
    assert result.exit_code == 0, result.output
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0].workflow_profile_id == "preset:efficiency"
    assert runs[0].workflow_profile_release == "0.5.0"
    assert runs[0].flow_snapshot is not None
    assert runs[0].flow_snapshot["max_plan_rounds"] == 2
