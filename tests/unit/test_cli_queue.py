import json
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from jev_agent.cli import scheduler_cmds
from jev_agent.cli.app import app
from jev_agent.cli.board_cmds import (
    COLUMNS,
    DONE_LIMIT,
    build_board,
    current_task,
    format_age,
    render_board,
)
from jev_agent.cli.scheduler_cmds import ProfileUnsupportedError, apply_profile
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.models import Idea, Plan, RunState, TaskCard, TaskState
from jev_agent.core.store import Store

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def _jev(repo: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--repo", str(repo)])
    return result.exit_code, " ".join(result.output.split())


def _store(repo: Path) -> Store:
    return Store.open(repo / ".jev" / "state.db")


# ---- jev queue -------------------------------------------------------------------------------


def test_queue_add_list_move_remove_clear(fixture_repo: Path) -> None:
    assert _jev(fixture_repo, "queue", "list")[1].startswith("The queue is empty")
    code, output = _jev(fixture_repo, "queue", "add", "first idea")
    assert code == 0 and "queued item 1 (normal)" in output
    _jev(fixture_repo, "queue", "add", "second idea", "--plan-only", "--no-learning")
    _jev(fixture_repo, "queue", "add", "urgent idea", "--priority", "high", "--profile", "box")
    code, output = _jev(fixture_repo, "queue", "list")
    assert code == 0
    assert output.index("urgent idea") < output.index("first idea") < output.index("second")
    assert "plan-only no-learning" in output and "profile=box" in output

    code, output = _jev(fixture_repo, "queue", "move", "2", "--top")
    assert code == 0 and "number 1" in output and "normal -> high" in output
    with _store(fixture_repo) as store:
        assert [i.id for i in store.list_queue(["queued"])] == [2, 3, 1]
    code, output = _jev(fixture_repo, "queue", "move", "2", "--down")
    assert code == 0 and "number 2" in output
    assert _jev(fixture_repo, "queue", "move", "2")[0] == 1
    assert _jev(fixture_repo, "queue", "move", "2", "--up", "--down")[0] == 1
    code, output = _jev(fixture_repo, "queue", "move", "9", "--up")
    assert code == 1 and "not found" in output

    assert _jev(fixture_repo, "queue", "remove", "3") == (0, "removed item 3")
    assert _jev(fixture_repo, "queue", "remove", "3")[0] == 1
    code, output = _jev(fixture_repo, "queue", "clear")
    assert code == 1 and "--yes" in output
    assert _jev(fixture_repo, "queue", "clear", "--yes") == (0, "removed 2 queued item(s)")


def test_queue_add_validates_flags(fixture_repo: Path) -> None:
    code, output = _jev(fixture_repo, "queue", "add", "x", "--learning", "a", "--no-learning")
    assert code == 1 and "cannot be used together" in output
    code, output = _jev(fixture_repo, "queue", "add", "x", "--learning", "nope")
    assert code == 1 and "jev learning create nope" in output
    code, output = _jev(fixture_repo, "queue", "add", "x", "--priority", "urgent")
    assert code == 2
    code, output = _jev(fixture_repo, "queue", "add", "  ")
    assert code == 1 and "empty" in output


def test_queue_remove_refuses_running(fixture_repo: Path) -> None:
    _jev(fixture_repo, "queue", "add", "x")
    with _store(fixture_repo) as store:
        store.claim_queue_item(1)
    code, output = _jev(fixture_repo, "queue", "remove", "1")
    assert code == 1 and "running" in output


# ---- jev scheduler ---------------------------------------------------------------------------


def test_scheduler_runs_queue_and_status_follows_approval(fixture_repo: Path) -> None:
    _jev(fixture_repo, "queue", "add", "add search")
    _jev(fixture_repo, "queue", "add", "add sort", "--plan-only")
    code, output = _jev(fixture_repo, "scheduler", "start", "--once")
    assert code == 0, output
    assert "AWAITING_APPROVAL -> waiting" in output and "started 1 run(s)" in output
    assert not (fixture_repo / ".jev" / "scheduler.lock").exists()

    code, output = _jev(fixture_repo, "scheduler", "status")
    assert code == 0 and "not running" in output
    assert "1 queued" in output and "1 waiting" in output

    code, output = _jev(fixture_repo, "scheduler", "start", "--max-runs", "1", "--poll", "0.1")
    assert code == 0 and "reached --max-runs 1" in output
    with _store(fixture_repo) as store:
        waiting = store.list_queue(["waiting"])
        run_id = waiting[0].run_id
    assert len(waiting) == 2 and run_id is not None

    code, output = _jev(fixture_repo, "approve", run_id)
    assert code == 0 and "DONE" in output
    code, output = _jev(fixture_repo, "queue", "list")
    assert "add search" not in output  # finished items are hidden without --all
    code, output = _jev(fixture_repo, "queue", "list", "--all")
    assert "done" in output and run_id in output


def test_scheduler_needs_workers_and_respects_lock(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = fixture_repo / ".jev" / "scheduler.lock"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text(
        json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started_at": "t"})
    )
    _jev(fixture_repo, "queue", "add", "x")
    code, output = _jev(fixture_repo, "scheduler", "start", "--once")
    assert code == 1 and "another scheduler is running" in output
    code, output = _jev(fixture_repo, "scheduler", "status")
    assert code == 0 and f"running (pid {os.getpid()}" in output
    lock.unlink()

    monkeypatch.delenv(FAKE_WORKERS_ENV)
    (fixture_repo / "jev.toml").write_text(
        "".join(f"[workers.{n}]\nenabled = false\n" for n in ("claude_code", "codex", "api"))
    )
    code, output = _jev(fixture_repo, "scheduler", "start", "--once")
    assert code == 1 and "No workers are available" in output


def test_scheduler_daily_budget_flag(fixture_repo: Path) -> None:
    _jev(fixture_repo, "queue", "add", "x")
    with _store(fixture_repo) as store:
        run = store.create_run(Idea(text="earlier", repo_path=fixture_repo))
        store.add_run_spend(run.id, 1.0)
    code, output = _jev(fixture_repo, "scheduler", "start", "--daily-budget-usd", "1")
    assert code == 0 and "daily budget reached" in output and "started 0 run(s)" in output


def test_profile_hook(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _store(fixture_repo) as store:
        plain = store.add_queue_item("a")
        boxed = store.add_queue_item("b", profile="box")
    assert apply_profile(plain) == {}

    async def with_profile(ctx: object, idea: str, *, profile: str | None = None) -> None:
        return None

    async def without_profile(ctx: object, idea: str) -> None:
        return None

    monkeypatch.setattr(scheduler_cmds, "start_run", with_profile)
    assert apply_profile(boxed) == {"profile": "box"}
    monkeypatch.setattr(scheduler_cmds, "start_run", without_profile)
    with pytest.raises(ProfileUnsupportedError, match="box"):
        apply_profile(boxed)


# ---- jev board -------------------------------------------------------------------------------


def _plan() -> Plan:
    cards = [TaskCard(id=f"T{i}", title=f"t{i}", description="d") for i in (1, 2)]
    return Plan(summary="s", tasks=cards)


def test_build_board_groups_runs_and_queue(tmp_path: Path) -> None:
    with Store.open(tmp_path / "state.db") as store:
        store.add_queue_item("queued idea", "high", plan_only=True)
        states = {
            "planning": RunState.PLANNING,
            "approval": RunState.AWAITING_APPROVAL,
            "running": RunState.EXECUTING,
            "integrating": RunState.INTEGRATING,
            "done": RunState.DONE,
            "human": RunState.NEEDS_HUMAN,
            "cancelled": RunState.CANCELLED,
            "budget": RunState.BUDGET_EXCEEDED,
        }
        for name, state in states.items():
            run = store.create_run(Idea(text=name, repo_path=tmp_path))
            store.set_run_state(run.id, state)
            if name == "running":
                store.upsert_tasks(run.id, _plan())
                store.set_task_state(run.id, "T1", TaskState.DONE)
                store.set_task_state(run.id, "T2", TaskState.RUNNING)
                store.add_run_spend(run.id, 0.5)
        runs = store.list_runs()
        tasks = {r.id: store.list_tasks(r.id) for r in runs}
        board = build_board(runs, tasks, store.list_queue(["queued"]), datetime.now(UTC))
    titles = {name: [c.title for c in cards] for name, cards in board.items()}
    assert titles == {
        "Backlog": ["queued idea"],
        "Planning": ["planning"],
        "Awaiting approval": ["approval"],
        "Running": ["integrating", "running"],
        "Done": ["done"],
        "Blocked": ["budget", "cancelled", "human"],
    }
    running = next(c for c in board["Running"] if c.title == "running")
    assert running.detail == "T2 RUNNING" and running.spend_usd == 0.5
    assert board["Backlog"][0].detail == "high plan-only" and board["Backlog"][0].ref == "queue #1"
    assert board["Blocked"][2].detail == "NEEDS_HUMAN"
    assert board["Running"][0].detail == "INTEGRATING"  # no tasks: show the run state


def test_board_limits_done_unless_all(tmp_path: Path) -> None:
    with Store.open(tmp_path / "state.db") as store:
        for i in range(DONE_LIMIT + 2):
            run = store.create_run(Idea(text=f"d{i}", repo_path=tmp_path))
            store.set_run_state(run.id, RunState.DONE)
        runs = store.list_runs()
    now = datetime.now(UTC)
    assert len(build_board(runs, {}, [], now)["Done"]) == DONE_LIMIT
    assert len(build_board(runs, {}, [], now, show_all=True)["Done"]) == DONE_LIMIT + 2


def test_age_and_current_task() -> None:
    now = datetime(2026, 10, 12, 12, tzinfo=UTC)
    assert format_age(now - timedelta(seconds=5), now) == "5s"
    assert format_age(now - timedelta(minutes=12), now) == "12m"
    assert format_age(now - timedelta(hours=3, minutes=5), now) == "3h"
    assert format_age(now - timedelta(days=2), now) == "2d"
    assert format_age(now + timedelta(seconds=5), now) == "0s"
    assert current_task([]) == ""


def _render(width: int, tmp_path: Path) -> str:
    with Store.open(tmp_path / "state.db") as store:
        store.add_queue_item("a very long idea " * 10)
        run = store.create_run(Idea(text="[bold]markup[/bold] idea", repo_path=tmp_path))
        store.set_run_state(run.id, RunState.NEEDS_HUMAN)
        from jev_agent.cli.board_cmds import load_board

        board = load_board(store, datetime.now(UTC))
    console = Console(width=width, record=True, color_system=None)
    console.print(render_board(board, width))
    return console.export_text()


def test_board_renders_narrow_stacked(tmp_path: Path) -> None:
    text = _render(40, tmp_path)
    lines = text.splitlines()
    assert all(len(line) <= 40 for line in lines)
    positions = [text.index(name) for name in COLUMNS]
    assert positions == sorted(positions)  # stacked, in column order
    assert "[bold]markup[/bold]" in text  # ideas are shown literally


def test_board_renders_wide_side_by_side(tmp_path: Path) -> None:
    text = _render(160, tmp_path)
    header = next(line for line in text.splitlines() if "Backlog" in line)
    assert all(name in header for name in COLUMNS)


def test_board_cli(fixture_repo: Path) -> None:
    _jev(fixture_repo, "queue", "add", "board idea")
    code, output = _jev(fixture_repo, "board")
    assert code == 0 and "Backlog (1)" in output and "board idea" in output
    code, output = _jev(fixture_repo, "board", "--all")
    assert code == 0


def test_board_watch_exits_on_ctrl_c(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    calls: list[float] = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) > 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", sleep)
    code, output = _jev(fixture_repo, "board", "--watch", "--interval", "0.5")
    assert code == 0 and calls == [0.5, 0.5] and "Backlog" in output
