"""Event log (`docs/events.md`): append, list and sequence helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection

from pydantic import BaseModel

from jev_agent.core.events import (
    ACTOR_PIPELINE,
    EVENT_SCHEMA_V,
    Event,
    EventType,
    event_from_row,
)

from .base import StoreBase


class EventsMixin(StoreBase):
    """The run event log."""

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
        """Append one event to the run's log. Call only from inside the caller's `_tx()`
        block: the event and the state change it describes commit atomically, and
        `run_seq = MAX(run_seq)+1` in the same transaction keeps per-run ordering
        gapless under the single-writer invariant (the lease allows one driver per run;
        `jev cancel` serializes on the write lock). Returns the event's `run_seq`, or
        None when the log is disabled. If the insert fails the whole transaction rolls
        back, so the state write and the event never diverge."""
        if not self._events_enabled:
            return None
        row = db.execute(
            "SELECT COALESCE(MAX(run_seq), 0) AS tip FROM run_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        run_seq = int(row["tip"]) + 1
        db.execute(
            "INSERT INTO run_events (run_id, run_seq, type, schema_v, ts, actor,"
            " parent_run_seq, synthetic, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                run_seq,
                event_type.value,
                EVENT_SCHEMA_V,
                self._clock(),
                actor,
                parent_run_seq,
                int(synthetic),
                payload.model_dump_json(),
            ),
        )
        return run_seq

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
        """Append an event in its own transaction, for events with no snapshot mutation
        (`decision.requested`, `run.resumed`, backfill). Events paired with a state
        change are emitted via `_emit` inside that change's `_tx()` instead."""
        if not self._events_enabled:
            return None
        with self._tx() as db:
            return self._emit(
                db,
                run_id,
                event_type,
                payload,
                actor=actor,
                parent_run_seq=parent_run_seq,
                synthetic=synthetic,
            )

    def list_events(
        self,
        run_id: str,
        *,
        event_types: Collection[EventType] | None = None,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[Event]:
        """The run's events in `run_seq` order. Raises NotFoundError for an unknown run."""
        self._require_run(run_id)
        query = "SELECT * FROM run_events WHERE run_id = ? AND run_seq > ?"
        params: list[object] = [run_id, after_seq]
        if event_types:
            marks = ", ".join("?" for _ in event_types)
            query += f" AND type IN ({marks})"
            params.extend(t.value for t in event_types)
        query += " ORDER BY run_seq"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [event_from_row(row) for row in rows]

    def latest_event_seq(
        self,
        run_id: str,
        event_type: EventType,
        *,
        task_id: str | None = None,
        attempt_id: int | None = None,
    ) -> int | None:
        """Newest `run_seq` of `event_type`, optionally filtered by the payload's
        `task_id`/`attempt_id`. Used to link causal parents (`parent_run_seq`)."""
        return self._latest_event_seq_locked(
            self._conn, run_id, event_type, task_id=task_id, attempt_id=attempt_id
        )

    @staticmethod
    def _latest_event_seq_locked(
        db: sqlite3.Connection,
        run_id: str,
        event_type: EventType,
        *,
        task_id: str | None = None,
        attempt_id: int | None = None,
    ) -> int | None:
        """`latest_event_seq` on an explicit connection (inside a `_tx()` block)."""
        query = "SELECT MAX(run_seq) AS tip FROM run_events WHERE run_id = ? AND type = ?"
        params: list[object] = [run_id, event_type.value]
        if task_id is not None:
            query += " AND json_extract(payload_json, '$.task_id') = ?"
            params.append(task_id)
        if attempt_id is not None:
            query += " AND json_extract(payload_json, '$.attempt_id') = ?"
            params.append(attempt_id)
        row = db.execute(query, params).fetchone()
        return None if row is None or row["tip"] is None else int(row["tip"])

    def event_tip(self, run_id: str) -> int:
        """Highest `run_seq` in the run's log (0 when the log is empty)."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(run_seq), 0) AS tip FROM run_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["tip"])
