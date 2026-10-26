"""The lesson graph (shared migration 4): `supersedes` / `contradicts` /
`applies-to` edges, FTS query, co-occurrence, and stats.

`derived-from` is a view over `lessons.source_run` and `co-occurs-with` is
computed on demand: neither is stored.
"""

from __future__ import annotations

import re
import sqlite3

from jev_agent.core.learning.base import LessonDBBase
from jev_agent.core.learning.models import (
    _EDGE_ORIGINS,
    _LESSON_ID,
    _PREFIX,
    _WORD,
    GRAPH_EDGE_TYPES,
    EdgeView,
    GraphStats,
    LearningError,
    Lesson,
    LessonEdge,
)
from jev_agent.core.learning.paths import (
    normalize_module_ref,
    qualify_module,
    split_module_ref,
)


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


class LessonGraphMixin(LessonDBBase):
    """Lesson-graph reads and writes (one `LessonDB` concern)."""

    def _require_graph(self) -> None:
        if not self.graph_ready():
            raise LearningError(
                "the lesson graph is not available in this database yet: its schema "
                "predates the graph migration (repo migration 21, shared migration 4)"
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


__all__ = ["LessonGraphMixin", "co_injected"]
