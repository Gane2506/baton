"""Profile rows of one lesson database: create/list/enable/flush/delete."""

from __future__ import annotations

import re

from jev_agent.config.schema import PROFILE_NAME_PATTERN
from jev_agent.core.learning.base import LessonDBBase
from jev_agent.core.learning.models import LearningError, Profile


def check_profile_name(name: str) -> str:
    if not re.match(PROFILE_NAME_PATTERN, name):
        raise LearningError(
            f"{name!r} is not a valid profile name (letters, digits, `-` and `_`, starting with "
            "a letter or digit, at most 40 characters)"
        )
    return name


class LessonProfilesMixin(LessonDBBase):
    """Profile CRUD (one `LessonDB` concern)."""

    def get_profile(self, name: str) -> Profile | None:
        row = self.conn.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM lessons l WHERE l.profile = p.name"
            " AND l.retired = 0) AS lesson_count FROM learning_profiles p WHERE p.name = ?",
            (name,),
        ).fetchone()
        return None if row is None else self._profile(row)

    def list_profiles(self) -> list[Profile]:
        rows = self.conn.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM lessons l WHERE l.profile = p.name"
            " AND l.retired = 0) AS lesson_count FROM learning_profiles p ORDER BY p.name"
        )
        return [self._profile(row) for row in rows]

    def create_profile(self, name: str) -> Profile:
        with self._tx() as db:
            db.execute(
                "INSERT INTO learning_profiles (name, enabled, created_at) VALUES (?, 1, ?)",
                (name, self._clock()),
            )
        profile = self.get_profile(name)
        if profile is None:
            raise LearningError(f"profile {name!r} was not stored")
        return profile

    def set_enabled(self, name: str, enabled: bool) -> None:
        """Enable or disable a profile. Raises LearningError when it does not exist
        (a silent no-op used to hide a typo'd name)."""
        with self._tx() as db:
            cur = db.execute(
                "UPDATE learning_profiles SET enabled = ? WHERE name = ?", (int(enabled), name)
            )
            if cur.rowcount == 0:
                raise LearningError(f"no learning profile named {name!r}")

    def flush(self, name: str) -> int:
        """Delete every lesson of the profile (retired ones too); return how many."""
        with self._tx() as db:
            if self.graph_ready():
                # The FTS triggers clean `lessons_fts` as the lesson rows go.
                db.execute("DELETE FROM lesson_edges WHERE profile = ?", (name,))
                db.execute("DELETE FROM lesson_modules WHERE profile = ?", (name,))
            return db.execute("DELETE FROM lessons WHERE profile = ?", (name,)).rowcount

    def delete_profile(self, name: str) -> None:
        with self._tx() as db:
            if self.graph_ready():
                db.execute("DELETE FROM lesson_edges WHERE profile = ?", (name,))
                db.execute("DELETE FROM lesson_modules WHERE profile = ?", (name,))
            db.execute("DELETE FROM lessons WHERE profile = ?", (name,))
            db.execute("DELETE FROM learning_profiles WHERE name = ?", (name,))


__all__ = ["LessonProfilesMixin", "check_profile_name"]
