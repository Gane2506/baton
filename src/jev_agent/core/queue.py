"""The idea queue: ordering, moves and the status of an item (docs/queue.md).

Items are stored by `Store` (`queue` table). This module holds the rules on top: queue order is
priority (high, normal, low) then position; `move` reorders queued items; and once an item has
a run, its status follows the run (a run approved and finished after the scheduler moved on
shows up as `done` without anyone touching the queue).
"""

from __future__ import annotations

from typing import Literal

from jev_agent.core.models import RunState
from jev_agent.core.store import (
    QUEUE_PRIORITIES,
    NotFoundError,
    QueueItem,
    QueuePriority,
    QueueStatus,
    Store,
)

__all__ = [
    "ACTIVE_STATUSES",
    "QueueError",
    "effective_status",
    "move_item",
    "priority_rank",
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
