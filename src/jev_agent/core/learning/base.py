"""Shared plumbing for `LessonDB`: the connection, the transaction helper, row
mappers, and the small reads the mixins share.

`LessonDBBase` is abstract only for the two reads that live in the lessons mixin
(`get_lesson`, `list_lessons`); everything else here is concrete
so the mixins type-check under strict mypy without Protocols.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from jev_agent.core.learning.models import _PREFIX, Lesson, Profile, Scope
from jev_agent.core.migrations import SHARED_MIGRATIONS, apply_migrations, configure_connection


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class LessonDBBase(ABC):
    """Connection, clock, and row mapping shared by every `LessonDB` mixin."""

    def __init__(
        self, conn: sqlite3.Connection, scope: Scope, *, clock: Callable[[], str] | None = None
    ) -> None:
        self.conn = conn
        self.scope = scope
        self._clock = _now if clock is None else clock

    @classmethod
    def open_shared(cls, path: Path, *, clock: Callable[[], str] | None = None) -> Self:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            configure_connection(conn)
            apply_migrations(conn, SHARED_MIGRATIONS)
        except BaseException:
            conn.close()
            raise
        return cls(conn, "shared", clock=clock)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()

    def lesson_id(self, number: int) -> str:
        return f"{_PREFIX[self.scope]}{number}"

    @abstractmethod
    def get_lesson(self, number: int) -> Lesson | None:
        """One lesson by row number (implemented in `lessons.py`)."""
        ...

    @abstractmethod
    def list_lessons(self, profile: str, include_retired: bool = False) -> list[Lesson]:
        """The profile's lessons, oldest first (implemented in `lessons.py`)."""
        ...

    def _profile(self, row: sqlite3.Row) -> Profile:
        return Profile(
            name=row["name"],
            scope=self.scope,
            enabled=bool(row["enabled"]),
            lessons=row["lesson_count"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _lesson(self, row: sqlite3.Row) -> Lesson:
        return Lesson(
            id=self.lesson_id(row["id"]),
            profile=row["profile"],
            text=row["text"],
            roles=tuple(json.loads(row["roles_json"])),
            tags=tuple(json.loads(row["tags_json"])),
            source_run=row["source_run"],
            source_task=row["source_task"],
            created_at=datetime.fromisoformat(row["created_at"]),
            uses=row["uses"],
            score=row["score"],
            retired=bool(row["retired"]),
        )

    def graph_ready(self) -> bool:
        """Whether this database has the lesson-graph tables.

        The graph migration ships in `SHARED_MIGRATIONS` first; repo databases get it with
        their own migration later. Graph writes degrade gracefully until then (no edges are
        recorded), while explicit graph reads raise `LearningError`.
        """
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lesson_edges'"
        ).fetchone()
        return row is not None


__all__ = ["LessonDBBase"]
