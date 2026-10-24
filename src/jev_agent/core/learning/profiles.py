"""Profile rows of one lesson database: create/list/enable/flush/delete, plus the
tuned Laya weights stored on a profile.
"""

from __future__ import annotations

import json
import re

from jev_agent.config.schema import PROFILE_NAME_PATTERN
from jev_agent.core.learning.base import LessonDBBase
from jev_agent.core.learning.models import LearningError, Profile
from jev_agent.core.learning.weights import _checked_weights, _stored_weights


def check_profile_name(name: str) -> str:
    if not re.match(PROFILE_NAME_PATTERN, name):
        raise LearningError(
            f"{name!r} is not a valid profile name (letters, digits, `-` and `_`, starting with "
            "a letter or digit, at most 40 characters)"
        )
    return name


class LessonProfilesMixin(LessonDBBase):
    """Profile CRUD and stored Laya weights (one `LessonDB` concern)."""

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
        assert profile is not None
        return profile

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE learning_profiles SET enabled = ? WHERE name = ?", (int(enabled), name)
            )

    def get_laya_weights(self, name: str) -> dict[str, float] | None:
        """Tuned Laya weights saved on the profile by `jev laya tune --apply`, or None for the
        configured ones. Raises LearningError when the stored weights no longer validate."""
        row = self.conn.execute(
            "SELECT laya_weights_json FROM learning_profiles WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else _stored_weights(name, row["laya_weights_json"])

    def set_laya_weights(self, name: str, weights: dict[str, float] | None) -> None:
        """Save tuned Laya weights on the profile (validated first); None clears them."""
        stored = None if weights is None else json.dumps(_checked_weights(name, weights))
        with self._tx() as db:
            db.execute(
                "UPDATE learning_profiles SET laya_weights_json = ? WHERE name = ?", (stored, name)
            )

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
