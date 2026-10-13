"""Recursive learning: profiles of lessons, choosing which lessons a prompt gets, and deciding
which profile (if any) a run uses. See docs/learning.md.

- A **profile** is a named store of **lessons**. Scope `repo` keeps it in the repo's state.db
  (`.jev/state.db`); scope `shared` keeps it in `$XDG_DATA_HOME/jev/learning.db` (else
  `~/.local/share/jev/learning.db`), usable from every repo. Names are unique across scopes;
  lookups try the repo first. Lesson ids carry their scope: `L12` (repo), `S3` (shared).
- **Selection** (`select_lessons`) is deterministic: lessons for the role, not retired, ranked
  by how many of their tags match the context (task files and title words, idea words), then
  by score, then newest first; capped by `[learning] max_lessons` and `max_chars`.
- **Resolution** (`resolve_learning`): a run flag beats the repo setting stored by
  `jev learning` beats `jev.toml` beats the defaults.

The distill step (the learner role, D7, scoring) lives in `core/learner.py`.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jev_agent.config.schema import PROFILE_NAME_PATTERN, JevConfig
from jev_agent.core.migrations import SHARED_MIGRATIONS, apply_migrations
from jev_agent.core.models import LESSON_ROLES
from jev_agent.core.store import Store

Scope = Literal["repo", "shared"]

MAX_LESSON_CHARS = 300
RETIRE_AT_SCORE = -2.0  # a lesson whose score falls to this is retired automatically

# Repo settings written by `jev learning` (table `settings` in state.db).
SETTING_ENABLED = "learning.enabled"  # "true" | "false"
SETTING_PROFILE = "learning.profile"  # the active profile's name

_PREFIX: dict[Scope, str] = {"repo": "L", "shared": "S"}
_LESSON_ID = re.compile(r"^([LS])(\d+)$", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9_]+")
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "that",
        "this",
        "then",
        "than",
        "when",
        "what",
        "which",
        "add",
        "use",
        "make",
        "new",
        "fix",
        "not",
        "are",
        "was",
        "were",
        "has",
        "have",
        "can",
        "should",
        "must",
        "all",
        "any",
        "its",
        "our",
        "your",
        "their",
        "test",
        "tests",
    }
)


class LearningError(Exception):
    """A learning command or setting cannot be applied. The message is user-facing."""


@dataclass(frozen=True)
class Profile:
    name: str
    scope: Scope
    enabled: bool
    lessons: int  # lessons that are not retired
    created_at: datetime


@dataclass(frozen=True)
class Lesson:
    id: str  # "L12" (repo) or "S3" (shared)
    profile: str
    text: str
    roles: tuple[str, ...]
    tags: tuple[str, ...]
    source_run: str | None
    source_task: str | None
    created_at: datetime
    uses: int
    score: float
    retired: bool

    @property
    def number(self) -> int:
        return int(self.id[1:])


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def check_profile_name(name: str) -> str:
    if not re.match(PROFILE_NAME_PATTERN, name):
        raise LearningError(
            f"{name!r} is not a valid profile name (letters, digits, `-` and `_`, starting with "
            "a letter or digit, at most 40 characters)"
        )
    return name


def shared_db_path() -> Path:
    """`$XDG_DATA_HOME/jev/learning.db`, falling back to `~/.local/share/jev/learning.db`."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "jev" / "learning.db"


