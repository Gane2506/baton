"""`Learning`: every profile visible from one repo — its own (in state.db) plus
the shared ones. The shared database is opened on first use, and only created
when something is written to it, so reading never leaves a file behind.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from jev_agent.core.learning.base import _now
from jev_agent.core.learning.db import LessonDB, shared_db_path
from jev_agent.core.learning.models import _LESSON_ID, LearningError, Lesson, Profile
from jev_agent.core.learning.profiles import check_profile_name
from jev_agent.core.store import Store


class Learning:
    """Every profile visible from one repo: its own (in state.db) plus the shared ones.

    The shared database is opened on first use, and only created when something is written to
    it, so reading never leaves a file behind. Call `close()` when done.
    """

    def __init__(
        self,
        repo: LessonDB,
        shared_path: Path | None = None,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.repo = repo
        self.shared_path = shared_path or shared_db_path()
        self._clock = _now if clock is None else clock
        self._shared: LessonDB | None = None

    @classmethod
    def for_store(
        cls,
        store: Store,
        shared_path: Path | None = None,
        *,
        clock: Callable[[], str] | None = None,
    ) -> Learning:
        return cls(LessonDB(store.connection, "repo", clock=clock), shared_path, clock=clock)

    def close(self) -> None:
        if self._shared is not None:
            self._shared.conn.close()
            self._shared = None

    def shared(self, create: bool = False) -> LessonDB | None:
        if self._shared is None and (create or self.shared_path.exists()):
            self._shared = LessonDB.open_shared(self.shared_path, clock=self._clock)
        return self._shared

    def _dbs(self) -> list[LessonDB]:
        shared = self.shared()
        return [self.repo] if shared is None else [self.repo, shared]

    # ---- profiles --------------------------------------------------------------------------

    def profiles(self) -> list[Profile]:
        return [p for db in self._dbs() for p in db.list_profiles()]

    def get(self, name: str) -> Profile | None:
        for db in self._dbs():
            profile = db.get_profile(name)
            if profile is not None:
                return profile
        return None

    def db_for(self, name: str) -> LessonDB:
        for db in self._dbs():
            if db.get_profile(name) is not None:
                return db
        raise LearningError(
            f"no learning profile named {name!r}; create it with `jev learning create {name}`"
        )

    def create(self, name: str, shared: bool = False) -> Profile:
        check_profile_name(name)
        existing = self.get(name)
        if existing is not None:
            raise LearningError(f"profile {name!r} already exists ({existing.scope} scope)")
        db = self.shared(create=True) if shared else self.repo
        if db is None:
            raise LearningError("the shared lesson database could not be opened")
        return db.create_profile(name)

    def ensure(self, name: str) -> Profile:
        """The profile, created in repo scope if it does not exist yet."""
        return self.get(name) or self.create(name)

    def set_enabled(self, name: str, enabled: bool) -> None:
        self.db_for(name).set_enabled(name, enabled)

    def flush(self, name: str) -> int:
        return self.db_for(name).flush(name)

    def delete(self, name: str) -> None:
        self.db_for(name).delete_profile(name)

    # ---- lessons ---------------------------------------------------------------------------

    def lessons(self, name: str, include_retired: bool = False) -> list[Lesson]:
        return self.db_for(name).list_lessons(name, include_retired)

    def find_lesson(self, lesson_id: str) -> tuple[LessonDB, Lesson]:
        match = _LESSON_ID.match(lesson_id.strip())
        db: LessonDB | None = None
        if match:
            db = self.repo if match.group(1).upper() == "L" else self.shared()
        lesson = None if db is None or match is None else db.get_lesson(int(match.group(2)))
        if db is None or lesson is None:
            raise LearningError(f"no lesson {lesson_id!r} (ids look like L12 or S3)")
        return db, lesson

    def forget(self, lesson_id: str) -> Lesson:
        """Retire one lesson so it is never injected again; it stays visible with `--all`."""
        db, lesson = self.find_lesson(lesson_id)
        db.retire(lesson.profile, lesson.number)
        return lesson


__all__ = ["Learning"]
