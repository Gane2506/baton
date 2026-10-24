"""Task persistence: upsert from a plan, state, assignment, attempts."""

from __future__ import annotations

import json

from jev_agent.core.events import (
    EventType,
    TaskAssignedPayload,
    TaskStateChangedPayload,
    TasksUpsertedPayload,
)
from jev_agent.core.models import Plan, TaskState

from .base import _TASK_UPDATE_COLUMNS, StoreBase, _check_update_assignments
from .errors import NotFoundError
from .records import TaskRecord
from .rows import _task_record


class TasksMixin(StoreBase):
    """Tasks within a run."""

    def upsert_tasks(self, run_id: str, plan: Plan) -> None:
        """Make the run's task set match `plan`.

        New cards are inserted as PENDING. Cards already present get their card and plan
        position updated but keep state, attempts and spend. Tasks missing from the plan are
        deleted; that fails with `sqlite3.IntegrityError` if attempts or decisions refer to
        them, so a plan can only drop tasks that have not started.
        """
        with self._tx() as db:
            self._require_run(run_id)
            ids = [card.id for card in plan.tasks]
            marks = ", ".join("?" for _ in ids)
            db.execute(
                f"DELETE FROM tasks WHERE run_id = ? AND id NOT IN ({marks})", (run_id, *ids)
            )
            for position, card in enumerate(plan.tasks):
                db.execute(
                    "INSERT INTO tasks (run_id, id, position, card_json, state)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT (run_id, id) DO UPDATE SET"
                    " position = excluded.position, card_json = excluded.card_json",
                    (run_id, card.id, position, card.model_dump_json(), TaskState.PENDING),
                )
            plan_round = db.execute(
                "SELECT plan_round FROM runs WHERE id = ?", (run_id,)
            ).fetchone()["plan_round"]
            self._emit(
                db,
                run_id,
                EventType.TASKS_UPSERTED,
                TasksUpsertedPayload(task_ids=ids, from_plan_round=int(plan_round or 0)),
                parent_run_seq=self._latest_event_seq_locked(db, run_id, EventType.PLAN_SET),
            )

    def get_task(self, run_id: str, task_id: str) -> TaskRecord:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE run_id = ? AND id = ?", (run_id, task_id)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"task {task_id!r} not found in run {run_id!r}")
        return _task_record(row)

    def list_tasks(self, run_id: str) -> list[TaskRecord]:
        """The run's tasks in plan order. Raises NotFoundError for an unknown run."""
        self._require_run(run_id)
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE run_id = ? ORDER BY position", (run_id,)
        )
        return [_task_record(row) for row in rows]

    def set_task_state(
        self,
        run_id: str,
        task_id: str,
        state: TaskState,
        note: str | None = None,
        *,
        parent_run_seq: int | None = None,
    ) -> None:
        """Set the task's state. A non-None `note` replaces the task's note.

        `parent_run_seq` links the causal parent event (the attempt or decision that
        caused this transition) when the caller knows it; the bisect blame walk falls
        back to the task's previous event otherwise.
        """
        with self._tx() as db:
            row = db.execute(
                "SELECT state FROM tasks WHERE run_id = ? AND id = ?", (run_id, task_id)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"task {task_id!r} not found in run {run_id!r}")
            from_state = str(row["state"])
            if note is None:
                db.execute(
                    "UPDATE tasks SET state = ? WHERE run_id = ? AND id = ?",
                    (state, run_id, task_id),
                )
            else:
                db.execute(
                    "UPDATE tasks SET state = ?, note = ? WHERE run_id = ? AND id = ?",
                    (state, note, run_id, task_id),
                )
            self._emit(
                db,
                run_id,
                EventType.TASK_STATE_CHANGED,
                TaskStateChangedPayload(
                    task_id=task_id,
                    from_state=from_state,
                    to_state=str(state),
                    note=note,
                ),
                parent_run_seq=parent_run_seq,
            )

    def set_task_assignment(
        self,
        run_id: str,
        task_id: str,
        state: TaskState,
        worker: str,
        model: str | None,
        effort: str | None,
        note: str | None = None,
    ) -> None:
        """Set who runs the task's next attempt with its new state (and note, if given)."""
        assignments = "state = ?, worker = ?, model = ?, effort = ?"
        params: tuple[object, ...] = (state, worker, model, effort)
        if note is not None:
            assignments, params = assignments + ", note = ?", (*params, note)
        with self._tx() as db:
            cur = db.execute(
                f"UPDATE tasks SET {assignments} WHERE run_id = ? AND id = ?",
                (*params, run_id, task_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"task {task_id!r} not found in run {run_id!r}")
            decision_id: int | None = None
            parent = self._latest_event_seq_locked(
                db, run_id, EventType.DECISION_RECORDED, task_id=task_id
            )
            if parent is not None:
                row = db.execute(
                    "SELECT payload_json FROM run_events WHERE run_id = ? AND run_seq = ?",
                    (run_id, parent),
                ).fetchone()
                if row is not None:
                    decision_id = json.loads(row["payload_json"]).get("decision_id")
            self._emit(
                db,
                run_id,
                EventType.TASK_ASSIGNED,
                TaskAssignedPayload(
                    task_id=task_id,
                    state=str(state),
                    worker=worker,
                    model=model,
                    effort=effort,
                    decision_id=decision_id,
                ),
                parent_run_seq=parent,
            )

    def increment_task_attempts(self, run_id: str, task_id: str) -> int:
        """Add one to the task's attempt counter and return the new value."""
        with self._tx() as db:
            row = db.execute(
                "UPDATE tasks SET attempts = attempts + 1 WHERE run_id = ? AND id = ?"
                " RETURNING attempts",
                (run_id, task_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"task {task_id!r} not found in run {run_id!r}")
        attempts: int = row["attempts"]
        return attempts

    def mark_task_lessons_scored(self, run_id: str, task_id: str) -> None:
        self._update_task(run_id, task_id, "lessons_scored = 1", ())

    def add_task_spend(self, run_id: str, task_id: str, usd: float) -> None:
        """Add to the task's spend only. Prefer `add_spend`, which also updates the run."""
        self._update_task(run_id, task_id, "spend_usd = spend_usd + ?", (usd,))

    def _update_task(
        self, run_id: str, task_id: str, assignments: str, params: tuple[object, ...]
    ) -> None:
        _check_update_assignments(assignments, _TASK_UPDATE_COLUMNS)
        with self._tx() as db:
            cur = db.execute(
                f"UPDATE tasks SET {assignments} WHERE run_id = ? AND id = ?",
                (*params, run_id, task_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"task {task_id!r} not found in run {run_id!r}")
