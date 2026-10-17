"""Unit tests for recurring scheduled tasks: schedule parsing/description/next-due
and the recurring-task store API (contract: jev_agent.core.recurrence and
jev_agent.core.store.RecurringTask / add/get/list/pause/resume/remove)."""

import sqlite3
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from jev_agent.core.migrations import SCHEMA_VERSION
from jev_agent.core.recurrence import (
    Schedule,
    ScheduleError,
    describe,
    local_timezone_name,
    next_due,
    parse_schedule,
)
from jev_agent.core.store import NotFoundError, QueuePriority, RecurringTask, Store

TORONTO = ZoneInfo("America/Toronto")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / ".jev" / "state.db") as s:
        yield s


# ---- parse_schedule: valid inputs ----------------------------------------------------


@pytest.mark.parametrize(
    "text, cron",
    [
        ("hourly", "0 * * * *"),
        ("daily", "0 0 * * *"),
        ("weekly", "0 0 * * 0"),
        ("monthly", "0 0 1 * *"),
        ("daily@09:30", "30 9 * * *"),
        ("daily@9:5", "5 9 * * *"),  # non-padded time parts are accepted
        ("weekly@mon-09:00", "0 9 * * 1"),
        ("weekly@SUN-23:59", "59 23 * * 0"),  # day names are case-insensitive
        ("0 9 * * *", "0 9 * * *"),
        ("*/15 * * * *", "*/15 * * * *"),
    ],
)
def test_parse_schedule_valid(text: str, cron: str) -> None:
    sched = parse_schedule(text)
    assert sched.cron == cron
    assert sched.source == text  # the original text is preserved


def test_parse_schedule_kinds() -> None:
    assert parse_schedule("hourly").kind == "hourly"
    assert parse_schedule("daily").kind == "daily"
    assert parse_schedule("weekly").kind == "weekly"
    assert parse_schedule("monthly").kind == "monthly"
    assert parse_schedule("0 9 * * *").kind == "cron"
    assert parse_schedule("*/15 * * * *").kind == "cron"


def test_schedule_is_frozen() -> None:
    sched = parse_schedule("daily")
    with pytest.raises(FrozenInstanceError):
        sched.cron = "x"  # type: ignore[misc]


# ---- parse_schedule: invalid inputs --------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "daily@25:00",  # hour out of range
        "daily@09:60",  # minute out of range
        "daily@9",  # missing minutes
        "weekly@funday-09:00",  # unknown day name
        "weekly@mon-9",  # missing :MM
        "not a cron",
        "0 9 * *",  # 4 fields
        "* * * * * *",  # 6 fields
    ],
)
def test_parse_schedule_invalid(text: str) -> None:
    with pytest.raises(ScheduleError, match="daily@HH:MM"):
        parse_schedule(text)


def test_parse_schedule_invalid_message_lists_valid_formats() -> None:
    with pytest.raises(ScheduleError) as excinfo:
        parse_schedule("bogus")
    message = str(excinfo.value)
    for hint in ("hourly", "daily", "weekly", "monthly", "daily@HH:MM"):
        assert hint in message, f"expected {hint!r} in {message!r}"


def test_schedule_error_is_value_error() -> None:
    assert issubclass(ScheduleError, ValueError)
    with pytest.raises(ValueError):
        parse_schedule("bogus")


# ---- describe ------------------------------------------------------------------------


def test_describe() -> None:
    assert describe(parse_schedule("hourly")) == "Every hour"
    assert describe(parse_schedule("daily")) == "Every day at 00:00"
    assert describe(parse_schedule("daily@09:30")) == "Every day at 09:30"
    assert describe(parse_schedule("weekly")) == "Every week on Sunday at 00:00"
    assert describe(parse_schedule("weekly@mon-09:00")) == "Every week on Monday at 09:00"
    assert describe(parse_schedule("monthly")) == "Every month on the 1st at 00:00"


def test_describe_raw_cron_contains_expression() -> None:
    assert "0 9 * * *" in describe(parse_schedule("0 9 * * *"))
    assert "*/15 * * * *" in describe(parse_schedule("*/15 * * * *"))


def test_local_timezone_name() -> None:
    name = local_timezone_name()
    assert isinstance(name, str) and name


# ---- next_due ------------------------------------------------------------------------


def test_next_due_daily_before_time() -> None:
    after = datetime(2026, 1, 5, 8, 0, tzinfo=TORONTO)
    due = next_due(parse_schedule("daily@09:30"), after, TORONTO)
    assert due == datetime(2026, 1, 5, 9, 30, tzinfo=TORONTO)
    assert due.tzinfo is not None and due > after


def test_next_due_daily_after_time_rolls_to_tomorrow() -> None:
    due = next_due(
        parse_schedule("daily@09:30"), datetime(2026, 1, 5, 10, 0, tzinfo=TORONTO), TORONTO
    )
    assert due == datetime(2026, 1, 6, 9, 30, tzinfo=TORONTO)


def test_next_due_is_strictly_greater_than_after() -> None:
    after = datetime(2026, 1, 5, 9, 30, tzinfo=TORONTO)  # exactly on the schedule
    due = next_due(parse_schedule("daily@09:30"), after, TORONTO)
    assert due == datetime(2026, 1, 6, 9, 30, tzinfo=TORONTO)
    assert due > after


def test_next_due_hourly() -> None:
    due = next_due(parse_schedule("hourly"), datetime(2026, 1, 5, 8, 20, tzinfo=TORONTO), TORONTO)
    assert due == datetime(2026, 1, 5, 9, 0, tzinfo=TORONTO)


