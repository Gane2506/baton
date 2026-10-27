"""`jev doctor`'s state-database check (issue #333): the schema version the database was
written with, against this jev's, plus integrity — read-only, so the check itself is
the diagnosis the old "delete state.db on update" ritual never had."""

import sqlite3
import subprocess
from pathlib import Path

import pytest

from jev_agent.cli import doctor_cmd
from jev_agent.core.migrations import SCHEMA_VERSION
from jev_agent.core.store import Store


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _check(git_repo: Path) -> dict[str, tuple[str, str]]:
    return {c.name: (c.status, c.detail) for c in doctor_cmd.run_checks(git_repo)}


def _state_db(git_repo: Path) -> Path:
    path = git_repo / ".jev" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_state_db_missing_is_ok(git_repo: Path) -> None:
    status, detail = _check(git_repo)["state database"]
    assert status == "ok"
    assert "not created yet" in detail


def test_state_db_current_schema_is_ok(git_repo: Path) -> None:
    with Store.open(_state_db(git_repo)):
        pass  # creating it applies this jev's migrations
    status, detail = _check(git_repo)["state database"]
    assert status == "ok"
    assert f"schema version {SCHEMA_VERSION}" in detail
    assert "integrity ok" in detail


def test_state_db_newer_schema_fails_and_names_the_fix(git_repo: Path) -> None:
    path = _state_db(git_repo)
    with Store.open(path):
        pass
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    status, detail = _check(git_repo)["state database"]
    assert status == "fail"
    assert f"schema version {SCHEMA_VERSION + 1}" in detail
    assert f"only knows up to {SCHEMA_VERSION}" in detail
    assert "upgrade jev" in detail
    assert "do not delete state.db" in detail


def test_state_db_older_schema_is_a_note(git_repo: Path) -> None:
    path = _state_db(git_repo)
    with Store.open(path):
        pass
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()
    status, detail = _check(git_repo)["state database"]
    assert status == "note"
    assert "will be migrated on first open" in detail


def test_state_db_corrupt_fails(git_repo: Path) -> None:
    _state_db(git_repo).write_bytes(b"this is not a sqlite database at all")
    status, detail = _check(git_repo)["state database"]
    assert status == "fail"
    assert "cannot be read" in detail


def test_state_db_check_never_migrates(git_repo: Path) -> None:
    """Doctor is read-only: a DB one version behind still reads that version after."""
    path = _state_db(git_repo)
    with Store.open(path):
        pass
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()
    _check(git_repo)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION - 1
    conn.close()
