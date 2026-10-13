import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jev_agent.core.migrations import MIGRATIONS, SCHEMA_VERSION
from jev_agent.core.models import Idea, RunState
from jev_agent.core.queue import QueueError, effective_status, move_item, run_states, status_for_run
from jev_agent.core.store import NotFoundError, QueueItem, QueueStatus, Store, StoreError


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / ".jev" / "state.db") as s:
        yield s


def _ids(store: Store) -> list[int]:
    return [item.id for item in store.list_queue(["queued"])]


def test_migration_5_from_version_4(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("".join(MIGRATIONS[:4]) + "\nPRAGMA user_version = 4;")
    with Store.open(db_path) as s:
        assert s.list_queue() == []
        assert SCHEMA_VERSION >= 5
        with pytest.raises(sqlite3.IntegrityError):
            s.connection.execute(
                "INSERT INTO queue (idea, priority, position, created_at, updated_at)"
                " VALUES ('x', 'urgent', 0, 'now', 'now')"
            )


def test_add_round_trip(store: Store) -> None:
    item = store.add_queue_item(
        "add search",
        "high",
        plan_only=True,
        learning_profile="work",
        profile="sandbox",
    )
    again = store.get_queue_item(item.id)
    assert again == item
    assert (item.idea, item.priority, item.status, item.run_id) == (
        "add search",
        "high",
        "queued",
        None,
    )
    assert item.plan_only and not item.no_learning
    assert (item.learning_profile, item.profile, item.note) == ("work", "sandbox", None)
    other = store.add_queue_item("b", no_learning=True)
    assert other.no_learning and other.priority == "normal" and other.position > item.position
    with pytest.raises(StoreError):
        store.add_queue_item("c", "urgent")  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        store.get_queue_item(999)


def test_order_is_priority_then_position(store: Store) -> None:
    low = store.add_queue_item("low", "low")
    n1 = store.add_queue_item("n1")
    high = store.add_queue_item("high", "high")
    n2 = store.add_queue_item("n2")
    assert _ids(store) == [high.id, n1.id, n2.id, low.id]
    next_item = store.next_queued()
    assert next_item is not None and next_item.id == high.id


def test_claim_status_delete_and_clear(store: Store, tmp_path: Path) -> None:
    a = store.add_queue_item("a")
    b = store.add_queue_item("b")
    assert store.claim_queue_item(a.id)
    assert not store.claim_queue_item(a.id)  # already running
    run = store.create_run(Idea(text="a", repo_path=tmp_path))
    store.set_queue_status(a.id, "waiting", run_id=run.id, note="awaiting approval")
    got = store.get_queue_item(a.id)
    assert (got.status, got.run_id, got.note) == ("waiting", run.id, "awaiting approval")
    store.set_queue_status(a.id, "done")  # keeps run and note
    assert store.get_queue_item(a.id).run_id == run.id
    assert store.list_queue([]) == []
    assert [i.id for i in store.list_queue(["done"])] == [a.id]
    assert store.clear_queued() == 1  # only b
    assert [i.id for i in store.list_queue()] == [a.id]
    store.delete_queue_item(a.id)
    with pytest.raises(NotFoundError):
        store.delete_queue_item(b.id)
    with pytest.raises(StoreError):
        store.set_queue_status(a.id, "bogus")  # type: ignore[arg-type]


def test_move_within_band(store: Store) -> None:
    a, b, c = (store.add_queue_item(x).id for x in "abc")
    move_item(store, c, "up")
    assert _ids(store) == [a, c, b]
    move_item(store, a, "down")
    assert _ids(store) == [c, a, b]
    move_item(store, b, "top")
    assert _ids(store) == [b, c, a]
    move_item(store, b, "up")  # already first: no change
    assert _ids(store) == [b, c, a]
    move_item(store, a, "down")  # already last
    assert _ids(store) == [b, c, a]


def test_move_across_bands_takes_priority(store: Store) -> None:
    h = store.add_queue_item("h", "high").id
    n = store.add_queue_item("n").id
    low = store.add_queue_item("l", "low").id
    moved = move_item(store, low, "top")
    assert moved.priority == "high"
    assert _ids(store) == [low, h, n]
    moved = move_item(store, h, "down")
    assert moved.priority == "normal"
    assert _ids(store) == [low, n, h]


def test_move_rejects_unknown_and_non_queued(store: Store) -> None:
    a = store.add_queue_item("a").id
    store.claim_queue_item(a)
    with pytest.raises(QueueError, match="running"):
        move_item(store, a, "up")
    with pytest.raises(QueueError, match="not found"):
        move_item(store, 42, "up")


def _item(status: QueueStatus, run_id: str | None) -> QueueItem:
    now = datetime.now(UTC)
    return QueueItem(
        1,
        "x",
        "normal",
        0,
        status,
        run_id,
        False,
        None,
        False,
        None,
        None,
        now,
        now,
    )


def test_status_for_run_and_effective_status() -> None:
    assert status_for_run(RunState.DONE) == "done"
    assert status_for_run(RunState.AWAITING_APPROVAL) == "waiting"
    assert status_for_run(RunState.NEEDS_HUMAN) == "waiting"
    assert status_for_run(RunState.FAILED) == "failed"
    assert status_for_run(RunState.BUDGET_EXCEEDED) == "failed"
    assert status_for_run(RunState.CANCELLED) == "cancelled"
    assert status_for_run(RunState.EXECUTING) == "running"
    # Without a run the stored status stands.
    assert effective_status(_item("queued", None), None) == "queued"
    # An item left waiting by the scheduler follows its run once approved and finished.
    assert effective_status(_item("waiting", "r"), RunState.DONE) == "done"
    assert effective_status(_item("waiting", "r"), RunState.EXECUTING) == "waiting"
    assert effective_status(_item("running", "r"), RunState.EXECUTING) == "running"
    assert effective_status(_item("done", "r"), None) == "done"  # run lookup failed


def test_run_states(store: Store, tmp_path: Path) -> None:
    run = store.create_run(Idea(text="a", repo_path=tmp_path))
    a = store.add_queue_item("a")
    store.set_queue_status(a.id, "waiting", run_id=run.id)
    store.add_queue_item("b")
    assert run_states(store, store.list_queue()) == {run.id: RunState.INTAKE}
