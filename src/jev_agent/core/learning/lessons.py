"""Lesson rows of one lesson database: add/get/list/edit/retire/delete, plus the
use/score ledger (`record_use`, `score_task`).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence

from jev_agent.core.learning.base import LessonDBBase
from jev_agent.core.learning.models import (
    _EDGE_ORIGINS,
    MAX_LESSON_CHARS,
    RETIRE_AT_SCORE,
    LearningError,
    Lesson,
)
from jev_agent.core.learning.paths import normalize_module_path, qualify_module


class LessonLessonsMixin(LessonDBBase):
    """Lesson CRUD and scoring (one `LessonDB` concern)."""

    def add_lesson(
        self,
        profile: str,
        text: str,
        roles: Sequence[str],
        tags: Sequence[str],
        source_run: str | None = None,
        source_task: str | None = None,
        repo_slug: str | None = None,
        origin: str = "manual",
    ) -> Lesson:
        """Add a lesson; path-looking tags become `applies-to` module edges.

        `origin` is where the lesson came from (`"manual"` for CLI/user adds, `"learner"`
        for the learner role): it is recorded on the auto-derived `applies-to` edges so
        `jev learning graph` attributes them correctly.
        """
        if origin not in _EDGE_ORIGINS:
            raise LearningError(f"unknown lesson origin {origin!r}")
        now = self._clock()
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO lessons (profile, text, roles_json, tags_json, source_run,"
                " source_task, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    profile,
                    text,
                    json.dumps(list(roles)),
                    json.dumps(list(tags)),
                    source_run,
                    source_task,
                    now,
                    now,
                ),
            )
            assert cur.lastrowid is not None
            number = cur.lastrowid
        lesson = self.get_lesson(number)
        assert lesson is not None
        if self.graph_ready():
            # Path-looking tags become `applies-to` module nodes; for shared profiles the
            # run's repo slug qualifies them (`slug:path`).
            seen: set[str] = set()
            with self._tx() as db:
                for tag in tags:
                    path = normalize_module_path(tag)
                    if path is None or path in seen:
                        continue
                    seen.add(path)
                    db.execute(
                        "INSERT OR IGNORE INTO lesson_modules"
                        " (profile, path, repo_slug, first_seen_at) VALUES (?, ?, ?, ?)",
                        (profile, path, repo_slug, now),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO lesson_edges (profile, src_type, src_id, edge,"
                        " dst_type, dst_id, origin, created_at)"
                        " VALUES (?, 'lesson', ?, 'applies-to', 'module', ?, ?, ?)",
                        (profile, str(number), qualify_module(path, repo_slug), origin, now),
                    )
        return lesson

    def get_lesson(self, number: int) -> Lesson | None:
        row = self.conn.execute("SELECT * FROM lessons WHERE id = ?", (number,)).fetchone()
        return None if row is None else self._lesson(row)

    def list_lessons(self, profile: str, include_retired: bool = False) -> list[Lesson]:
        """The profile's lessons, oldest first."""
        sql = "SELECT * FROM lessons WHERE profile = ?"
        if not include_retired:
            sql += " AND retired = 0"
        rows = self.conn.execute(sql + " ORDER BY id", (profile,))
        return [self._lesson(row) for row in rows]

    # Writes to one lesson name its profile too, so a stale id can never change a lesson of
    # another profile (issue #40).

    def update_text(self, profile: str, number: int, text: str) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE lessons SET text = ?, updated_at = ? WHERE id = ? AND profile = ?",
                (text, self._clock(), number, profile),
            )

    def retire(self, profile: str, number: int) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE lessons SET retired = 1, updated_at = ? WHERE id = ? AND profile = ?",
                (self._clock(), number, profile),
            )

    def update_lesson(
        self,
        profile: str,
        number: int,
        text: str,
        roles: Sequence[str],
        tags: Sequence[str],
    ) -> None:
        """Edit a lesson's text, roles, and tags (issue #161)."""
        if not text.strip():
            raise LearningError("lesson text cannot be empty")
        if len(text) > MAX_LESSON_CHARS:
            raise LearningError(f"lesson text too long (max {MAX_LESSON_CHARS} chars)")
        with self._tx() as db:
            cur = db.execute(
                "UPDATE lessons SET text = ?, roles_json = ?, tags_json = ?, updated_at = ?"
                " WHERE id = ? AND profile = ?",
                (
                    text.strip(),
                    json.dumps(list(roles)),
                    json.dumps(list(tags)),
                    self._clock(),
                    number,
                    profile,
                ),
            )
            if cur.rowcount == 0:
                raise LearningError(f"no lesson {self.lesson_id(number)} in profile {profile!r}")

    def delete_lesson(self, profile: str, number: int) -> None:
        """Permanently delete a lesson (issue #161). Unlike `retire`, the row is removed."""
        with self._tx() as db:
            cur = db.execute("DELETE FROM lessons WHERE id = ? AND profile = ?", (number, profile))
            if cur.rowcount == 0:
                raise LearningError(f"no lesson {self.lesson_id(number)} in profile {profile!r}")

    # ---- skills distilled from lessons (issue #161) ---------------------------------------

    def record_use(self, profile: str, number: int, score_delta: float = 0.0) -> None:
        """One more use, plus a score change; retire the lesson if its score is too low."""
        with self._tx() as db:
            self._record_use(db, profile, number, score_delta)

    def score_task(
        self,
        profile: str,
        run_id: str,
        task_id: str,
        numbers: Iterable[int],
        score_delta: float,
    ) -> bool:
        """Record one use of each lesson for a task that ended, exactly once per (run, task):
        the ledger row and the score changes commit together. False if it was scored before."""
        with self._tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO scored_tasks (run_id, task_id, profile, scored_at)"
                " VALUES (?, ?, ?, ?)",
                (run_id, task_id, profile, self._clock()),
            )
            if cur.rowcount == 0:
                return False
            for number in numbers:
                self._record_use(db, profile, number, score_delta)
        return True

    def _record_use(self, db: sqlite3.Connection, profile: str, number: int, delta: float) -> None:
        db.execute(
            "UPDATE lessons SET uses = uses + 1, score = score + ?, updated_at = ?,"
            " retired = CASE WHEN score + ? <= ? THEN 1 ELSE retired END"
            " WHERE id = ? AND profile = ?",
            (delta, self._clock(), delta, RETIRE_AT_SCORE, number, profile),
        )


__all__ = ["LessonLessonsMixin"]
