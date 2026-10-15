"""Learning profile use cases shared by the CLI and the web UI (issue #92).

The actions behind `jev learning ...` and the dashboard's learning page: typed inputs,
the effect, and the data for the front end to report. Nothing here prints, prompts or
exits; problems are `LearningError` (a `ValueError`) with user-facing messages.
"""

from __future__ import annotations

from dataclasses import dataclass

from jev_agent.config.schema import JevConfig
from jev_agent.core.learning import (
    Learning,
    LearningStatus,
    Lesson,
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
