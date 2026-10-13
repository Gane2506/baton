"""CLI error output (#25) and read-only commands (#26): errors go to stderr verbatim, a newer
state DB is a one-line error, and read-only commands build no pipeline, make no HTTP call and
create no `.jev/`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from jev_agent.cli import wiring
from jev_agent.cli.app import app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV, NO_WORKERS_MESSAGE

runner = CliRunner()


def _newer_db(repo: Path) -> None:
    (repo / ".jev").mkdir()
    conn = sqlite3.connect(repo / ".jev" / "state.db")
    conn.execute("PRAGMA user_version = 999")
    conn.close()


def test_errors_go_to_stderr_with_brackets_verbatim(fixture_repo: Path) -> None:
    result = runner.invoke(app, ["ship", "20261013-000000-abcd", "--repo", str(fixture_repo)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "error:" in result.stderr
    assert "`[ship] enabled = true`" in " ".join(result.stderr.split())


def test_no_workers_message_keeps_its_table_names(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    planned = runner.invoke(app, ["run", "add search", "--repo", str(fixture_repo)])
    assert planned.exit_code == 0, planned.output
    run_id = next(w for w in planned.output.split() if w[:8].isdigit() and "-" in w).rstrip(":")
    monkeypatch.delenv(FAKE_WORKERS_ENV)
    monkeypatch.setattr(wiring, "which", lambda cmd: None)  # no claude, no codex
    result = runner.invoke(app, ["approve", run_id, "--repo", str(fixture_repo)])
    out = " ".join(result.output.split())
    assert "`[workers.claude_code]`" in out and "`[workers.api]`" in out
    assert " ".join(NO_WORKERS_MESSAGE.split()) in out


@pytest.mark.parametrize(
    "args",
    [["status"], ["run", "an idea"], ["queue", "list"], ["ship", "sync", "--all"], ["kpi"]],
)
def test_newer_state_db_is_a_one_line_error(fixture_repo: Path, args: list[str]) -> None:
    _newer_db(fixture_repo)
    result = runner.invoke(app, [*args, "--repo", str(fixture_repo)])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)  # no traceback
    assert result.stdout == ""
    text = " ".join(result.stderr.split())
    assert text.startswith("error: database has schema version 999")
    assert "upgrade jev" in text


READ_ONLY = [
    ["status"],
    ["log", "20261013-000000-abcd"],
    ["board"],
    ["queue", "list"],
    ["learning", "list"],
    ["learning", "show"],
    ["quality", "show", "20261013-000000-abcd"],
    ["scheduler", "status"],
    ["chat", "--list"],
    ["kpi"],
    ["ship", "sync", "--all"],
]


@pytest.mark.parametrize("args", READ_ONLY, ids=lambda a: " ".join(a))
def test_read_only_commands_make_no_http_call_and_create_no_state_dir(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    (fixture_repo / "jev.toml").write_text(
        "[models]\nprefer_free = true\n[workers.api]\nenabled = true\n"
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-fake-test-key")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    monkeypatch.setattr(wiring, "catalog_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, [*args, "--repo", str(fixture_repo)])
    assert result.exit_code in (0, 1), result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert calls == []
    assert not (fixture_repo / ".jev").exists()


def test_read_only_status_in_a_fresh_repo_says_no_runs(fixture_repo: Path) -> None:
    result = runner.invoke(app, ["status", "--repo", str(fixture_repo)])
    assert result.exit_code == 0, result.output
    assert "No runs yet" in result.output
    assert not (fixture_repo / ".jev").exists()


def test_writing_commands_still_create_the_state_db(fixture_repo: Path) -> None:
    result = runner.invoke(app, ["queue", "add", "an idea", "--repo", str(fixture_repo)])
    assert result.exit_code == 0, result.output
    assert (fixture_repo / ".jev" / "state.db").is_file()
    listed = runner.invoke(app, ["queue", "list", "--repo", str(fixture_repo)])
    assert listed.exit_code == 0 and "The queue is empty" not in listed.output
