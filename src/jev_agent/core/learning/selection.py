"""Choosing which lessons a prompt gets: context terms, deterministic ranking
(`select_lessons`), and evidence-gated contradiction suppression.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable

from jev_agent.core.learning.db import LessonDB
from jev_agent.core.learning.graph import co_injected
from jev_agent.core.learning.models import _STOPWORDS, _WORD, Lesson, Suppression


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
    "context_terms",
    "normalize_tag",
    "select_lessons",
    "suppress_contradictions",
    "tag_overlap",
]
