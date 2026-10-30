"""Attempt persistence: start/finish attempts, test annotations."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from jev_agent.core.events import (
    AttemptFinishedPayload,
    AttemptStartedPayload,
    AttemptTestedPayload,
    EventType,
)
from jev_agent.core.models import Role, WorkerResult

from .base import StoreBase
from .errors import NotFoundError, StoreError
from .records import AttemptRecord
from .rows import _attempt_record


class AttemptsMixin(StoreBase):
    """Worker attempts."""

    def start_attempt(
        self,
        run_id: str,
        task_id: str | None,
        attempt_no: int,
        role: Role,
        worker: str,
        model: str | None = None,
        effort: str | None = None,
        log_path: Path | None = None,
        lesson_ids: Sequence[str] = (),
        stage: str | None = None,
    ) -> int:
        """Record that a worker started; returns the attempt id for `finish_attempt`.

        `lesson_ids` are the learned lessons injected into the attempt's prompt.
        """
        with self._tx() as db:
            self._require_run(run_id)
            if task_id is not None:
                self._require_task(run_id, task_id)
            cur = db.execute(
                "INSERT INTO attempts (run_id, task_id, attempt_no, role, worker, model, effort,"
                " log_path, started_at, lesson_ids, stage)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    task_id,
                    attempt_no,
                    role,
                    worker,
                    model,
                    effort,
                    None if log_path is None else str(log_path),
                    self._clock(),
                    json.dumps(list(lesson_ids)) if lesson_ids else None,
                    stage,
                ),
            )
            if cur.lastrowid is None:
                raise StoreError("attempt insert did not return an id")
            self._emit(
                db,
                run_id,
                EventType.ATTEMPT_STARTED,
                AttemptStartedPayload(
                    attempt_id=cur.lastrowid,
                    task_id=task_id,
                    attempt_no=attempt_no,
                    role=str(role),
                    worker=worker,
                    model=model,
                    lesson_ids=list(lesson_ids),
                    log_path=None if log_path is None else str(log_path),
                ),
            )
            return cur.lastrowid

    def finish_attempt(self, attempt_id: int, result: WorkerResult) -> None:
        """Store the worker's result. The log path is taken from `result.raw_log_path`."""
        with self._tx() as db:
            row = db.execute(
                "SELECT run_id, task_id, started_at FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"attempt {attempt_id} not found")
            now = self._clock()
            cur = db.execute(
                "UPDATE attempts SET status = ?, result_json = ?, cost_usd = ?, log_path = ?,"
                " finished_at = ? WHERE id = ?",
                (
                    result.status,
                    result.model_dump_json(),
                    result.cost_usd,
                    str(result.raw_log_path),
                    now,
                    attempt_id,
                ),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"attempt {attempt_id} not found")
            duration_s: float | None = None
            if row["started_at"]:
                try:
                    duration_s = (
                        datetime.fromisoformat(now) - datetime.fromisoformat(row["started_at"])
                    ).total_seconds()
                except ValueError:
                    duration_s = None
            parent = self._latest_event_seq_locked(
                db, str(row["run_id"]), EventType.ATTEMPT_STARTED, attempt_id=attempt_id
            )
            self._emit(
                db,
                str(row["run_id"]),
                EventType.ATTEMPT_FINISHED,
                AttemptFinishedPayload(
                    attempt_id=attempt_id,
                    task_id=row["task_id"],
                    status=str(result.status),
                    summary=result.summary,
                    cost_usd=result.cost_usd,
                    duration_s=duration_s,
                ),
                parent_run_seq=parent,
            )

    def emit_attempt_tests(
        self, run_id: str, task_id: str | None, attempt_no: int, tests: str
    ) -> int | None:
        """Record a coder attempt's test outcome (`attempt.tested`: "passed" | "failed" |
        "skipped"). The attempt row already exists (`finish_attempt` ran first); this is a
        standalone annotation event, and what `jev bisect --test` scans for."""
        row = self._conn.execute(
            "SELECT id FROM attempts WHERE run_id = ? AND IFNULL(task_id, '') = IFNULL(?, '')"
            " AND attempt_no = ? ORDER BY id DESC LIMIT 1",
            (run_id, task_id, attempt_no),
        ).fetchone()
        if row is None:
            return None
        attempt_id = int(row["id"])
        return self.emit(
            run_id,
            EventType.ATTEMPT_TESTED,
            AttemptTestedPayload(attempt_id=attempt_id, task_id=task_id, tests=tests),
            parent_run_seq=self.latest_event_seq(
                run_id, EventType.ATTEMPT_FINISHED, attempt_id=attempt_id
            ),
        )

    def get_attempt(self, attempt_id: int) -> AttemptRecord:
        row = self._conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"attempt {attempt_id} not found")
        return _attempt_record(row)

    def list_attempts(self, run_id: str) -> list[AttemptRecord]:
        """All attempts of a run (run-level and task-level), oldest first."""
        self._require_run(run_id)
        rows = self._conn.execute("SELECT * FROM attempts WHERE run_id = ? ORDER BY id", (run_id,))
        return [_attempt_record(row) for row in rows]
