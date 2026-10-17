"""Board states: every RunState lands in exactly one column, cards speak plain
language, and the dashboard's states legend covers everything."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from jev_agent.cli.board_cmds import (
    COLUMN_FOR_STATE,
    COLUMNS,
    RUN_STATE_PHRASE,
    TASK_STATE_PHRASE,
    Card,
    build_board,
    state_legend,
)
from jev_agent.core.models import Idea, RunState, TaskState
from jev_agent.core.store import Store


def test_every_run_state_has_a_column() -> None:
    assert set(COLUMN_FOR_STATE) == set(RunState)


def test_every_state_has_a_plain_phrase() -> None:
    assert set(RUN_STATE_PHRASE) == set(RunState)
    assert set(TASK_STATE_PHRASE) == set(TaskState)
    # no enum shouting leaks into the phrases
    for state, phrase in RUN_STATE_PHRASE.items():
        assert phrase != state.value, state


def test_state_legend_covers_every_column_and_state() -> None:
    legend = state_legend()
    assert [column for column, _ in legend] == list(COLUMNS)
    mentioned = " ".join(states for _, states in legend)
    for phrase in RUN_STATE_PHRASE.values():
        assert phrase in mentioned


def _board_with_states(tmp_path: Path, states: list[RunState]) -> dict[str, list[Card]]:
    with Store.open(tmp_path / "state.db") as store:
        for i, state in enumerate(states):
            run = store.create_run(Idea(text=f"idea {i}", repo_path=tmp_path))
            store.set_run_state(run.id, state)
        runs = store.list_runs()
        return build_board(runs, {}, [], datetime.now(UTC))


def test_per_step_gates_land_in_awaiting_approval(tmp_path: Path) -> None:
    board = _board_with_states(
        tmp_path,
        [
            RunState.AWAITING_PLANNING,
            RunState.AWAITING_VALIDATING,
            RunState.AWAITING_INTEGRATING,
            RunState.AWAITING_SHIPPING,
        ],
    )
    titles = [c.title for c in board["Awaiting approval"]]
    assert titles == ["idea 3", "idea 2", "idea 1", "idea 0"]  # newest first
    details = {c.title: c.detail for c in board["Awaiting approval"]}
    assert details["idea 0"] == "waiting for your approval to plan"
    assert details["idea 1"] == "waiting for your approval to validate"
    assert details["idea 2"] == "waiting for your approval to integrate"
    assert details["idea 3"] == "waiting for your approval to ship"


def test_blocked_cards_use_plain_language(tmp_path: Path) -> None:
    board = _board_with_states(
        tmp_path,
        [RunState.NEEDS_HUMAN, RunState.FAILED, RunState.BUDGET_EXCEEDED, RunState.CANCELLED],
    )
    details = sorted(c.detail for c in board["Blocked"])
    assert details == ["cancelled", "failed", "over budget", "waiting on you"]
