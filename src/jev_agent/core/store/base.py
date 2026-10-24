"""Shared store base: connection lifecycle, transactions and helpers.

`StoreBase` owns the connection, the `_tx()` transaction helper, the
`_require_*` existence guards and the settings table, and declares the
internal interface the domain mixins implement (`_emit`, `_update_run`,
`_update_task`, ...). It never touches a domain table itself.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import BaseModel

from jev_agent.core.events import ACTOR_PIPELINE, EventType
from jev_agent.core.migrations import apply_migrations, configure_connection

from .errors import NotFoundError


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def new_run_id() -> str:
    """A short, sortable run id: UTC time plus 4 random hex chars, e.g. 20261012-153012-ab12."""
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _is_runs_id_collision(exc: sqlite3.IntegrityError) -> bool:
    """True when the error is a PK/UNIQUE collision on `runs.id` (and nothing else).

    SQLite reports it as `UNIQUE constraint failed: runs.id`. Matching the full
    phrase matters: a prefix match on `runs.id` would also catch e.g.
    `NOT NULL constraint failed: runs.idea_json`.
    """
    return "UNIQUE constraint failed: runs.id" in str(exc)


# Columns `_update_run` / `_update_task` / `_update_queue_item` may assign (issue #224,
# extended to queue items in #232). The helpers take a raw SQL fragment, so without
# this allowlist any future caller could smuggle an arbitrary assignment (or a second
# statement's worth of SQL) past review — the values stay bound parameters, but the
# column list is the injection surface. Keep these in sync with the callers; a column
# added here needs a caller that sets it, and a caller that sets a new column needs
# an entry here.
_RUN_UPDATE_COLUMNS = frozenset(
    {"learning_profile", "learning_outcome", "quality_level", "spend_usd"}
)
_TASK_UPDATE_COLUMNS = frozenset({"lessons_scored", "spend_usd"})
_QUEUE_UPDATE_COLUMNS = frozenset({"status", "run_id", "note"})


def _check_update_assignments(assignments: str, allowed: frozenset[str]) -> None:
    """Reject an `_update_*` fragment that assigns outside `allowed`.

    Each comma-separated piece must be `<column> = <expression>` with the
    column allowlisted and the expression one of the shapes the callers use:
    a bound parameter (`?`), an integer literal (`1`), or `<column> +|- ?`.
    Anything else (an unknown column, a missing `=`, a `;`-smuggled second
    statement, a function call) raises `ValueError` before any SQL runs.
    """
    for piece in assignments.split(","):
        match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*", piece, flags=re.DOTALL)
        if match is None:
            raise ValueError(f"refusing UPDATE fragment outside the allowlist: {assignments!r}")
        column, expr = match.group(1), match.group(2)
        if column not in allowed:
            raise ValueError(f"refusing UPDATE fragment outside the allowlist: {assignments!r}")
        if expr == "?" or re.fullmatch(r"-?\d+", expr):
            continue
        ref = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*[+-]\s*\?", expr)
        if ref is None or ref.group(1) not in allowed:
            raise ValueError(f"refusing UPDATE fragment outside the allowlist: {assignments!r}")


class StoreBase(ABC):
    """Connection and transaction plumbing shared by every store mixin."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], str] | None = None,
        events_enabled: bool = True,
    ) -> None:
        self._conn = conn
        self._clock = _now if clock is None else clock
        # Kill switch (`[events] enabled = false`): no event is emitted and the event
        # commands report the log as disabled. The snapshot tables are unaffected.
        self._events_enabled = events_enabled

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection, for the repo-scope lesson tables (`core/learning`)."""
        return self._conn

    @property
    def events_enabled(self) -> bool:
        """Whether the event log is being written (`[events] enabled`)."""
        return self._events_enabled

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        clock: Callable[[], str] | None = None,
        events_enabled: bool = True,
    ) -> Self:
        """Open (creating if needed) the database at `path` and apply pending migrations."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit, transactions are managed explicitly by _tx().
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            configure_connection(conn)
            apply_migrations(conn)
        except BaseException:
            conn.close()
            raise
        return cls(conn, clock=clock, events_enabled=events_enabled)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
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

    # ---- internal interface (implemented by the domain mixins) -------------------

    @abstractmethod
    def _emit(
        self,
        db: sqlite3.Connection,
        run_id: str,
        event_type: EventType,
        payload: BaseModel,
        *,
        actor: str = ACTOR_PIPELINE,
        parent_run_seq: int | None = None,
        synthetic: bool = False,
    ) -> int | None:
        """Append one event inside the caller's `_tx()` block (see `events.py`)."""
        ...

    @abstractmethod
    def emit(
        self,
        run_id: str,
        event_type: EventType,
        payload: BaseModel,
        *,
        actor: str = ACTOR_PIPELINE,
        parent_run_seq: int | None = None,
        synthetic: bool = False,
    ) -> int | None:
        """Append an event in its own transaction (see `events.py`)."""
        ...

    @abstractmethod
    def latest_event_seq(
        self,
        run_id: str,
        event_type: EventType,
        *,
        task_id: str | None = None,
        attempt_id: int | None = None,
    ) -> int | None:
        """Newest `run_seq` of `event_type` (see `events.py`)."""
        ...

    @staticmethod
    @abstractmethod
    def _latest_event_seq_locked(
        db: sqlite3.Connection,
        run_id: str,
        event_type: EventType,
        *,
        task_id: str | None = None,
        attempt_id: int | None = None,
    ) -> int | None:
        """`latest_event_seq` on an explicit connection (see `events.py`)."""
        ...

    @abstractmethod
    def _update_run(self, run_id: str, assignments: str, params: tuple[object, ...]) -> None:
        """Update run columns (see `runs.py`)."""
        ...

    @abstractmethod
    def _update_task(
        self, run_id: str, task_id: str, assignments: str, params: tuple[object, ...]
    ) -> None:
        """Update task columns (see `tasks.py`)."""
        ...

    @abstractmethod
    def _update_queue_item(
        self, item_id: int, assignments: str, params: tuple[object, ...]
    ) -> None:
        """Update queue columns (see `queue.py`)."""
        ...

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

    def _require_run(self, run_id: str) -> None:
        if self._conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
            raise NotFoundError(f"run {run_id!r} not found")

    def _require_task(self, run_id: str, task_id: str) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM tasks WHERE run_id = ? AND id = ?", (run_id, task_id)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"task {task_id!r} not found in run {run_id!r}")
