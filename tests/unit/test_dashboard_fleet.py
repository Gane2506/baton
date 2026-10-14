"""The fleet view: every active agent across runs and servers (`GET /fleet`,
`GET /fleet/live`), through Starlette's in-process test client."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from jev_agent.cli.app import app as cli_app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
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
