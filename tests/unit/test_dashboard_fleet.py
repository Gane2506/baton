"""The fleet view: every active agent across runs and servers (`GET /fleet`,
`GET /fleet/live`), through Starlette's in-process test client."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from jev_agent.cli.app import app as cli_app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.config import load_config
from jev_agent.config.schema import JevConfig, RolesConfig
from jev_agent.core.models import RunState
from jev_agent.core.store import Store
from jev_agent.dashboard import views
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings

cli = CliRunner()

SECRET_PROBE = "fleet-probe-not-a-secret"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


@pytest.fixture
def client(fixture_repo: Path) -> Iterator[TestClient]:
    settings = DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"}))
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _config_with_server(repo: Path) -> None:
    """The jev dispatcher (rules fallback) in the repo; one medium-access server for the
    coder in the user config (servers and roles are user-level settings)."""
    (repo / "jev.toml").write_text('[dispatcher]\nprimary = "jev"\n')
    user_config = repo.parent / "xdg" / "jev" / "config.toml"
    user_config.parent.mkdir(parents=True, exist_ok=True)
    user_config.write_text(
        '[dispatcher]\nprimary = "rules"\n'
        '[trust]\nrepos = ["*"]\n'
        '[servers.devbox]\nhost = "devbox.example.test"\naccess = "medium"\n'
        '[roles.coder]\nservers = ["devbox"]\n'
    )


def _add_run(repo: Path, idea: str = "build search & filters") -> str:
    """A run awaiting approval (planned by the fake workers) with one open coder attempt."""
    _config_with_server(repo)
    result = cli.invoke(cli_app, ["run", idea, "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    with Store.open(repo / ".jev" / "state.db") as store:
        run_id = store.list_runs()[0].id
        store.start_attempt(run_id, None, 1, "coder", "claude-code", model="opus-test")
    return run_id


def _age_attempt(repo: Path, run_id: str, days: int = 2) -> None:
    db_path = repo / ".jev" / "state.db"
    db = sqlite3.connect(db_path)
    try:
        (current,) = db.execute(
            "SELECT started_at FROM attempts WHERE run_id = ?", (run_id,)
        ).fetchone()
        old = datetime.fromisoformat(current) - timedelta(days=days)
        db.execute(
            "UPDATE attempts SET started_at = ? WHERE run_id = ?",
            (old.isoformat(timespec="microseconds"), run_id),
        )
        db.commit()
    finally:
        db.close()


def test_fleet_page_lists_active_agent(client: TestClient, fixture_repo: Path) -> None:
    run_id = _add_run(fixture_repo)
    response = client.get("/fleet")
    assert response.status_code == 200
    html = response.text
    assert "<h1>Fleet</h1>" in html
    assert run_id in html
    assert "coder" in html
    assert "devbox" in html and "medium" in html
    assert 'data-live="/fleet/live"' in html


def test_fleet_live_fragment_lists_active_agent(client: TestClient, fixture_repo: Path) -> None:
    run_id = _add_run(fixture_repo)
    response = client.get("/fleet/live")
    assert response.status_code == 200
    assert run_id in response.text


def test_fleet_empty_state(client: TestClient) -> None:
    for url in ("/fleet", "/fleet/live"):
        response = client.get(url)
        assert response.status_code == 200
        assert "No agents running" in response.text


def test_fleet_excludes_stopped_runs(client: TestClient, fixture_repo: Path) -> None:
    run_id = _add_run(fixture_repo)
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        # The attempt is still open, but the run stopped: a dead pipeline, not an agent.
        assert store.set_run_state(run_id, RunState.DONE, reason="test")
    assert client.get("/fleet/live").text.count("No agents running") == 1


def test_fleet_marks_stale_attempts(client: TestClient, fixture_repo: Path) -> None:
    run_id = _add_run(fixture_repo)
    _age_attempt(fixture_repo, run_id)
    with views.open_repo(fixture_repo) as repo:
        (agent,) = views.fleet(repo)
    assert agent.stale
    assert "stale?" in client.get("/fleet/live").text


def test_fleet_never_leaks_secrets(
    client: TestClient, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JEV_FLEET_PROBE", SECRET_PROBE)
    _add_run(fixture_repo)
    for url in ("/fleet", "/fleet/live"):
        html = client.get(url).text
        assert SECRET_PROBE not in html
        # Server hostnames are not shown either: names and tiers only.
        assert "devbox.example.test" not in html


def test_sidebar_nav_has_fleet(client: TestClient) -> None:
    html = client.get("/").text
    assert 'href="/fleet"' in html
    assert ">Fleet<" in html


# ---- fleet roster: the six role cards with per-role prompt injection ------------------------


def _fleet_csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/fleet").text)
    assert match
    return match.group(1)


def _post_addendum(client: TestClient, role: str, value: str) -> httpx.Response:
    response = client.post(
        "/fleet/addendum",
        data={"role": role, "value": value, "csrf_token": _fleet_csrf(client)},
        follow_redirects=False,
    )
    return cast(httpx.Response, response)


def test_fleet_page_links_its_stylesheet(client: TestClient) -> None:
    html = client.get("/fleet").text
    assert 'href="/static/fleet.css"' in html
    css = client.get("/static/fleet.css").text
    assert ".roster-grid" in css


def test_fleet_page_renders_six_role_cards(client: TestClient) -> None:
    html = client.get("/fleet").text
    for title in ("Planner", "Validator", "Coder", "Reviewer", "Code Reviewer", "Learner"):
        assert f"<h3>{title}</h3>" in html
    # One card per role, each with its own additional-prompt form.
    assert html.count('action="/fleet/addendum"') == 6
    assert 'name="value"' in html
    assert "Additional prompt" in html


def test_fleet_roster_shows_working_status(client: TestClient, fixture_repo: Path) -> None:
    _add_run(fixture_repo)
    html = client.get("/fleet").text
    coder = html.split('id="role-coder"')[1].split("</section>")[0]
    assert "Working" in coder
    planner = html.split('id="role-planner"')[1].split("</section>")[0]
    assert "Idle" in planner


def test_fleet_roster_idle_when_nothing_running(client: TestClient) -> None:
    html = client.get("/fleet").text
    assert "No agents running" in html
    assert html.count(">Idle<") == 6


def test_fleet_addendum_save_round_trip(client: TestClient, fixture_repo: Path) -> None:
    _config_with_server(fixture_repo)
    response = _post_addendum(client, "planner", "Always start with a summary.")
    assert response.status_code == 303
    assert load_config(fixture_repo).roles.planner.prompt_addendum == (
        "Always start with a summary."
    )
    # The saved value renders back into the card's textarea.
    html = client.get("/fleet").text
    assert "Always start with a summary." in html
    # Other roles are untouched.
    assert load_config(fixture_repo).roles.coder.prompt_addendum == ""


def test_fleet_addendum_multiline_round_trip(client: TestClient, fixture_repo: Path) -> None:
    _config_with_server(fixture_repo)
    value = "Line one.\n\nLine two."
    assert _post_addendum(client, "code_reviewer", value).status_code == 303
    assert load_config(fixture_repo).roles.code_reviewer.prompt_addendum == value


def test_fleet_addendum_empty_clears(client: TestClient, fixture_repo: Path) -> None:
    _config_with_server(fixture_repo)
    _post_addendum(client, "learner", "Some guidance.")
    assert _post_addendum(client, "learner", "   ").status_code == 303
    assert load_config(fixture_repo).roles.learner.prompt_addendum == ""


def test_fleet_addendum_rejects_unknown_role(client: TestClient) -> None:
    response = _post_addendum(client, "janitor", "do the thing")
    assert response.status_code == 303
    assert response.headers["location"] == "/fleet"


def test_fleet_addendum_rejects_control_characters(client: TestClient, fixture_repo: Path) -> None:
    _config_with_server(fixture_repo)
    response = _post_addendum(client, "coder", "bad\x00char")
    assert response.status_code == 303
    assert load_config(fixture_repo).roles.coder.prompt_addendum == ""


def test_role_roster_uses_schema_blurbs() -> None:
    config = JevConfig()
    cards = views.role_roster(config, [])
    assert [card.role for card in cards] == list(views.ROLE_NAMES)
    assert [card.title for card in cards] == [
        "Planner",
        "Validator",
        "Coder",
        "Reviewer",
        "Code Reviewer",
        "Learner",
    ]
    # Blurbs come from the schema's field descriptions: no second source of truth.
    for card in cards:
        assert card.blurb == RolesConfig.model_fields[card.role].description
    # Working flags follow the fleet agents' roles.
    agent = views.FleetAgent(
        run_id="r",
        run_idea="idea",
        run_state=RunState.EXECUTING,
        task_id=None,
        task_title=None,
        task_state=None,
        role="coder",
        worker="claude_code",
        model=None,
        started_at=datetime.now(UTC),
        stale=False,
        grants=[],
    )
    working = {card.role: card.working for card in views.role_roster(config, [agent])}
    assert working == {
        "planner": False,
        "validator": False,
        "coder": True,
        "reviewer": False,
        "code_reviewer": False,
        "learner": False,
    }
