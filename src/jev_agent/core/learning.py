"""Recursive learning: profiles of lessons, choosing which lessons a prompt gets, and deciding
which profile (if any) a run uses. See docs/learning.md.

- A **profile** is a named store of **lessons**. Scope `repo` keeps it in the repo's state.db
  (`.jev/state.db`); scope `shared` keeps it in `$XDG_DATA_HOME/jev/learning.db` (else
  `~/.local/share/jev/learning.db`), usable from every repo. Names are unique across scopes;
  lookups try the repo first. Lesson ids carry their scope: `L12` (repo), `S3` (shared), and
  are never reused once deleted.
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
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from jev_agent.config.schema import PROFILE_NAME_PATTERN, JevConfig, LayaSettings
from jev_agent.core.migrations import SHARED_MIGRATIONS, apply_migrations, configure_connection
from jev_agent.core.models import LESSON_ROLES
from jev_agent.core.store import Store

Scope = Literal["repo", "shared"]

MAX_LESSON_CHARS = 300
RETIRE_AT_SCORE = -2.0  # a lesson whose score falls to this is retired automatically

# Edge types stored in `lesson_edges` (shared migration 4). `derived-from` is a view over
# `lessons.source_run` and `co-occurs-with` is computed on demand: neither is stored.
GRAPH_EDGE_TYPES = ("supersedes", "contradicts", "applies-to")
_EDGE_ORIGINS = ("manual", "learner", "backfill")

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


@dataclass(frozen=True)
class LessonEdge:
    """One recorded graph edge. Lesson endpoints are display ids (`L12`/`S3`); module
    endpoints are paths, qualified with the repo slug for shared profiles
    (`baton:src/auth/login.py`). `derived-from` edges are never stored (see `EdgeView`)."""

    src: str
    edge: str  # one of GRAPH_EDGE_TYPES
    dst: str
    origin: str  # "manual" | "learner" | "backfill"


@dataclass(frozen=True)
class EdgeView:
    """One edge touching a lesson, from the lesson's point of view. `contradicts` is
    symmetric in display (one stored row shows up on both sides); `derived-from` is
    computed from `lessons.source_run`, never stored."""

    direction: str  # "out" | "in"
    edge: str
    other_type: str  # "lesson" | "run" | "task" | "module"
    other: str  # display id, run id, or module path
    origin: str  # "manual" | "learner" | "backfill" | "derived"


@dataclass(frozen=True)
class GraphStats:
    """What `jev learning graph stats` shows."""

    lessons: int
    retired: int
    edges: dict[str, int]  # by edge type, including derived-from
    contradictions_open: int  # contradicts pairs with both sides active
    top_modules: list[tuple[str, int]]  # module path, applies-to count


@dataclass(frozen=True)
class Suppression:
    """One lesson dropped from injection by the contradiction rule."""

    lesson: str  # the suppressed lesson's display id
    kept: str  # the contradicting lesson that won
    reason: str


def normalize_module_path(tag: str) -> str | None:
    """`tag` as a normalized module path, or None when it is not path-looking.

    Path-looking means containing `/` or ending in `.py` (the design's rule). The path is
    lowercased with leading `./` and surrounding slashes stripped, mirroring how
    `context_terms` lowercases and prefix-matches paths. Used for deriving modules from
    tags, where a strict rule keeps keyword tags from becoming module nodes.
    """
    path = tag.strip().removeprefix("./").strip("/")
    if not path or ("/" not in path and not path.lower().endswith(".py")):
        return None
    return path.lower()


def normalize_module_ref(ref: str) -> str | None:
    """Lenient module normalization for explicit input (CLI args, the learner's
    `applies_to`, `--module` filters): lowercased with `./` and slashes stripped. Bare
    directory names (`src`) are allowed; empty or whitespace-containing refs are not."""
    path = ref.strip().removeprefix("./").strip("/")
    if not path or any(ch.isspace() for ch in path):
        return None
    return path.lower()


def qualify_module(path: str, repo_slug: str | None) -> str:
    """A module path as stored on edges: `slug:path` for shared profiles, plain otherwise."""
    return f"{repo_slug}:{path}" if repo_slug else path


def split_module_ref(ref: str) -> tuple[str | None, str]:
    """A module reference (`path` or `slug:path`) into its slug and path parts."""
    ref = ref.strip()
    if ":" in ref:
        head, tail = ref.split(":", 1)
        if "/" in tail or tail.lower().endswith(".py"):
            return head.strip().lower() or None, tail
    return None, ref


def repo_slug_for(repo: Path | str) -> str:
    """Stable, human-meaningful repo slug from a directory name (design risk 4).

    `…/code/baton` → `baton`. Derived from the directory name, not the remote URL: it is
    visible without git, stable across clones that keep the checkout name, and readable in
    qualified module paths like `baton:src/auth/login.py`. Non-alphanumerics collapse to
    one `-`; an empty name falls back to `repo`.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", Path(repo).name.strip().lower()).strip("-")
    return slug or "repo"


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def co_injected(conn: sqlite3.Connection, a: str, b: str) -> bool:
    """Have lessons `a` and `b` (display ids, e.g. `L12`) ever been injected into the same
    attempt's prompt? Evidence for the contradiction rule, from `attempts.lesson_ids`."""
    row = conn.execute(
        "SELECT 1 FROM attempts WHERE lesson_ids IS NOT NULL AND json_valid(lesson_ids)"
        " AND EXISTS (SELECT 1 FROM json_each(lesson_ids) AS j WHERE j.value = ?)"
        " AND EXISTS (SELECT 1 FROM json_each(lesson_ids) AS j WHERE j.value = ?)"
        " LIMIT 1",
        (a, b),
    ).fetchone()
    return row is not None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _checked_weights(profile: str, weights: object) -> dict[str, float]:
    """`weights` if they are valid `[dispatcher.laya] weights`, else a LearningError."""
    try:
        return LayaSettings.model_validate({"weights": weights}).weights
    except ValidationError as exc:
        problems = "; ".join(str(e["msg"]) for e in exc.errors())
        raise LearningError(f"profile {profile!r}: invalid Laya weights: {problems}") from exc


def _stored_weights(profile: str, text: str | None) -> dict[str, float] | None:
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LearningError(f"profile {profile!r}: stored Laya weights are not JSON") from exc
    return _checked_weights(profile, value)


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

    def __init__(
        self, conn: sqlite3.Connection, scope: Scope, *, clock: Callable[[], str] | None = None
    ) -> None:
        self.conn = conn
        self.scope = scope
        self._clock = _now if clock is None else clock

    @classmethod
    def open_shared(cls, path: Path, *, clock: Callable[[], str] | None = None) -> LessonDB:
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

    # ---- the lesson graph (shared migration 4) ---------------------------------------------

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

    def _require_graph(self) -> None:
        if not self.graph_ready():
            raise LearningError(
                "the lesson graph is not available in this database yet: repo-scope profiles "
                "need the repo database's graph migration, which has not been applied"
            )

    def _lesson_number(self, profile: str, ref: str) -> int:
        """A lesson display id (`L12`) → its row number, validated: right scope for this
        database, exists, and belongs to `profile`."""
        match = _LESSON_ID.match(ref.strip())
        if match is None or match.group(1).upper() != _PREFIX[self.scope]:
            raise LearningError(
                f"{ref!r} is not a {self.scope}-scope lesson id (ids look like {self.lesson_id(1)})"
            )
        number = int(match.group(2))
        lesson = self.get_lesson(number)
        if lesson is None or lesson.profile != profile:
            raise LearningError(f"no lesson {ref.strip()!r} in profile {profile!r}")
        return number

    def _module_node(self, db: sqlite3.Connection, profile: str, ref: str) -> str:
        """Ensure the module node for a `path` / `slug:path` reference; return the stored
        (qualified) path."""
        slug, tail = split_module_ref(ref)
        path = normalize_module_ref(tail)
        if path is None:
            raise LearningError(
                f"{ref!r} is not a module path: use `a`, `a/b`, `a/b.py` or `slug:a/b`"
            )
        qualified = qualify_module(path, slug)
        db.execute(
            "INSERT OR IGNORE INTO lesson_modules (profile, path, repo_slug, first_seen_at)"
            " VALUES (?, ?, ?, ?)",
            (profile, path, slug, self._clock()),
        )
        return qualified

    def add_edge(
        self, profile: str, src: str, edge: str, dst: str, *, origin: str = "manual"
    ) -> bool:
        """Record one graph edge; True when it was new, False when it already existed.

        `src`/`dst` are lesson ids for `supersedes` and `contradicts`, and a lesson id
        plus a module path for `applies-to`. `supersedes` is advisory only: it never
        retires the old lesson — the old lesson stays active until a human retires it.
        """
        self._require_graph()
        if edge not in GRAPH_EDGE_TYPES:
            raise LearningError(f"unknown edge {edge!r} (use: {', '.join(GRAPH_EDGE_TYPES)})")
        if origin not in _EDGE_ORIGINS:
            raise LearningError(f"unknown edge origin {origin!r}")
        with self._tx() as db:
            if edge == "applies-to":
                src_type, src_id = "lesson", str(self._lesson_number(profile, src))
                dst_type, dst_id = "module", self._module_node(db, profile, dst)
            else:
                src_type, src_id = "lesson", str(self._lesson_number(profile, src))
                dst_type, dst_id = "lesson", str(self._lesson_number(profile, dst))
                if src_id == dst_id:
                    raise LearningError("a lesson cannot relate to itself")
            cur = db.execute(
                "INSERT OR IGNORE INTO lesson_edges (profile, src_type, src_id, edge,"
                " dst_type, dst_id, origin, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (profile, src_type, src_id, edge, dst_type, dst_id, origin, self._clock()),
            )
            return cur.rowcount == 1

    def remove_edge(self, profile: str, src: str, edge: str, dst: str) -> bool:
        """Remove a recorded edge; True when one was removed. `contradicts` matches in
        either direction (the edge is symmetric)."""
        self._require_graph()
        if edge not in GRAPH_EDGE_TYPES:
            raise LearningError(f"unknown edge {edge!r} (use: {', '.join(GRAPH_EDGE_TYPES)})")
        with self._tx() as db:
            if edge == "applies-to":
                src_id = str(self._lesson_number(profile, src))
                slug, tail = split_module_ref(dst)
                path = normalize_module_ref(tail)
                if path is None:
                    raise LearningError(f"{dst!r} is not a module path")
                dst_id = qualify_module(path, slug)
                cur = db.execute(
                    "DELETE FROM lesson_edges WHERE profile = ? AND edge = 'applies-to'"
                    " AND src_type = 'lesson' AND src_id = ?"
                    " AND dst_type = 'module' AND dst_id = ?",
                    (profile, src_id, dst_id),
                )
            else:
                a = str(self._lesson_number(profile, src))
                b = str(self._lesson_number(profile, dst))
                cur = db.execute(
                    "DELETE FROM lesson_edges WHERE profile = ? AND edge = ?"
                    " AND src_type = 'lesson' AND dst_type = 'lesson'"
                    " AND ((src_id = ? AND dst_id = ?) OR (src_id = ? AND dst_id = ?))",
                    (profile, edge, a, b, b, a),
                )
            return cur.rowcount > 0

    def edges_for_lesson(self, profile: str, number: int) -> list[EdgeView]:
        """Every stored edge touching the lesson, both directions, plus the `derived-from`
        edge to its source run (computed from `lessons.source_run`, never stored)."""
        self._require_graph()
        key = str(number)
        rows = self.conn.execute(
            "SELECT src_type, src_id, edge, dst_type, dst_id, origin FROM lesson_edges"
            " WHERE profile = ? AND ((src_type = 'lesson' AND src_id = ?)"
            " OR (dst_type = 'lesson' AND dst_id = ?))",
            (profile, key, key),
        ).fetchall()
        views: list[EdgeView] = []
        for row in rows:
            if row["src_type"] == "lesson" and row["src_id"] == key:
                other = self._display_endpoint(row["dst_type"], row["dst_id"])
                views.append(EdgeView("out", row["edge"], row["dst_type"], other, row["origin"]))
            else:
                other = self._display_endpoint(row["src_type"], row["src_id"])
                views.append(EdgeView("in", row["edge"], row["src_type"], other, row["origin"]))
        lesson = self.get_lesson(number)
        if lesson is not None and lesson.source_run:
            views.append(EdgeView("out", "derived-from", "run", lesson.source_run, "derived"))
        return views

    def _display_endpoint(self, endpoint_type: str, endpoint_id: str) -> str:
        if endpoint_type == "lesson":
            return self.lesson_id(int(endpoint_id))
        return endpoint_id

    def lesson_edges(self, profile: str) -> list[LessonEdge]:
        """Every stored edge of the profile (lesson endpoints as display ids)."""
        self._require_graph()
        rows = self.conn.execute(
            "SELECT src_type, src_id, edge, dst_type, dst_id, origin FROM lesson_edges"
            " WHERE profile = ? ORDER BY id",
            (profile,),
        ).fetchall()
        return [
            LessonEdge(
                self._display_endpoint(row["src_type"], row["src_id"]),
                row["edge"],
                self._display_endpoint(row["dst_type"], row["dst_id"]),
                row["origin"],
            )
            for row in rows
        ]

    def co_occurs(
        self,
        attempts_conn: sqlite3.Connection,
        profile: str,
        number: int,
        min_times: int = 2,
        limit: int = 10,
    ) -> list[tuple[str, int]]:
        """Lessons injected together with this one in at least `min_times` attempts.

        Computed on demand from `attempts.lesson_ids` (design: never materialized).
        `attempts_conn` is the repo database's connection; only ids of this database's
        scope are considered.
        """
        rows = attempts_conn.execute(
            "SELECT j2.value AS other, COUNT(*) AS n FROM attempts AS a,"
            " json_each(a.lesson_ids) AS j1, json_each(a.lesson_ids) AS j2"
            " WHERE a.lesson_ids IS NOT NULL AND json_valid(a.lesson_ids)"
            " AND j1.value = ? AND j2.value != j1.value"
            " AND substr(j2.value, 1, 1) = ?"
            " GROUP BY j2.value HAVING n >= ? ORDER BY n DESC LIMIT ?",
            (self.lesson_id(number), _PREFIX[self.scope], min_times, limit),
        ).fetchall()
        # Index access: the attempts connection may not use sqlite3.Row.
        return [(row[0], row[1]) for row in rows]

    def graph_query(
        self,
        profile: str,
        text: str,
        *,
        module: str | None = None,
        run_state: str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> list[Lesson]:
        """Full-text search over lesson text and tags (all quoted terms must match), ranked
        by score then newest — the same score/recency signal selection uses.

        `module` filters to lessons with an `applies-to` edge on the module, with
        directory-prefix semantics (`src/auth` also matches `src/auth/login.py`, qualified
        or not). `run_state` and `since` (YYYY-MM-DD) filter on the source runs' state and
        date: they need a repo-scope profile, because shared databases hold no runs.
        """
        self._require_graph()
        tokens = [word for word in _WORD.findall(text.lower()) if len(word) >= 2]
        if not tokens:
            raise LearningError("a graph query needs at least one searchable word")
        match = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        if (run_state is not None or since is not None) and self.scope != "repo":
            raise LearningError(
                "--from-runs/--since need a repo-scope profile: shared databases hold no runs"
            )
        if since is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
            raise LearningError(f"{since!r} is not a date (use YYYY-MM-DD)")
        sql = (
            "SELECT l.* FROM lessons AS l JOIN lessons_fts ON lessons_fts.rowid = l.id"
            " WHERE l.profile = ? AND lessons_fts MATCH ?"
        )
        params: list[object] = [profile, match]
        if module is not None:
            slug, tail = split_module_ref(module)
            path = normalize_module_ref(tail)
            if path is None:
                raise LearningError(f"{module!r} is not a module path")
            exact = qualify_module(path, slug)
            like = _like_escape(exact)
            sql += (
                " AND EXISTS (SELECT 1 FROM lesson_edges AS e WHERE e.profile = l.profile"
                " AND e.edge = 'applies-to' AND e.src_type = 'lesson'"
                " AND e.src_id = CAST(l.id AS TEXT) AND e.dst_type = 'module'"
                " AND (e.dst_id = ? OR e.dst_id LIKE ? ESCAPE '\\'"
                " OR e.dst_id LIKE ? ESCAPE '\\' OR e.dst_id LIKE ? ESCAPE '\\'))"
            )
            params += [exact, like + "/%", "%:" + like, "%:" + like + "/%"]
        if run_state is not None or since is not None:
            sql += " AND l.source_run IN (SELECT id FROM runs WHERE 1 = 1"
            if run_state is not None:
                sql += " AND state = ?"
                params.append(run_state)
            if since is not None:
                sql += " AND created_at >= ?"
                params.append(since)
            sql += ")"
        sql += " ORDER BY l.score DESC, l.id DESC LIMIT ?"
        params.append(limit)
        return [self._lesson(row) for row in self.conn.execute(sql, params)]

    def graph_stats(self, profile: str) -> GraphStats:
        """Lesson/edge counts, open contradictions, and the most-applied-to modules."""
        self._require_graph()
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, SUM(retired) AS r FROM lessons WHERE profile = ?",
            (profile,),
        ).fetchone()
        edges = {
            r["edge"]: r["n"]
            for r in self.conn.execute(
                "SELECT edge, COUNT(*) AS n FROM lesson_edges WHERE profile = ? GROUP BY edge",
                (profile,),
            )
        }
        edges["derived-from"] = self.conn.execute(
            "SELECT COUNT(*) FROM lessons WHERE profile = ? AND source_run IS NOT NULL",
            (profile,),
        ).fetchone()[0]
        contradictions_open = self.conn.execute(
            """SELECT COUNT(*) FROM (
                   SELECT DISTINCT
                       CASE WHEN CAST(src_id AS INTEGER) < CAST(dst_id AS INTEGER)
                            THEN src_id ELSE dst_id END AS a,
                       CASE WHEN CAST(src_id AS INTEGER) < CAST(dst_id AS INTEGER)
                            THEN dst_id ELSE src_id END AS b
                   FROM lesson_edges
                   WHERE profile = ? AND edge = 'contradicts'
                     AND src_type = 'lesson' AND dst_type = 'lesson'
               ) AS p
               JOIN lessons AS l1 ON l1.id = CAST(p.a AS INTEGER) AND l1.profile = ?
               JOIN lessons AS l2 ON l2.id = CAST(p.b AS INTEGER) AND l2.profile = ?
               WHERE l1.retired = 0 AND l2.retired = 0""",
            (profile, profile, profile),
        ).fetchone()[0]
        top_modules = [
            (r["dst_id"], r["n"])
            for r in self.conn.execute(
                "SELECT dst_id, COUNT(*) AS n FROM lesson_edges WHERE profile = ?"
                " AND edge = 'applies-to' GROUP BY dst_id ORDER BY n DESC, dst_id LIMIT 10",
                (profile,),
            )
        ]
        return GraphStats(
            lessons=row["n"],
            retired=row["r"] or 0,
            edges=edges,
            contradictions_open=contradictions_open,
            top_modules=top_modules,
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
        assert db is not None
        return db.create_profile(name)

    def ensure(self, name: str) -> Profile:
        """The profile, created in repo scope if it does not exist yet."""
        return self.get(name) or self.create(name)

    def set_enabled(self, name: str, enabled: bool) -> None:
        self.db_for(name).set_enabled(name, enabled)

    def flush(self, name: str) -> int:
        return self.db_for(name).flush(name)

    def laya_weights(self, name: str) -> dict[str, float] | None:
        return self.db_for(name).get_laya_weights(name)

    def set_laya_weights(self, name: str, weights: dict[str, float] | None) -> None:
        self.db_for(name).set_laya_weights(name, weights)

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


# ---- which profile a run uses ------------------------------------------------------------


@dataclass(frozen=True)
class LearningStatus:
    """The repo's learning switch and active profile, with where each value came from."""

    enabled: bool
    enabled_source: str  # "the switch" | "jev.toml" | "default"
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
        enabled_source="the switch"
        if enabled is not None
        else ("jev.toml" if "enabled" in set_in_config else "default"),
        profile=profile or lc.profile,
        profile_source="the switch"
        if profile
        else ("jev.toml" if "profile" in set_in_config else "default"),
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


def use_profile(store: Store, learning: Learning, name: str) -> None:
    """Make an existing profile the repo's active one (`jev learning use`)."""
    if learning.get(check_profile_name(name)) is None:
        raise LearningError(f"no learning profile named {name!r}; create it first")
    set_active_profile(store, name)


def enable_learning(
    store: Store, config: JevConfig, learning: Learning, name: str | None = None
) -> str:
    """`jev learning enable [NAME]`: without a name, turn learning on for the repo (creating
    and enabling the active profile); with one, re-enable that profile. Returns the profile."""
    if name is not None:
        learning.set_enabled(name, True)
        return name
    active = learning_status(store, config).profile
    learning.ensure(active)
    learning.set_enabled(active, True)
    set_repo_learning(store, True)
    return active


def disable_learning(store: Store, learning: Learning, name: str | None = None) -> None:
    """`jev learning disable [NAME]`: learning off for the repo, or one profile disabled.
    Lessons are kept either way."""
    if name is None:
        set_repo_learning(store, False)
    else:
        learning.set_enabled(name, False)


def delete_profile(store: Store, learning: Learning, name: str) -> None:
    """Delete a profile and its lessons; the repo falls back to the configured profile when it
    was the active one."""
    learning.delete(name)
    if store.get_setting(SETTING_PROFILE) == name:
        set_active_profile(store, None)


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


def suppress_contradictions(
    db: LessonDB,
    attempts: sqlite3.Connection,
    profile: str,
    chosen: list[Lesson],
) -> tuple[list[Lesson], list[Suppression]]:
    """Drop the lower-scored side of unresolved `contradicts` pairs from `chosen`.

    Advisory edges, evidence-gated suppression (the safe default): a pair is only
    suppressed when both lessons were selected AND the pair has actually been injected
    together before (`co_injected` over `attempts.lesson_ids`) — never on the edge's
    assertion alone. Deterministic: the higher score wins; ties keep the older lesson.
    Without the graph tables (a repo database before its graph migration) nothing is
    suppressed.
    """
    if not db.graph_ready():
        return chosen, []
    ids = {lesson.id for lesson in chosen}
    pairs: set[tuple[str, str]] = set()
    for lesson in chosen:
        for view in db.edges_for_lesson(profile, lesson.number):
            if (
                view.edge == "contradicts"
                and view.other_type == "lesson"
                and view.other in ids
                and view.other != lesson.id
            ):
                first, second = sorted((lesson.id, view.other))
                pairs.add((first, second))
    by_id = {lesson.id: lesson for lesson in chosen}
    dropped: set[str] = set()
    suppressions: list[Suppression] = []
    for first, second in sorted(pairs):
        if first in dropped or second in dropped:
            continue
        one, two = by_id[first], by_id[second]
        winner, loser = (
            (one, two) if (one.score, -one.number) >= (two.score, -two.number) else (two, one)
        )
        if not co_injected(attempts, first, second):
            continue  # asserted but never observed together: leave both in
        dropped.add(loser.id)
        suppressions.append(
            Suppression(
                loser.id, winner.id, f"contradicts {winner.id}; the pair co-injected before"
            )
        )
    return [lesson for lesson in chosen if lesson.id not in dropped], suppressions


__all__ = [
    "GRAPH_EDGE_TYPES",
    "LESSON_ROLES",
    "MAX_LESSON_CHARS",
    "RETIRE_AT_SCORE",
    "SETTING_ENABLED",
    "SETTING_PROFILE",
    "EdgeView",
    "GraphStats",
    "Learning",
    "LearningError",
    "LearningStatus",
    "Lesson",
    "LessonDB",
    "LessonEdge",
    "Profile",
    "Scope",
    "Suppression",
    "check_profile_name",
    "co_injected",
    "context_terms",
    "learning_status",
    "normalize_module_path",
    "normalize_module_ref",
    "qualify_module",
    "repo_slug_for",
    "resolve_learning",
    "select_lessons",
    "set_active_profile",
    "set_repo_learning",
    "shared_db_path",
    "split_module_ref",
    "suppress_contradictions",
]
