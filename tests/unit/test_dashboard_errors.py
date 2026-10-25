"""Dashboard error paths: a state database this jev cannot read is a message, not a bare 500.

The CLI turns a database written by a newer jev (`SchemaVersionError`) and an unreadable
database into `CliError`; the web UI must match that (issue #25 parity).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from jev_agent.app.errors import JevError
from jev_agent.core.store import Store
from jev_agent.dashboard import views
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings


def _store(repo: Path) -> Store:
    (repo / ".jev").mkdir(exist_ok=True)
    return Store.open(repo / ".jev" / "state.db")


@pytest.fixture
def client(fixture_repo: Path) -> Iterator[TestClient]:
    app = create_app(DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})))
    with TestClient(app) as client:
        yield client


def test_newer_state_database_shows_a_readable_error(
    client: TestClient, fixture_repo: Path
) -> None:
    with _store(fixture_repo) as store:
        store.connection.execute("PRAGMA user_version = 999")
    page = client.get("/")
    assert page.status_code == 500
    assert "schema version 999" in page.text
    assert "Internal Server Error" not in page.text


def test_corrupt_state_database_shows_a_readable_error(
    client: TestClient, fixture_repo: Path
) -> None:
    with _store(fixture_repo):
        pass  # a valid database first (migrations applied)
    state = fixture_repo / ".jev"
    for name in ("state.db-wal", "state.db-shm"):
        (state / name).unlink(missing_ok=True)
    (state / "state.db").write_bytes(b"this is not sqlite")
    page = client.get("/")
    assert page.status_code == 500
    assert "cannot read the state database" in page.text
    assert "Internal Server Error" not in page.text


# ---- issue #253: the JSON / file error paths must rewrite CLI jargon too --------

_RAW = "No workers are available: enable `[workers.api]` in jev.toml"


def _raise_workers(*args: object, **kwargs: object) -> None:
    raise JevError(_RAW)


def test_chat_work_error_is_friendly(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(views, "open_repo", _raise_workers)
    response = client.get("/chats/nope/work")
    assert response.status_code == 500
    assert response.json()["error"].startswith("No workers are available — turn on")
    assert "jev.toml" not in response.json()["error"]


def test_connections_live_error_is_friendly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "open_repo", _raise_workers)
    response = client.get("/connections/live")
    assert response.status_code == 500
    assert response.json()["error"].startswith("No workers are available — turn on")
    assert "jev.toml" not in response.json()["error"]


def test_run_file_error_is_friendly(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(views, "safe_run_file", _raise_workers)
    page = client.get("/runs/r1/files/log.txt")
    assert page.status_code == 500
    assert "No workers are available — turn on" in page.text
    # The raw CLI jargon must be gone from the error itself (the page chrome
    # legitimately mentions jev.toml in the command palette, so assert on the
    # jargon fragment, not the filename).
    assert "enable `[workers.api]`" not in page.text
