"""SQLite state store: runs, tasks, attempts and decisions (PLAN.md section 9).

The store only persists and loads state. It knows nothing about which state transitions are
legal or what to do next; that belongs to the pipeline. JSON columns hold pydantic models
written with `model_dump_json` and read back with `model_validate_json`, so round-trips are
exact. Every write runs in its own transaction.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, get_args

from jev_agent.core.migrations import SchemaVersionError, apply_migrations
from jev_agent.core.models import (
    Decision,
    DecisionRequest,
    Idea,
    Plan,
    Role,
    RunState,
    TaskCard,
    TaskState,
    ValidationResult,
    WorkerResult,
)
from jev_agent.quality.levels import QualityLevel, parse_level
from jev_agent.quality.models import GateResult

__all__ = [
    "AttemptRecord",
    "DecisionRecord",
    "NotFoundError",
    "QualityReportRecord",
    "QueueItem",
    "QueuePriority",
    "QueueStatus",
    "RunRecord",
    "SchemaVersionError",
    "Store",
    "StoreError",
    "TaskRecord",
    "new_run_id",
]


class StoreError(Exception):
    """Base class for store errors."""


class NotFoundError(StoreError, LookupError):
    """A run, task or attempt id does not exist."""


@dataclass(frozen=True)
class RunRecord:
    id: str
    idea: Idea
    state: RunState
    plan: Plan | None
    plan_round: int
    spend_usd: float
    created_at: datetime
    updated_at: datetime
    reason: str | None = None  # why the run stopped (NEEDS_HUMAN, FAILED, ...), if it did
    validation: ValidationResult | None = None  # the latest validator verdict
    # For a NEEDS_HUMAN run that `resume` may continue: the state to continue from.
    resume_state: RunState | None = None
    # The learning profile this run reads and writes lessons with; None: learning is off.
    learning_profile: str | None = None
    # What the end-of-run learning step did ("learned: ...", "skipped: ..."); None: not yet.
    learning_outcome: str | None = None
    # The quality gate level this run uses (docs/quality.md); "none" for older runs.
    quality_level: QualityLevel = "none"


@dataclass(frozen=True)
class TaskRecord:
    run_id: str
    id: str
    card: TaskCard
    state: TaskState
    attempts: int
    spend_usd: float
    # Current worker assignment (set at D4, changed by retry/escalate/reassign).
    worker: str | None = None
    model: str | None = None
    effort: str | None = None
    note: str | None = None  # feedback for the next attempt, or why the task failed
    lessons_scored: bool = False  # the lessons injected into its attempts were scored


@dataclass(frozen=True)
class AttemptRecord:
    id: int
    run_id: str
    task_id: str | None  # None for run-level work such as planning
    attempt_no: int
    role: Role
    worker: str
    model: str | None
    effort: str | None
    log_path: Path | None
    started_at: datetime
    finished_at: datetime | None
    result: WorkerResult | None  # None until finish_attempt is called
    lesson_ids: tuple[str, ...] = ()  # lessons injected into this attempt's prompt


@dataclass(frozen=True)
class QualityReportRecord:
    """One quality gate: a task attempt or an integration round (docs/quality.md)."""

    id: int
    run_id: str
    task_id: str | None
    result: GateResult
    report_path: Path | None
    created_at: datetime


@dataclass(frozen=True)
class DecisionRecord:
    id: int
    run_id: str
    task_id: str | None
    request: DecisionRequest
    decision: Decision
    fallback_reason: str | None  # why the rules dispatcher was used instead, if it was
    created_at: datetime


QueuePriority = Literal["high", "normal", "low"]
QueueStatus = Literal["queued", "running", "waiting", "done", "failed", "cancelled"]
QUEUE_PRIORITIES: tuple[QueuePriority, ...] = get_args(QueuePriority)
QUEUE_STATUSES: tuple[QueueStatus, ...] = get_args(QueueStatus)


@dataclass(frozen=True)
class QueueItem:
    """One idea waiting in (or worked through from) the queue; see `core/queue.py`."""

    id: int
    idea: str
    priority: QueuePriority
    position: int
    status: QueueStatus  # as stored; `core.queue.effective_status` folds in the run's state
    run_id: str | None
    plan_only: bool
    learning_profile: str | None  # `--learning NAME`
    no_learning: bool  # `--no-learning`
    profile: str | None  # workspace profile name, passed through to the run
    note: str | None
    created_at: datetime
    updated_at: datetime
    quality: QualityLevel | None = None  # `--quality LEVEL`; None: the config's level


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def new_run_id() -> str:
    """A short, sortable run id: UTC time plus 4 random hex chars, e.g. 20261012-153012-ab12."""
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


class Store:
    """Persistent state for runs. Open with `Store.open(path)`; use as a context manager."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection, for the repo-scope lesson tables (`core/learning.py`)."""
        return self._conn

    @classmethod
    def open(cls, path: Path) -> Store:
        """Open (creating if needed) the database at `path` and apply pending migrations."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit, transactions are managed explicitly by _tx().
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            apply_migrations(conn)
        except BaseException:
            conn.close()
            raise
        return cls(conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Run the block in one write transaction; roll back on any error."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()

    # ---- runs ----------------------------------------------------------------------------

    def create_run(
        self,
        idea: Idea,
        learning_profile: str | None = None,
        quality_level: QualityLevel = "none",
    ) -> RunRecord:
        now = _now()
        for _ in range(5):  # retry on the (very unlikely) id collision within one second
            run_id = new_run_id()
            try:
                with self._tx() as db:
                    db.execute(
                        "INSERT INTO runs (id, idea_json, state, created_at, updated_at,"
                        " learning_profile, quality_level) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            idea.model_dump_json(),
                            RunState.INTAKE,
                            now,
                            now,
                            learning_profile,
                            quality_level,
                        ),
                    )
            except sqlite3.IntegrityError:
                continue
            return self.get_run(run_id)
        raise StoreError("could not generate a unique run id")

    def get_run(self, run_id: str) -> RunRecord:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"run {run_id!r} not found")
        return _run_record(row)

    def list_runs(self) -> list[RunRecord]:
        """All runs, newest first."""
        rows = self._conn.execute("SELECT * FROM runs ORDER BY created_at DESC, rowid DESC")
        return [_run_record(row) for row in rows]

    def set_run_state(
        self,
        run_id: str,
        state: RunState,
        reason: str | None = None,
        resume_state: RunState | None = None,
    ) -> None:
        """Set the run's state. `reason` (why it stopped) replaces any previous reason.

        `resume_state` is where `resume` may continue a stopped run from; every state change
        replaces it, so it is only set while the run stays stopped.
        """
        self._update_run(
            run_id, "state = ?, reason = ?, resume_state = ?", (state, reason, resume_state)
        )

    def set_run_validation(self, run_id: str, validation: ValidationResult | None) -> None:
        value = None if validation is None else validation.model_dump_json()
        self._update_run(run_id, "validation_json = ?", (value,))

    def set_run_plan(self, run_id: str, plan: Plan, plan_round: int) -> None:
        self._update_run(
            run_id, "plan_json = ?, plan_round = ?", (plan.model_dump_json(), plan_round)
        )

    def set_run_learning(self, run_id: str, profile: str | None) -> None:
        """Change the learning profile the run uses from now on (None: learning off)."""
        self._update_run(run_id, "learning_profile = ?", (profile,))

    def set_run_learning_outcome(self, run_id: str, outcome: str | None) -> None:
        self._update_run(run_id, "learning_outcome = ?", (outcome,))

    def set_run_quality(self, run_id: str, level: QualityLevel) -> None:
        """Change the quality level the run uses from now on (`jev resume --quality`)."""
        self._update_run(run_id, "quality_level = ?", (parse_level(level),))

    def add_run_spend(self, run_id: str, usd: float) -> None:
        self._update_run(run_id, "spend_usd = spend_usd + ?", (usd,))

    def add_spend(self, run_id: str, task_id: str | None, usd: float) -> None:
        """Add `usd` to the run total and, if `task_id` is given, to that task, atomically."""
        with self._tx() as db:
            cur = db.execute(
                "UPDATE runs SET spend_usd = spend_usd + ?, updated_at = ? WHERE id = ?",
                (usd, _now(), run_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"run {run_id!r} not found")
            if task_id is not None:
                cur = db.execute(
                    "UPDATE tasks SET spend_usd = spend_usd + ? WHERE run_id = ? AND id = ?",
                    (usd, run_id, task_id),
                )
                if cur.rowcount == 0:
                    raise NotFoundError(f"task {task_id!r} not found in run {run_id!r}")

    def _update_run(self, run_id: str, assignments: str, params: tuple[object, ...]) -> None:
        with self._tx() as db:
            cur = db.execute(
                f"UPDATE runs SET {assignments}, updated_at = ? WHERE id = ?",
                (*params, _now(), run_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"run {run_id!r} not found")

    # ---- tasks ---------------------------------------------------------------------------

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
        self, run_id: str, task_id: str, state: TaskState, note: str | None = None
    ) -> None:
        """Set the task's state. A non-None `note` replaces the task's note."""
        if note is None:
            self._update_task(run_id, task_id, "state = ?", (state,))
        else:
            self._update_task(run_id, task_id, "state = ?, note = ?", (state, note))

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
        self._update_task(run_id, task_id, assignments, params)

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
        with self._tx() as db:
            cur = db.execute(
                f"UPDATE tasks SET {assignments} WHERE run_id = ? AND id = ?",
                (*params, run_id, task_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"task {task_id!r} not found in run {run_id!r}")

    # ---- attempts ------------------------------------------------------------------------

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
                " log_path, started_at, lesson_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    task_id,
                    attempt_no,
                    role,
                    worker,
                    model,
                    effort,
                    None if log_path is None else str(log_path),
                    _now(),
                    json.dumps(list(lesson_ids)) if lesson_ids else None,
                ),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def finish_attempt(self, attempt_id: int, result: WorkerResult) -> None:
        """Store the worker's result. The log path is taken from `result.raw_log_path`."""
        with self._tx() as db:
            cur = db.execute(
                "UPDATE attempts SET status = ?, result_json = ?, cost_usd = ?, log_path = ?,"
                " finished_at = ? WHERE id = ?",
                (
                    result.status,
                    result.model_dump_json(),
                    result.cost_usd,
                    str(result.raw_log_path),
                    _now(),
                    attempt_id,
                ),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"attempt {attempt_id} not found")

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

    # ---- decisions -----------------------------------------------------------------------

    def record_decision(
        self,
        run_id: str,
        task_id: str | None,
        request: DecisionRequest,
        decision: Decision,
        fallback_reason: str | None = None,
    ) -> int:
        """Store one dispatcher call; returns the decision id."""
        with self._tx() as db:
            self._require_run(run_id)
            if task_id is not None:
                self._require_task(run_id, task_id)
            cur = db.execute(
                "INSERT INTO decisions (run_id, task_id, point, request_json, decision_json,"
                " dispatcher, model, cost_usd, latency_ms, fallback_reason, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    task_id,
                    request.point,
                    request.model_dump_json(),
                    decision.model_dump_json(),
                    decision.dispatcher,
                    decision.model,
                    decision.cost_usd,
                    decision.latency_ms,
                    fallback_reason,
                    _now(),
                ),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def list_decisions(self, run_id: str) -> list[DecisionRecord]:
        """All decisions of a run, oldest first."""
        self._require_run(run_id)
        rows = self._conn.execute("SELECT * FROM decisions WHERE run_id = ? ORDER BY id", (run_id,))
        return [_decision_record(row) for row in rows]

    # ---- quality gate reports ------------------------------------------------------------

    def add_quality_report(
        self, run_id: str, result: GateResult, report_path: Path | None = None
    ) -> int:
        """Store one gate's result; returns its id."""
        with self._tx() as db:
            self._require_run(run_id)
            cur = db.execute(
                "INSERT INTO quality_reports (run_id, task_id, scope, number, level, blocked,"
                " result_json, report_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    result.task_id,
                    result.scope,
                    result.number,
                    result.level,
                    int(result.blocked),
                    result.model_dump_json(),
                    None if report_path is None else str(report_path),
                    _now(),
                ),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def list_quality_reports(self, run_id: str) -> list[QualityReportRecord]:
        """The run's gates, oldest first."""
        self._require_run(run_id)
        rows = self._conn.execute(
            "SELECT * FROM quality_reports WHERE run_id = ? ORDER BY id", (run_id,)
        )
        return [_quality_record(row) for row in rows]

    # ---- settings (set by `jev learning`) --------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_setting(self, key: str, value: str | None) -> None:
        """Store `value` under `key`; None removes the setting."""
        with self._tx() as db:
            if value is None:
                db.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                db.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)"
                    " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

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
    ) -> QueueItem:
        """Append an idea to the queue (last within its priority)."""
        if priority not in QUEUE_PRIORITIES:
            raise StoreError(f"unknown priority {priority!r}")
        if quality is not None:
            quality = parse_level(quality)
        now = _now()
        with self._tx() as db:
            row = db.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM queue").fetchone()
            cur = db.execute(
                "INSERT INTO queue (idea, priority, position, plan_only, learning_profile,"
                " no_learning, profile, quality, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    idea,
                    priority,
                    row["p"],
                    int(plan_only),
                    learning_profile,
                    int(no_learning),
                    profile,
                    quality,
                    now,
                    now,
                ),
            )
            assert cur.lastrowid is not None
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
                (_now(), item_id),
            )
        return cur.rowcount == 1

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
                    (priority, position, _now(), item_id),
                )
                if cur.rowcount == 0:
                    raise NotFoundError(f"queue item {item_id} not found")

    def delete_queue_item(self, item_id: int) -> None:
        with self._tx() as db:
            cur = db.execute("DELETE FROM queue WHERE id = ?", (item_id,))
            if cur.rowcount == 0:
                raise NotFoundError(f"queue item {item_id} not found")

    def clear_queued(self) -> int:
        """Delete every item that is still `queued`; returns how many were deleted."""
        with self._tx() as db:
            cur = db.execute("DELETE FROM queue WHERE status = 'queued'")
        return cur.rowcount

    def _update_queue_item(
        self, item_id: int, assignments: str, params: tuple[object, ...]
    ) -> None:
        with self._tx() as db:
            cur = db.execute(
                f"UPDATE queue SET {assignments}, updated_at = ? WHERE id = ?",
                (*params, _now(), item_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"queue item {item_id} not found")

    # ---- helpers -------------------------------------------------------------------------

    def _require_run(self, run_id: str) -> None:
        if self._conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
            raise NotFoundError(f"run {run_id!r} not found")

    def _require_task(self, run_id: str, task_id: str) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM tasks WHERE run_id = ? AND id = ?", (run_id, task_id)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"task {task_id!r} not found in run {run_id!r}")


def _time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _run_record(row: sqlite3.Row) -> RunRecord:
    plan_json = row["plan_json"]
    validation_json = row["validation_json"]
    return RunRecord(
        id=row["id"],
        idea=Idea.model_validate_json(row["idea_json"]),
        state=RunState(row["state"]),
        plan=None if plan_json is None else Plan.model_validate_json(plan_json),
        plan_round=row["plan_round"],
        spend_usd=row["spend_usd"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        reason=row["reason"],
        validation=None
        if validation_json is None
        else ValidationResult.model_validate_json(validation_json),
        resume_state=None if row["resume_state"] is None else RunState(row["resume_state"]),
        learning_profile=row["learning_profile"],
        learning_outcome=row["learning_outcome"],
        quality_level=row["quality_level"] or "none",
    )


def _task_record(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        run_id=row["run_id"],
        id=row["id"],
        card=TaskCard.model_validate_json(row["card_json"]),
        state=TaskState(row["state"]),
        attempts=row["attempts"],
        spend_usd=row["spend_usd"],
        worker=row["worker"],
        model=row["model"],
        effort=row["effort"],
        note=row["note"],
        lessons_scored=bool(row["lessons_scored"]),
    )


def _attempt_record(row: sqlite3.Row) -> AttemptRecord:
    result_json = row["result_json"]
    log_path = row["log_path"]
    return AttemptRecord(
        id=row["id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        attempt_no=row["attempt_no"],
        role=row["role"],
        worker=row["worker"],
        model=row["model"],
        effort=row["effort"],
        log_path=None if log_path is None else Path(log_path),
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=_time(row["finished_at"]),
        result=None if result_json is None else WorkerResult.model_validate_json(result_json),
        lesson_ids=tuple(json.loads(row["lesson_ids"])) if row["lesson_ids"] else (),
    )


def _decision_record(row: sqlite3.Row) -> DecisionRecord:
    return DecisionRecord(
        id=row["id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        request=DecisionRequest.model_validate_json(row["request_json"]),
        decision=Decision.model_validate_json(row["decision_json"]),
        fallback_reason=row["fallback_reason"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _quality_record(row: sqlite3.Row) -> QualityReportRecord:
    return QualityReportRecord(
        id=row["id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        result=GateResult.model_validate_json(row["result_json"]),
        report_path=None if row["report_path"] is None else Path(row["report_path"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _queue_item(row: sqlite3.Row) -> QueueItem:
    return QueueItem(
        id=row["id"],
        idea=row["idea"],
        priority=row["priority"],
        position=row["position"],
        status=row["status"],
        run_id=row["run_id"],
        plan_only=bool(row["plan_only"]),
        learning_profile=row["learning_profile"],
        no_learning=bool(row["no_learning"]),
        profile=row["profile"],
        note=row["note"],
        quality=row["quality"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
