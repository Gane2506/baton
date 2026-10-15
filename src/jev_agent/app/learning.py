"""Learning profile use cases shared by the CLI and the web UI (issue #92).

The actions behind `jev learning ...` and the dashboard's learning page: typed inputs,
the effect, and the data for the front end to report. Nothing here prints, prompts or
exits; problems are `LearningError` (a `ValueError`) with user-facing messages.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jev_agent.config.schema import JevConfig
from jev_agent.core.learning import (
    EdgeView,
    GraphStats,
    Learning,
    LearningError,
    LearningStatus,
    Lesson,
    LessonEdge,
    Profile,
    delete_profile,
    disable_learning,
    enable_learning,
    learning_status,
    use_profile,
)
from jev_agent.core.store import Store


@dataclass(frozen=True)
class LearningOverview:
    """What `jev learning list` and the dashboard's learning page show."""

    status: LearningStatus
    profiles: list[Profile]


def overview(store: Store, config: JevConfig, learning: Learning) -> LearningOverview:
    """The repo's learning status and its profiles."""
    return LearningOverview(learning_status(store, config), learning.profiles())


def active_profile(store: Store, config: JevConfig) -> str:
    """The active learning profile's name."""
    return learning_status(store, config).profile


def create_profile(learning: Learning, name: str, *, shared: bool) -> Profile:
    """Create a profile (repo scope, unless `shared`). `LearningError` for a bad name."""
    return learning.create(name, shared=shared)


def use_learning_profile(store: Store, learning: Learning, name: str) -> None:
    """Make a profile the active one for this repo. `LearningError` for an unknown name."""
    use_profile(store, learning, name)


def enable(store: Store, config: JevConfig, learning: Learning, name: str | None) -> str:
    """Turn learning on for this repo (no `name`), or re-enable one profile. Returns the
    active profile's name. `LearningError` for an unknown name."""
    return enable_learning(store, config, learning, name)


def disable(store: Store, learning: Learning, name: str | None) -> None:
    """Turn learning off for this repo (no `name`), or disable one profile."""
    disable_learning(store, learning, name)


def lessons(learning: Learning, name: str, include_retired: bool = False) -> list[Lesson]:
    """A profile's lessons. `LearningError` for an unknown profile."""
    return learning.lessons(name, include_retired)


def flush(learning: Learning, name: str) -> int:
    """Delete every lesson of a profile (the profile stays); returns the count deleted.
    `LearningError` for an unknown profile."""
    learning.db_for(name)  # unknown profile: error before any destructive step
    return learning.flush(name)


def delete(store: Store, learning: Learning, name: str) -> None:
    """Delete a profile and all its lessons. `LearningError` for an unknown profile."""
    learning.db_for(name)  # unknown profile: error before any destructive step
    delete_profile(store, learning, name)


def forget(learning: Learning, lesson_id: str) -> Lesson:
    """Retire one lesson so it is never used again; returns it."""
    return learning.forget(lesson_id)


# ---- the lesson graph (`jev learning graph ...`) ----------------------------------------


@dataclass(frozen=True)
class LessonNeighborhood:
    """A lesson with its 1-hop graph neighborhood, for `graph show` and the dashboard."""

    lesson: Lesson
    edges: list[EdgeView]
    co_occurs: list[tuple[str, int]]  # (lesson id, times co-injected)


def graph_query(
    learning: Learning,
    name: str,
    text: str,
    *,
    module: str | None = None,
    run_state: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[Lesson]:
    """Full-text lesson search with module/run filters. `LearningError` for a bad query or
    an unknown profile."""
    return learning.db_for(name).graph_query(
        name, text, module=module, run_state=run_state, since=since, limit=limit
    )


def graph_show(
    learning: Learning, lesson_id: str, attempts_conn: sqlite3.Connection | None = None
) -> LessonNeighborhood:
    """A lesson and its 1-hop neighborhood. `LearningError` for an unknown id."""
    db, lesson = learning.find_lesson(lesson_id)
    co: list[tuple[str, int]] = (
        db.co_occurs(attempts_conn, lesson.profile, lesson.number) if attempts_conn else []
    )
    return LessonNeighborhood(lesson, db.edges_for_lesson(lesson.profile, lesson.number), co)


def graph_link(learning: Learning, src: str, edge: str, dst: str) -> LessonEdge:
    """Record a graph edge between two lessons (or a lesson and a module for `applies-to`).
    Both lessons must live in the same profile; cross-profile edges are deferred.
    `supersedes` is advisory: the old lesson stays active until a human retires it.
    `LearningError` for unknown ids, a duplicate edge, or a bad edge type."""
    db, lesson = learning.find_lesson(src)
    if edge != "applies-to":
        _, other = learning.find_lesson(dst)
        if other.profile != lesson.profile:
            raise LearningError(
                f"cross-profile edges are not supported: {src} is in {lesson.profile!r},"
                f" {dst} in {other.profile!r}"
            )
    if not db.add_edge(lesson.profile, lesson.id, edge, dst):
        raise LearningError(f"edge {lesson.id} {edge} {dst} is already recorded")
    return LessonEdge(lesson.id, edge, dst, "manual")


def graph_unlink(learning: Learning, src: str, edge: str, dst: str) -> bool:
    """Remove a recorded graph edge; False when there was none. `contradicts` matches in
    either direction."""
    db, lesson = learning.find_lesson(src)
    return db.remove_edge(lesson.profile, lesson.id, edge, dst)


def graph_stats(learning: Learning, name: str) -> GraphStats:
    """Lesson/edge counts, open contradictions, and top modules. `LearningError` for an
    unknown profile."""
    return learning.db_for(name).graph_stats(name)