def test_next_due_weekly() -> None:
    sched = parse_schedule("weekly@mon-09:00")
    sunday = datetime(2026, 1, 4, 10, 0, tzinfo=TORONTO)
    assert next_due(sched, sunday, TORONTO) == datetime(2026, 1, 5, 9, 0, tzinfo=TORONTO)
    monday_late = datetime(2026, 1, 5, 10, 0, tzinfo=TORONTO)
    assert next_due(sched, monday_late, TORONTO) == datetime(2026, 1, 12, 9, 0, tzinfo=TORONTO)


def test_next_due_monthly() -> None:
    due = next_due(parse_schedule("monthly"), datetime(2026, 1, 15, 12, 0, tzinfo=TORONTO), TORONTO)
    assert due == datetime(2026, 2, 1, 0, 0, tzinfo=TORONTO)


# ---- store CRUD ----------------------------------------------------------------------


def _add(
    store: Store,
    brief: str = "water the plants",
    text: str = "daily@09:30",
    priority: QueuePriority = "normal",
    plan_only: bool = False,
    learning_profile: str | None = None,
    no_learning: bool = False,
    profile: str | None = None,
    quality: str | None = None,
    ship_mode: str | None = None,
) -> RecurringTask:
    return store.add_recurring_task(
        brief,
        parse_schedule(text),
        "America/Toronto",
        datetime(2026, 1, 6, 14, 30, tzinfo=UTC),
        priority=priority,
        plan_only=plan_only,
        learning_profile=learning_profile,
        no_learning=no_learning,
        profile=profile,
        quality=quality,
        ship_mode=ship_mode,
    )


def test_add_get_round_trip(store: Store) -> None:
    task = _add(
        store,
        priority="high",
        plan_only=True,
        learning_profile="work",
        no_learning=True,
        profile="sandbox",
        quality="strict",
        ship_mode="auto",
    )
    assert isinstance(task, RecurringTask)
    got = store.get_recurring_task(task.id)
    assert got == task
    assert (got.brief, got.schedule_source, got.timezone) == (
        "water the plants",
        "daily@09:30",
        "America/Toronto",
    )
    cron = got.schedule.cron if isinstance(got.schedule, Schedule) else got.schedule
    assert cron == "30 9 * * *"
    assert got.next_due_at == datetime(2026, 1, 6, 14, 30, tzinfo=UTC)
    assert got.last_run_at is None and got.active_item_id is None
    assert got.enabled
    assert (got.priority, got.plan_only) == ("high", True)
    assert (got.learning_profile, got.no_learning) == ("work", True)
    assert (got.profile, got.quality, got.ship_mode) == ("sandbox", "strict", "auto")
    assert isinstance(got.created_at, datetime)


def test_add_defaults(store: Store) -> None:
    task = _add(store)
    assert (task.priority, task.plan_only, task.no_learning) == ("normal", False, False)
    assert task.learning_profile is None and task.profile is None
    assert task.quality is None and task.ship_mode is None
    assert task.enabled


def test_list_and_enabled_only_filter(store: Store) -> None:
    a = _add(store, "a")
    b = _add(store, "b", text="hourly")
    assert [t.id for t in store.list_recurring_tasks()] == [a.id, b.id]
    store.set_recurring_enabled(b.id, False)
    assert [t.id for t in store.list_recurring_tasks(enabled_only=True)] == [a.id]
    assert [t.id for t in store.list_recurring_tasks(enabled_only=False)] == [a.id, b.id]


def test_pause_and_resume(store: Store) -> None:
    task = _add(store)
    store.set_recurring_enabled(task.id, False)
    assert not store.get_recurring_task(task.id).enabled
    store.set_recurring_enabled(task.id, True)
    assert store.get_recurring_task(task.id).enabled
    with pytest.raises(NotFoundError):
        store.set_recurring_enabled(999, False)


def test_remove(store: Store) -> None:
    task = _add(store)
    store.remove_recurring_task(task.id)
    with pytest.raises(NotFoundError):
        store.get_recurring_task(task.id)
    with pytest.raises(NotFoundError):
        store.remove_recurring_task(task.id)


def test_get_missing_raises(store: Store) -> None:
    with pytest.raises(NotFoundError):
        store.get_recurring_task(999)


def test_update_after_enqueue_and_clear_active(store: Store) -> None:
    task = _add(store)
    item = store.add_queue_item("water the plants")
    new_due = datetime(2026, 1, 7, 14, 30, tzinfo=UTC)
    ran_at = datetime(2026, 1, 6, 12, 5, tzinfo=UTC)
    store.update_recurring_after_enqueue(
        task.id, next_due_at=new_due, last_run_at=ran_at, active_item_id=item.id
    )
    got = store.get_recurring_task(task.id)
    assert (got.next_due_at, got.last_run_at, got.active_item_id) == (new_due, ran_at, item.id)
    store.clear_recurring_active(task.id)
    cleared = store.get_recurring_task(task.id)
    assert cleared.active_item_id is None
    assert (cleared.next_due_at, cleared.last_run_at) == (new_due, ran_at)
    with pytest.raises(NotFoundError):
        store.update_recurring_after_enqueue(
            999, next_due_at=new_due, last_run_at=ran_at, active_item_id=item.id
        )


# ---- migration -----------------------------------------------------------------------


def test_migration_15_recurring_tasks_table(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    with Store.open(db_path):
        pass  # runs every migration
    assert SCHEMA_VERSION >= 15, f"expected the recurring-tasks migration, got {SCHEMA_VERSION}"
    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(recurring_tasks)")}
    expected = {
        "id",
        "brief",
        "schedule",
        "schedule_source",
        "timezone",
        "next_due_at",
        "last_run_at",
        "enabled",
        "active_item_id",
        "priority",
        "plan_only",
        "learning_profile",
        "no_learning",
        "profile",
        "quality",
        "ship_mode",
        "created_at",
    }
    assert expected <= cols, f"missing columns: {expected - cols}"
