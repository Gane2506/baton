"""Skills distilled from lessons (issue #161): add/list/delete, plus the
tag-grouping proposer the dashboard shows for approval.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime

from jev_agent.core.learning.base import LessonDBBase
from jev_agent.core.learning.models import LearningError, Lesson, Skill


class LessonSkillsMixin(LessonDBBase):
    """The `learned_skills` table (one `LessonDB` concern)."""

    def _skills_ready(self) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'learned_skills'"
        ).fetchone()
        return row is not None

    def _skill(self, row: sqlite3.Row) -> Skill:
        return Skill(
            id=row["id"],
            profile=row["profile"],
            name=row["name"],
            description=row["description"],
            source=row["source"],
            lesson_ids=tuple(json.loads(row["lesson_ids"])),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def add_skill(
        self,
        profile: str,
        name: str,
        description: str,
        lesson_ids: Sequence[str],
        source: str = "learned",
    ) -> Skill:
        """Add a skill distilled from lessons. `source` is 'learned' or 'manual'."""
        if source not in ("learned", "manual"):
            raise LearningError(f"unknown skill source {source!r}")
        if not name.strip():
            raise LearningError("skill name cannot be empty")
        if not description.strip():
            raise LearningError("skill description cannot be empty")
        with self._tx() as db:
            try:
                cur = db.execute(
                    "INSERT INTO learned_skills (profile, name, description, source,"
                    " lesson_ids, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        profile,
                        name.strip(),
                        description.strip(),
                        source,
                        json.dumps(list(lesson_ids)),
                        self._clock(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise LearningError(
                    f"skill {name!r} already exists in profile {profile!r}"
                ) from None
            assert cur.lastrowid is not None
            row = db.execute(
                "SELECT * FROM learned_skills WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._skill(row)

    def list_skills(self, profile: str) -> list[Skill]:
        """The profile's skills, oldest first."""
        if not self._skills_ready():
            return []
        rows = self.conn.execute(
            "SELECT * FROM learned_skills WHERE profile = ? ORDER BY id", (profile,)
        )
        return [self._skill(row) for row in rows]

    def delete_skill(self, profile: str, skill_id: int) -> None:
        """Delete a skill."""
        with self._tx() as db:
            cur = db.execute(
                "DELETE FROM learned_skills WHERE id = ? AND profile = ?",
                (skill_id, profile),
            )
            if cur.rowcount == 0:
                raise LearningError(f"no skill {skill_id} in profile {profile!r}")

    def propose_skills(self, profile: str) -> list[dict[str, object]]:
        """Analyze lessons and propose skills (issue #161).

        Groups lessons by shared tags into candidate skills. Returns a list of
        dicts with 'name', 'description', and 'lesson_ids'. The caller (dashboard)
        shows these for user approval before calling `add_skill`.
        """
        lessons = self.list_lessons(profile)
        if not lessons:
            return []
        # Group by tag: lessons sharing a tag are candidates for one skill.
        by_tag: dict[str, list[Lesson]] = {}
        for lesson in lessons:
            for tag in lesson.tags:
                by_tag.setdefault(tag, []).append(lesson)
        proposals: list[dict[str, object]] = []
        seen: set[frozenset[str]] = set()
        for tag, group in sorted(by_tag.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if len(group) < 2:
                continue  # a skill needs at least 2 lessons
            key = frozenset(lesson.id for lesson in group)
            if key in seen:
                continue
            seen.add(key)
            # Build a skill name from the tag and a description from the lessons.
            name = tag.replace("_", " ").replace("-", " ").title()
            description = f"Distilled from {len(group)} lessons about '{tag}': " + "; ".join(
                lesson.text for lesson in group[:3]
            )
            if len(group) > 3:
                description += f"; and {len(group) - 3} more"
            proposals.append(
                {
                    "name": name,
                    "description": description,
                    "lesson_ids": [lesson.id for lesson in group],
                    "tag": tag,
                }
            )
        return proposals


__all__ = ["LessonSkillsMixin"]
