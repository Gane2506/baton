"""The idea queue: ordering, moves and the status of an item (docs/queue.md).

Items are stored by `Store` (`queue` table). This module holds the rules on top: queue order is
priority (high, normal, low) then position; `move` reorders queued items; and once an item has
a run, its status follows the run (a run approved and finished after the scheduler moved on
shows up as `done` without anyone touching the queue).
"""

from __future__ import annotations

from typing import Literal

from jev_agent.core.learning import Learning, LearningError, check_profile_name
from jev_agent.core.models import RunState
from jev_agent.core.store import (
    QUEUE_PRIORITIES,
    QUEUE_STATUSES,
    NotFoundError,
    QueueItem,
    QueuePriority,
    QueueStatus,
    Store,
)
from jev_agent.quality.levels import parse_level

__all__ = [
    "ACTIVE_STATUSES",
    "QueueError",
    "add_idea",
    "effective_status",
    "move_item",
    "priority_rank",
    "queue_counts",
    "remove_item",
    "status_for_run",
]

Move = Literal["up", "down", "top"]

# Statuses `jev queue list` shows without --all.
ACTIVE_STATUSES: tuple[QueueStatus, ...] = ("queued", "running", "waiting")


class QueueError(Exception):
    """A queue command that cannot be carried out (unknown item, item not queued, ...)."""


def priority_rank(priority: QueuePriority) -> int:
    """0 for high, 1 for normal, 2 for low: queue order sorts by this first."""
    return QUEUE_PRIORITIES.index(priority)


def status_for_run(state: RunState) -> QueueStatus:
    """The queue status of an item whose run is in `state`.

    A run waiting on a person (plan approval, NEEDS_HUMAN) is `waiting`; a run still in
    progress, or interrupted and resumable, is `running`.
    """
    if state is RunState.DONE:
        return "done"
    if state in (RunState.AWAITING_APPROVAL, RunState.NEEDS_HUMAN):
        return "waiting"
    if state in (RunState.FAILED, RunState.BUDGET_EXCEEDED):
        return "failed"
    if state is RunState.CANCELLED:
        return "cancelled"
    return "running"


def effective_status(item: QueueItem, run_state: RunState | None) -> QueueStatus:
    """The item's status with its run's current state folded in.

    Without a run (or with a run id that no longer resolves) the stored status stands. An item
    that was interrupted mid-run is stored as `waiting` (it needs `jev resume`); it keeps that
    status while its run sits in an in-progress state.
    """
    if item.run_id is None or run_state is None:
        return item.status
    derived = status_for_run(run_state)
    if derived == "running" and item.status == "waiting":
        return "waiting"
    return derived


def run_states(store: Store, items: list[QueueItem]) -> dict[str, RunState]:
    """The current state of every run linked from `items`."""
    states: dict[str, RunState] = {}
    for item in items:
        if item.run_id is not None and item.run_id not in states:
            try:
                states[item.run_id] = store.get_run(item.run_id).state
            except NotFoundError:
                continue
    return states


def move_item(store: Store, item_id: int, move: Move) -> QueueItem:
    """Move a queued item up or down one place, or to the top of the queue.

    Positions of all queued items are renumbered. When the move crosses into another priority
    band (for example a `normal` item moved above a `high` one), the item takes that band's
    priority, so the stored order and the shown order always agree. Returns the moved item.
    """
    try:
        item = store.get_queue_item(item_id)
    except NotFoundError as exc:
        raise QueueError(str(exc)) from exc
    if item.status != "queued":
        raise QueueError(f"queue item {item_id} is {item.status}; only queued items can move")
    queued = store.list_queue(["queued"])
    index = next(i for i, q in enumerate(queued) if q.id == item_id)
    target = {"up": index - 1, "down": index + 1, "top": 0}[move]
    target = max(0, min(target, len(queued) - 1))
    order = [q for q in queued if q.id != item_id]
    order.insert(target, item)

    rank = priority_rank(item.priority)
    if target > 0:
        rank = max(rank, priority_rank(order[target - 1].priority))
    if target < len(order) - 1:
        rank = min(rank, priority_rank(order[target + 1].priority))
    priorities = {q.id: q.priority for q in order}
    priorities[item_id] = QUEUE_PRIORITIES[rank]
    store.set_queue_order([(q.id, priorities[q.id], pos) for pos, q in enumerate(order)])
    return store.get_queue_item(item_id)


def add_idea(
    store: Store,
    learning: Learning,
    idea: str,
    priority: str = "normal",
    *,
    plan_only: bool = False,
    learning_profile: str | None = None,
    no_learning: bool = False,
    profile: str | None = None,
    quality: str | None = None,
) -> QueueItem:
    """Check the options and add an idea to the queue (`jev queue add`, the dashboard).

    Raises `QueueError` with a user-facing message for an empty idea, both learning options,
    an unknown learning profile, an empty workspace profile, or an unknown priority or level.
    """
    text = idea.strip()
    if not text:
        raise QueueError("the idea is empty")
    if learning_profile is not None and no_learning:
        raise QueueError("--learning and --no-learning cannot be used together")
    if learning_profile is not None:
        try:
            check_profile_name(learning_profile)
        except LearningError as exc:
            raise QueueError(str(exc)) from exc
        if learning.get(learning_profile) is None:
            raise QueueError(
                f"no learning profile named {learning_profile!r}; "
                f"create it with `jev learning create {learning_profile}`"
            )
    if profile is not None and not profile.strip():
        raise QueueError("--profile needs a name")
    rank = next((p for p in QUEUE_PRIORITIES if p == priority), None)
    if rank is None:
        raise QueueError(f"unknown priority {priority!r} (use {', '.join(QUEUE_PRIORITIES)})")
    try:
        level = None if quality is None else parse_level(quality)
    except ValueError as exc:
        raise QueueError(str(exc)) from exc
    return store.add_queue_item(
        text,
        rank,
        plan_only=plan_only,
        learning_profile=learning_profile,
        no_learning=no_learning,
        profile=profile,
        quality=level,
    )


def remove_item(store: Store, item_id: int) -> None:
    """Remove an item (`jev queue remove`, the dashboard); a running item stays. Its run, if it
    has one, is kept."""
    try:
        item = store.get_queue_item(item_id)
    except NotFoundError as exc:
        raise QueueError(str(exc)) from exc
    if item.status == "running":
        raise QueueError(
            f"queue item {item_id} is running; stop the scheduler (Ctrl-C) first, "
            "or cancel its run with `jev cancel`"
        )
    store.delete_queue_item(item_id)


def queue_counts(store: Store) -> dict[QueueStatus, int]:
    """How many items have each status, with their runs' states folded in."""
    items = store.list_queue()
    states = run_states(store, items)
    counts = dict.fromkeys(QUEUE_STATUSES, 0)
    for item in items:
        counts[effective_status(item, states.get(item.run_id or ""))] += 1
    return counts
