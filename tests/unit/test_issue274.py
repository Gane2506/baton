"""Pins for issue #274: Store.set_run_state must validate/coerce its state
arguments instead of persisting a raw string and poisoning the board.

A raw string such as 'planning' used to be written verbatim into the runs
table; `_run_record` then raised building `RunState(row["state"])`, so the
board 500'd on every page until the row was fixed by hand. Only the Python
API could reach it (the CLI and dashboard always pass RunState members), but
a store method that writes poison is a robustness hole.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from jev_agent.core.models import Idea, RunState
from jev_agent.core.store import RunRecord, Store


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / ".jev" / "state.db"


@pytest.fixture
def store(db_path: Path) -> Iterator[Store]:
    with Store.open(db_path) as s:
        yield s


def _make_run(store: Store, tmp_path: Path) -> RunRecord:
    return store.create_run(
        Idea(text="add search", repo_path=tmp_path / "repo", context_files=[Path("a.md")])
    )


def test_lowercase_state_string_rejected_and_board_not_poisoned(
    store: Store, tmp_path: Path
) -> None:
    """The exact scripting error from the issue: 'planning' is not a state."""
    run = _make_run(store, tmp_path)
    with pytest.raises(ValueError, match="invalid run state"):
        store.set_run_state(run.id, "planning")
    # Nothing was written: the run is still INTAKE and the board still lists it.
    assert store.get_run(run.id).state is RunState.INTAKE
    assert [r.id for r in store.list_runs()] == [run.id]


def test_gibberish_state_rejected(store: Store, tmp_path: Path) -> None:
    run = _make_run(store, tmp_path)
    with pytest.raises(ValueError, match="invalid run state"):
        store.set_run_state(run.id, "BOGUS")
    assert store.get_run(run.id).state is RunState.INTAKE


def test_non_string_state_rejected(store: Store, tmp_path: Path) -> None:
    run = _make_run(store, tmp_path)
    with pytest.raises(ValueError, match="invalid run state"):
        store.set_run_state(run.id, cast(Any, 42))
    with pytest.raises(ValueError, match="invalid run state"):
        store.set_run_state(run.id, cast(Any, None))
    assert store.get_run(run.id).state is RunState.INTAKE


def test_valid_state_string_coerced(store: Store, tmp_path: Path) -> None:
    """An exact RunState value passed as a plain string is a state, not poison."""
    run = _make_run(store, tmp_path)
    assert store.set_run_state(run.id, "DONE") is True
    assert store.get_run(run.id).state is RunState.DONE


def test_invalid_resume_state_rejected(store: Store, tmp_path: Path) -> None:
    run = _make_run(store, tmp_path)
    with pytest.raises(ValueError, match="invalid run state"):
        store.set_run_state(run.id, RunState.NEEDS_HUMAN, resume_state="nope")
    assert store.get_run(run.id).state is RunState.INTAKE
    assert store.get_run(run.id).resume_state is None


def test_invalid_unless_member_rejected(store: Store, tmp_path: Path) -> None:
    run = _make_run(store, tmp_path)
    with pytest.raises(ValueError, match="invalid run state"):
        store.set_run_state(run.id, RunState.DONE, unless=["CANCELLED", "nope"])
    assert store.get_run(run.id).state is RunState.INTAKE


def test_valid_string_unless_guard_still_works(store: Store, tmp_path: Path) -> None:
    run = _make_run(store, tmp_path)
    assert store.set_run_state(run.id, RunState.CANCELLED, unless=["DONE"]) is True
    # Guarded: the run is CANCELLED, so the change must not happen.
    assert store.set_run_state(run.id, RunState.DONE, unless=["CANCELLED"]) is False
    assert store.get_run(run.id).state is RunState.CANCELLED
