"""Scheduler integration for recurring tasks: `Scheduler._pump_recurring` enqueues
due, enabled recurring tasks, advances their schedule, and skips when the
previous occurrence is still queued/running/waiting."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jev_agent.core.models import Idea, RunState
from jev_agent.core.recurrence import parse_schedule
from jev_agent.core.scheduler import Scheduler, SchedulerOptions
from jev_agent.core.store import QueueItem, RunRecord, Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / ".jev" / "state.db") as s:
        yield s


async def _noop_start(item: QueueItem) -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        id="noop",
        idea=Idea(text=item.idea, repo_path=Path(".")),
        state=RunState.DONE,
        plan=None,
        plan_round=0,
        spend_usd=0.0,
        created_at=now,
        updated_at=now,
    )


def _no_sleep(seconds: float) -> None:
    raise AssertionError("unexpected sleep")


def _scheduler(
    store: Store, now_dt: datetime, logs: list[str] | None = None
) -> tuple[Scheduler, dict[str, datetime]]:
    """A scheduler whose clock is fixed; the returned cell lets tests advance time."""
    now_cell = {"t": now_dt}
    log: Callable[[str], None] = logs.append if logs is not None else (lambda m: None)
    sched = Scheduler(
        store,
        _noop_start,
        SchedulerOptions(),
        sleep=_no_sleep,
        log=log,
        now=lambda: now_cell["t"],
    )
    return sched, now_cell


def _add_due(store: Store, brief: str = "hourly check") -> int:
    task = store.add_recurring_task(
        brief,
        parse_schedule("hourly"),
        "America/Toronto",
        datetime(2026, 1, 5, 11, 0, tzinfo=UTC),  # in the past relative to the tests' now
    )
    return task.id


def test_pump_enqueues_due_task(store: Store) -> None:
    now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    sched, _ = _scheduler(store, now)
    task_id = _add_due(store)

    assert sched._pump_recurring() == 1

    items = store.list_queue(["queued"])
    assert len(items) == 1 and items[0].idea == "hourly check"
    got = store.get_recurring_task(task_id)
    assert got.active_item_id == items[0].id
    assert got.last_run_at is not None
    assert got.next_due_at is not None and got.next_due_at > now  # advanced past now


def test_pump_enqueues_multiple_due_tasks(store: Store) -> None:
    now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    sched, _ = _scheduler(store, now)
    _add_due(store, "first")
    _add_due(store, "second")

    assert sched._pump_recurring() == 2
    assert {i.idea for i in store.list_queue(["queued"])} == {"first", "second"}


@pytest.mark.parametrize("state", ["queued", "running"])
def test_pump_skips_while_previous_occurrence_active(store: Store, state: str) -> None:
    logs: list[str] = []
    now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    sched, _ = _scheduler(store, now, logs)
    task_id = _add_due(store)
    item = store.add_queue_item("hourly check")
    if state == "running":
        assert store.claim_queue_item(item.id)
    # Still due, but the previous occurrence has not finished yet.
    store.update_recurring_after_enqueue(
        task_id,
        next_due_at=datetime(2026, 1, 5, 11, 0, tzinfo=UTC),
        last_run_at=now,
        active_item_id=item.id,
    )

    assert sched._pump_recurring() == 0
    assert len(store.list_queue()) == 1  # no double-enqueue
    assert logs, "expected a skip log when the previous occurrence is still active"


def test_pump_reenqueues_after_done_and_time_passes(store: Store) -> None:
    now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    sched, now_cell = _scheduler(store, now)
    task_id = _add_due(store)

    assert sched._pump_recurring() == 1
    first = store.list_queue(["queued"])[0]
    # Next due is 13:00 UTC, so an immediate second pump finds nothing due.
    assert sched._pump_recurring() == 0

    store.set_queue_status(first.id, "done")
    now_cell["t"] = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)  # past the new next_due_at
    assert sched._pump_recurring() == 1

    items = store.list_queue(["queued"])
    assert len(items) == 1 and items[0].id != first.id
    got = store.get_recurring_task(task_id)
    assert got.active_item_id == items[0].id
    assert got.next_due_at is not None and got.next_due_at > now_cell["t"]


def test_pump_ignores_disabled_task(store: Store) -> None:
    now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    sched, _ = _scheduler(store, now)
    task_id = _add_due(store)
    store.set_recurring_enabled(task_id, False)

    assert sched._pump_recurring() == 0
    assert store.list_queue() == []


def test_pump_ignores_not_due_task(store: Store) -> None:
    now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    sched, _ = _scheduler(store, now)
    store.add_recurring_task(
        "hourly check",
        parse_schedule("hourly"),
        "America/Toronto",
        datetime(2026, 1, 6, 12, 0, tzinfo=UTC),  # in the future
    )

    assert sched._pump_recurring() == 0
    assert store.list_queue() == []
