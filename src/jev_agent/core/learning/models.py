"""Learning data: constants, the error type, and the frozen dataclasses.

Everything here is pure data (no I/O, no SQL); the DB, graph, and selection
modules build on these types.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from jev_agent.core.models import LESSON_ROLES

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
class Skill:
    """A reusable capability distilled from lessons (issue #161)."""

    id: int
    profile: str
    name: str
    description: str
    source: str  # "learned" | "manual"
    lesson_ids: tuple[str, ...]
    created_at: datetime


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


__all__ = [
    "GRAPH_EDGE_TYPES",
    "LESSON_ROLES",
    "MAX_LESSON_CHARS",
    "RETIRE_AT_SCORE",
    "SETTING_ENABLED",
    "SETTING_PROFILE",
    "EdgeView",
    "GraphStats",
    "LearningError",
    "Lesson",
    "LessonEdge",
    "Profile",
    "Scope",
    "Skill",
    "Suppression",
]
