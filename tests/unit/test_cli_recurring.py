"""CLI tests for recurring scheduled tasks: `jev queue add --repeat` and
`jev queue recurring list|pause|resume|remove`, plus the Recurring section in
`jev queue list`."""

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli.app import app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.store import Store

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # These tests model the user's own repository: trust it, so its jev.toml may set the
    # guarded keys (servers, tools, permissions; docs/security.md).
    trust = tmp_path / "xdg" / "jev" / "config.toml"
    trust.parent.mkdir(parents=True, exist_ok=True)
    with trust.open("a") as fh:  # keep the suite's user config (tests/conftest.py)
        fh.write('[trust]\nrepos = ["*"]\n')
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


def _jev(repo: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--repo", str(repo)])
    return result.exit_code, " ".join(result.output.split())


def _store(repo: Path) -> Store:
    return Store.open(repo / ".jev" / "state.db")


def _recurring_task_id(output: str) -> int:
    match = re.search(r"recurring task #?(\d+)", output)
    assert match, output
    return int(match.group(1))


# ---- jev queue add --repeat ------------------------------------------------------------------


def test_queue_add_repeat_creates_recurring_task(fixture_repo: Path) -> None:
    code, output = _jev(fixture_repo, "queue", "add", "nightly backup", "--repeat", "daily@09:30")
    assert code == 0, output
    task_id = _recurring_task_id(output)
    with _store(fixture_repo) as store:
        task = store.get_recurring_task(task_id)
    assert task.brief == "nightly backup"
    assert task.schedule_source == "daily@09:30"
    assert task.enabled


def test_queue_add_repeat_accepts_shorthands_and_cron(fixture_repo: Path) -> None:
    for text in ("hourly", "weekly@mon-09:00", "*/15 * * * *"):
        code, output = _jev(fixture_repo, "queue", "add", "x", "--repeat", text)
        assert code == 0, output
        _recurring_task_id(output)
    with _store(fixture_repo) as store:
        assert len(store.list_recurring_tasks()) == 3


def test_queue_add_repeat_rejects_bad_schedule(fixture_repo: Path) -> None:
    for text in ("bogus", "0 9 * *"):
        code, output = _jev(fixture_repo, "queue", "add", "x", "--repeat", text)
        assert code != 0
        assert "daily@HH:MM" in output, output


# ---- jev queue recurring ---------------------------------------------------------------------


def test_recurring_list_pause_resume_remove(fixture_repo: Path) -> None:
    _jev(fixture_repo, "queue", "add", "morning standup notes", "--repeat", "daily@09:30")
    _jev(fixture_repo, "queue", "add", "weekly review", "--repeat", "weekly@mon-09:00")

    code, output = _jev(fixture_repo, "queue", "recurring", "list")
    assert code == 0, output
    assert "morning standup notes" in output and "weekly review" in output

    code, output = _jev(fixture_repo, "queue", "recurring", "pause", "1")
    assert code == 0, output
    with _store(fixture_repo) as store:
        assert not store.get_recurring_task(1).enabled

    code, output = _jev(fixture_repo, "queue", "recurring", "resume", "1")
    assert code == 0, output
    with _store(fixture_repo) as store:
        assert store.get_recurring_task(1).enabled

    code, output = _jev(fixture_repo, "queue", "recurring", "remove", "2")
    assert code == 0, output
    code, output = _jev(fixture_repo, "queue", "recurring", "list")
    assert code == 0, output
    assert "weekly review" not in output and "morning standup notes" in output

    code, output = _jev(fixture_repo, "queue", "recurring", "pause", "99")
    assert code != 0
    code, output = _jev(fixture_repo, "queue", "recurring", "remove", "99")
    assert code != 0


def test_queue_list_shows_recurring_section(fixture_repo: Path) -> None:
    _jev(fixture_repo, "queue", "add", "morning standup notes", "--repeat", "daily@09:30")
    code, output = _jev(fixture_repo, "queue", "list")
    assert code == 0, output
    # The recurring section is rendered as a rich table titled "recurring" (lowercase);
    # match case-insensitively so the test pins the section's presence, not its case.
    assert "recurring" in output.lower()
    assert "morning standup notes" in output
