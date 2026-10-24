"""Idea queue (`jev queue`, `jev scheduler`) and recurring tasks."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime

from jev_agent.config.schema import ShipMode
from jev_agent.core.recurrence import Schedule
from jev_agent.quality.levels import QualityLevel, parse_level

from .base import _QUEUE_UPDATE_COLUMNS, StoreBase, _check_update_assignments
from .errors import NotFoundError, StoreError
from .records import (
    QUEUE_PRIORITIES,
    QUEUE_STATUSES,
    QueueItem,
    QueuePriority,
    QueueStatus,
    RecurringTask,
)
from .rows import _iso, _queue_item, _recurring_task


class QueueMixin(StoreBase):
    """The idea queue and cron-driven recurring tasks."""

    # ---- idea queue (`jev queue`, `jev scheduler`) ---------------------------------------

    def add_queue_item(
        self,
        idea: str,
        priority: QueuePriority = "normal",
        *,
        plan_only: bool = False,
        learning_profile: str | None = None,
        no_learning: bool = False,
        profile: str | None = None,
        quality: QualityLevel | None = None,
        ship_mode: ShipMode | None = None,
    ) -> QueueItem:
        """Append an idea to the queue (last within its priority)."""
        if priority not in QUEUE_PRIORITIES:
            raise StoreError(f"unknown priority {priority!r}")
        if quality is not None:
            quality = parse_level(quality)
        now = self._clock()
        with self._tx() as db:
            row = db.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM queue").fetchone()
            cur = db.execute(
                "INSERT INTO queue (idea, priority, position, plan_only, learning_profile,"
                " no_learning, profile, quality, ship_mode, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    idea,
                    priority,
                    row["p"],
                    int(plan_only),
                    learning_profile,
                    int(no_learning),
                    profile,
                    quality,
                    ship_mode,
                    now,
                    now,
                ),
            )
            if cur.lastrowid is None:
                raise StoreError("queue insert did not return an id")
            item_id = cur.lastrowid
        return self.get_queue_item(item_id)

    def get_queue_item(self, item_id: int) -> QueueItem:
        row = self._conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"queue item {item_id} not found")
        return _queue_item(row)

    def list_queue(self, statuses: Sequence[QueueStatus] | None = None) -> list[QueueItem]:
        """Queue items (only those with a stored status in `statuses`, if given) in queue
        order: priority (high first), then position."""
        sql = "SELECT * FROM queue"
        params: tuple[object, ...] = ()
        if statuses is not None:
            if not statuses:
                return []
            sql += f" WHERE status IN ({', '.join('?' for _ in statuses)})"
            params = tuple(statuses)
        sql += (
            " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,"
            " position, id"
        )
        return [_queue_item(row) for row in self._conn.execute(sql, params)]

    def next_queued(self) -> QueueItem | None:
        """The first queued item in queue order, or None when nothing is queued."""
        items = self.list_queue(["queued"])
        return items[0] if items else None

    def claim_queue_item(self, item_id: int) -> bool:
        """Move a queued item to `running`; False if it is no longer queued (removed, or
        claimed by someone else)."""
        with self._tx() as db:
            cur = db.execute(
                "UPDATE queue SET status = 'running', updated_at = ?"
                " WHERE id = ? AND status = 'queued'",
                (self._clock(), item_id),
            )
        return cur.rowcount == 1

    @contextmanager
    def link_new_run(self, item_id: int) -> Iterator[None]:
        """While active, link the first run created through this connection to queue item
        `item_id` (the scheduler's running item), in the transaction that inserts the run.

        A TEMP trigger: it lives only on this connection (no schema change) and is dropped on
        exit. So a scheduler killed mid-run leaves its item linked to its run, or no run at all.
        """
        self._conn.execute("DROP TRIGGER IF EXISTS temp.jev_link_queue_run")
        self._conn.execute(
            "CREATE TEMP TRIGGER jev_link_queue_run AFTER INSERT ON main.runs BEGIN"
            " UPDATE queue SET run_id = NEW.id"
            f" WHERE id = {int(item_id)} AND status = 'running' AND run_id IS NULL; END"
        )
        try:
            yield
        finally:
            self._conn.execute("DROP TRIGGER IF EXISTS temp.jev_link_queue_run")

    def set_queue_status(
        self,
        item_id: int,
        status: QueueStatus,
        *,
        run_id: str | None = None,
        note: str | None = None,
    ) -> None:
        """Set the item's status. A non-None `run_id` or `note` replaces the stored one."""
        if status not in QUEUE_STATUSES:
            raise StoreError(f"unknown queue status {status!r}")
        assignments = "status = ?"
        params: tuple[object, ...] = (status,)
        if run_id is not None:
            assignments, params = assignments + ", run_id = ?", (*params, run_id)
        if note is not None:
            assignments, params = assignments + ", note = ?", (*params, note)
        self._update_queue_item(item_id, assignments, params)

    def set_queue_order(self, order: Sequence[tuple[int, QueuePriority, int]]) -> None:
        """Set (id, priority, position) for several items in one transaction."""
        with self._tx() as db:
            for item_id, priority, position in order:
                if priority not in QUEUE_PRIORITIES:
                    raise StoreError(f"unknown priority {priority!r}")
                cur = db.execute(
                    "UPDATE queue SET priority = ?, position = ?, updated_at = ? WHERE id = ?",
                    (priority, position, self._clock(), item_id),
                )
                if cur.rowcount == 0:
                    raise NotFoundError(f"queue item {item_id} not found")

    def delete_queue_item(self, item_id: int, *, keep_running: bool = False) -> bool:
        """Delete the item; with `keep_running`, only if it is not `running` (one statement,
        so a scheduler's claim cannot slip in between). False when it was kept."""
        sql = "DELETE FROM queue WHERE id = ?" + (
            " AND status != 'running'" if keep_running else ""
        )
        with self._tx() as db:
            if db.execute(sql, (item_id,)).rowcount == 1:
                return True
            if db.execute("SELECT 1 FROM queue WHERE id = ?", (item_id,)).fetchone() is None:
                raise NotFoundError(f"queue item {item_id} not found")
        return False

    def clear_queued(self) -> int:
        """Delete every item that is still `queued`; returns how many were deleted."""
        with self._tx() as db:
            cur = db.execute("DELETE FROM queue WHERE status = 'queued'")
        return cur.rowcount

    def _update_queue_item(
        self, item_id: int, assignments: str, params: tuple[object, ...]
    ) -> None:
        # The fragment is caller-built, as in `_update_run`/`_update_task` (issue #224):
        # the allowlist is what keeps a future caller from smuggling an arbitrary
        # assignment past review.
        _check_update_assignments(assignments, _QUEUE_UPDATE_COLUMNS)
        with self._tx() as db:
            cur = db.execute(
                f"UPDATE queue SET {assignments}, updated_at = ? WHERE id = ?",
                (*params, self._clock(), item_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"queue item {item_id} not found")

    # ---- recurring tasks (cron-driven queue items, `core.recurrence`) --------------------

    def add_recurring_task(
        self,
        brief: str,
        schedule: Schedule,
        timezone: str,
        next_due_at: datetime,
        *,
        priority: QueuePriority = "normal",
        plan_only: bool = False,
        learning_profile: str | None = None,
        no_learning: bool = False,
        profile: str | None = None,
        quality: str | None = None,
        ship_mode: str | None = None,
    ) -> RecurringTask:
        """Register a recurring task: `brief` runs on `schedule` in `timezone`, first due at
        `next_due_at`. The normalized cron goes in `schedule`, the original text in
        `schedule_source`."""
        if priority not in QUEUE_PRIORITIES:
            raise StoreError(f"unknown priority {priority!r}")
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO recurring_tasks (brief, schedule, schedule_source, timezone,"
                " next_due_at, priority, plan_only, learning_profile, no_learning, profile,"
                " quality, ship_mode, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    brief,
                    schedule.cron,
                    schedule.source,
                    timezone,
                    _iso(next_due_at),
                    priority,
                    int(plan_only),
                    learning_profile,
                    int(no_learning),
                    profile,
                    quality,
                    ship_mode,
                    self._clock(),
                ),
            )
            if cur.lastrowid is None:
                raise StoreError("recurring-task insert did not return an id")
            task_id = cur.lastrowid
        return self.get_recurring_task(task_id)

    def get_recurring_task(self, task_id: int) -> RecurringTask:
        row = self._conn.execute(
            "SELECT * FROM recurring_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"recurring task {task_id} not found")
        return _recurring_task(row)

    def list_recurring_tasks(self, enabled_only: bool = False) -> list[RecurringTask]:
        """All recurring tasks (only enabled ones with `enabled_only`), oldest first."""
        sql = "SELECT * FROM recurring_tasks"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        return [_recurring_task(row) for row in self._conn.execute(sql)]

    def set_recurring_enabled(self, task_id: int, enabled: bool) -> RecurringTask:
        """Enable or disable a recurring task (disabled tasks are skipped by the scheduler)."""
        with self._tx() as db:
            cur = db.execute(
                "UPDATE recurring_tasks SET enabled = ? WHERE id = ?", (int(enabled), task_id)
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"recurring task {task_id} not found")
        return self.get_recurring_task(task_id)

    def update_recurring_after_enqueue(
        self, task_id: int, *, next_due_at: datetime, last_run_at: datetime, active_item_id: int
    ) -> RecurringTask:
        """Record that the task fired at `last_run_at`: it is next due at `next_due_at` and
        `active_item_id` is the queue item it enqueued."""
        with self._tx() as db:
            cur = db.execute(
                "UPDATE recurring_tasks SET next_due_at = ?, last_run_at = ?,"
                " active_item_id = ? WHERE id = ?",
                (_iso(next_due_at), _iso(last_run_at), active_item_id, task_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"recurring task {task_id} not found")
        return self.get_recurring_task(task_id)

    def clear_recurring_active(self, task_id: int) -> RecurringTask:
        """Clear the task's in-flight queue item (its run finished); the schedule stays."""
        with self._tx() as db:
            cur = db.execute(
                "UPDATE recurring_tasks SET active_item_id = NULL WHERE id = ?", (task_id,)
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"recurring task {task_id} not found")
        return self.get_recurring_task(task_id)

    def remove_recurring_task(self, task_id: int) -> None:
        """Delete a recurring task. Raises NotFoundError when it does not exist."""
        with self._tx() as db:
            cur = db.execute("DELETE FROM recurring_tasks WHERE id = ?", (task_id,))
            if cur.rowcount == 0:
                raise NotFoundError(f"recurring task {task_id} not found")