class LessonDB:
    """Profiles and lessons in one SQLite database (the repo's state.db, or the shared one)."""

    def __init__(self, conn: sqlite3.Connection, scope: Scope) -> None:
        self.conn = conn
        self.scope = scope

    @classmethod
    def open_shared(cls, path: Path) -> LessonDB:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            apply_migrations(conn, SHARED_MIGRATIONS)
        except BaseException:
            conn.close()
            raise
        return cls(conn, "shared")

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

    # ---- profiles --------------------------------------------------------------------------

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
                (name, _now()),
            )
        profile = self.get_profile(name)
        assert profile is not None
        return profile

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE learning_profiles SET enabled = ? WHERE name = ?", (int(enabled), name)
            )

    def flush(self, name: str) -> int:
        """Delete every lesson of the profile (retired ones too); return how many."""
        with self._tx() as db:
            return db.execute("DELETE FROM lessons WHERE profile = ?", (name,)).rowcount

    def delete_profile(self, name: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM lessons WHERE profile = ?", (name,))
            db.execute("DELETE FROM learning_profiles WHERE name = ?", (name,))

    def _profile(self, row: sqlite3.Row) -> Profile:
        return Profile(
            name=row["name"],
            scope=self.scope,
            enabled=bool(row["enabled"]),
            lessons=row["lesson_count"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # ---- lessons ---------------------------------------------------------------------------

    def add_lesson(
        self,
        profile: str,
        text: str,
        roles: Sequence[str],
        tags: Sequence[str],
        source_run: str | None = None,
        source_task: str | None = None,
    ) -> Lesson:
        now = _now()
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

    def update_text(self, number: int, text: str) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE lessons SET text = ?, updated_at = ? WHERE id = ?", (text, _now(), number)
            )

    def retire(self, number: int) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE lessons SET retired = 1, updated_at = ? WHERE id = ?", (_now(), number)
            )

    def record_use(self, number: int, score_delta: float = 0.0) -> None:
        """One more use, plus a score change; retire the lesson if its score is too low."""
        with self._tx() as db:
            db.execute(
                "UPDATE lessons SET uses = uses + 1, score = score + ?, updated_at = ?,"
                " retired = CASE WHEN score + ? <= ? THEN 1 ELSE retired END WHERE id = ?",
                (score_delta, _now(), score_delta, RETIRE_AT_SCORE, number),
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


class Learning:
    """Every profile visible from one repo: its own (in state.db) plus the shared ones.

    The shared database is opened on first use, and only created when something is written to
    it, so reading never leaves a file behind. Call `close()` when done.
    """

    def __init__(self, repo: LessonDB, shared_path: Path | None = None) -> None:
        self.repo = repo
        self.shared_path = shared_path or shared_db_path()
        self._shared: LessonDB | None = None

    @classmethod
    def for_store(cls, store: Store, shared_path: Path | None = None) -> Learning:
        return cls(LessonDB(store.connection, "repo"), shared_path)

    def close(self) -> None:
        if self._shared is not None:
            self._shared.conn.close()
            self._shared = None

    def shared(self, create: bool = False) -> LessonDB | None:
        if self._shared is None and (create or self.shared_path.exists()):
            self._shared = LessonDB.open_shared(self.shared_path)
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
        assert db is not None
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
        db.retire(lesson.number)
        return lesson


# ---- which profile a run uses ------------------------------------------------------------


@dataclass(frozen=True)
class LearningStatus:
    """The repo's learning switch and active profile, with where each value came from."""

    enabled: bool
    enabled_source: str  # "cli setting" | "config" | "default"
    profile: str
    profile_source: str


def _setting_bool(value: str | None) -> bool | None:
    return None if value is None else value == "true"


def learning_status(store: Store | None, config: JevConfig) -> LearningStatus:
    """The repo switch and active profile: CLI setting (state.db) > jev.toml > default."""
    lc, set_in_config = config.learning, config.learning.model_fields_set
    enabled = None if store is None else _setting_bool(store.get_setting(SETTING_ENABLED))
    profile = None if store is None else store.get_setting(SETTING_PROFILE)
    return LearningStatus(
        enabled=lc.enabled if enabled is None else enabled,
        enabled_source="cli setting"
        if enabled is not None
        else ("config" if "enabled" in set_in_config else "default"),
        profile=profile or lc.profile,
        profile_source="cli setting"
        if profile
        else ("config" if "profile" in set_in_config else "default"),
    )


def resolve_learning(
    store: Store,
    learning: Learning,
    config: JevConfig,
    flag_profile: str | None = None,
    no_learning: bool = False,
) -> str | None:
    """The profile a new run uses, or None for no learning.

    Precedence: `--no-learning` / `--learning NAME` (NAME must exist; it is used even if
    disabled) > the repo setting from `jev learning enable|disable|use` > `[learning]` in
    jev.toml > defaults (off, "default"). The active profile is created in repo scope if it
    does not exist; a disabled active profile means no learning.
    """
    if flag_profile is not None and no_learning:
        raise LearningError("--learning and --no-learning cannot be used together")
    if no_learning:
        return None
    if flag_profile is not None:
        profile = learning.get(check_profile_name(flag_profile))
        if profile is None:
            raise LearningError(
                f"no learning profile named {flag_profile!r}; "
                f"create it with `jev learning create {flag_profile}`"
            )
        return profile.name
    status = learning_status(store, config)
    if not status.enabled:
        return None
    profile = learning.ensure(status.profile)
    return profile.name if profile.enabled else None


def set_repo_learning(store: Store, enabled: bool) -> None:
    store.set_setting(SETTING_ENABLED, "true" if enabled else "false")


def set_active_profile(store: Store, name: str | None) -> None:
    store.set_setting(SETTING_PROFILE, name)


# ---- selection ---------------------------------------------------------------------------


def context_terms(texts: Iterable[str] = (), paths: Iterable[str] = ()) -> set[str]:
    """Lowercase words (3+ characters, no stopwords) from `texts`, and for each path: the path,
    its directory prefixes, its components and their stems (`src/a/b.py` gives `src`, `src/a`,
    `src/a/b.py`, `a`, `b.py`, `b`)."""
    terms: set[str] = set()
    for text in texts:
        terms.update(w for w in _WORD.findall(text.lower()) if len(w) >= 3 and w not in _STOPWORDS)
    for raw in paths:
        path = raw.strip().strip("/").lower()
        if not path:
            continue
        parts = path.split("/")
        terms.add(path)
        for i in range(1, len(parts)):
            terms.add("/".join(parts[:i]))
        for part in parts:
            terms.add(part)
            terms.add(part.rsplit(".", 1)[0])
    terms.discard("")
    return terms


def normalize_tag(tag: str) -> str:
    return " ".join(tag.strip().strip("/").lower().split())


def tag_overlap(lesson: Lesson, terms: set[str]) -> int:
    """How many of the lesson's tags match `terms`. A multi-word tag (`rate limiting`,
    `rate-limiting`) matches when each of its words does."""
    count = 0
    for tag in lesson.tags:
        norm = normalize_tag(tag)
        words = [w for w in re.split(r"[\s_-]+", norm) if w]
        if norm in terms or (len(words) > 1 and all(w in terms for w in words)):
            count += 1
    return count


def select_lessons(
    lessons: Iterable[Lesson],
    role: str | None,
    terms: set[str],
    max_lessons: int,
    max_chars: int,
) -> list[Lesson]:
    """Lessons for `role` (None: any role), best first: most tag matches, then highest score,
    then newest.

    At most `max_lessons`, with at most `max_chars` of text in total (a lesson that does not
    fit is skipped and smaller ones after it may still fit).
    """
    eligible = [
        lesson
        for lesson in lessons
        if not lesson.retired and (role is None or role in lesson.roles or not lesson.roles)
    ]
    ranked = sorted(eligible, key=lambda x: (-tag_overlap(x, terms), -x.score, -x.number))
    chosen: list[Lesson] = []
    used = 0
    for lesson in ranked:
        if len(chosen) >= max_lessons:
            break
        if used + len(lesson.text) > max_chars:
            continue
        chosen.append(lesson)
        used += len(lesson.text)
    return chosen


__all__ = [
    "LESSON_ROLES",
    "MAX_LESSON_CHARS",
    "RETIRE_AT_SCORE",
    "SETTING_ENABLED",
    "SETTING_PROFILE",
    "Learning",
    "LearningError",
    "LearningStatus",
    "Lesson",
    "LessonDB",
    "Profile",
    "Scope",
    "check_profile_name",
    "context_terms",
    "learning_status",
    "resolve_learning",
    "select_lessons",
    "set_active_profile",
    "set_repo_learning",
    "shared_db_path",
]
