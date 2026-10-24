"""`jev chat`'s learning choice: which lessons the chat model sees and which learning flags a
launched run gets. Three states: the repo default (resolved at launch, like `jev run`
without flags), one named profile, or off."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jev_agent.core.learning import (
    Learning,
    LearningError,
    Lesson,
    Profile,
    check_profile_name,
    context_terms,
    learning_status,
    select_lessons,
)

if TYPE_CHECKING:
    from jev_agent.config.schema import JevConfig
    from jev_agent.core.store import Store


@dataclass(frozen=True)
class ChatLearning:
    """The chat's learning choice. `profile` set: that profile; `off`: no learning; neither:
    the repo default (whatever `jev run` without flags would use)."""

    profile: str | None = None
    off: bool = False

    def __post_init__(self) -> None:
        if self.profile is not None and self.off:
            raise LearningError("--learning and --no-learning cannot be used together")

    @property
    def is_default(self) -> bool:
        return self.profile is None and not self.off

    def run_flags(self) -> dict[str, Any]:
        """`learning` / `no_learning` keyword arguments for `start_run` (and the queue)."""
        return {"learning": self.profile, "no_learning": self.off}

    def describe(self) -> str:
        if self.off:
            return "off"
        if self.profile is not None:
            return f"profile {self.profile}"
        return "repo default"

    def to_json(self) -> dict[str, Any]:
        return {"profile": self.profile, "off": self.off}

    @classmethod
    def from_json(cls, data: Any) -> ChatLearning:
        if not isinstance(data, dict):
            return cls()
        profile = data.get("profile")
        profile = profile if isinstance(profile, str) else None
        off = bool(data.get("off"))
        if profile is not None and off:
            # Issue #202: a stored choice with both set is corrupt (the constructor
            # forbids it), and it used to raise LearningError out of `ChatMeta.from_json`
            # and crash chat listing for every chat. A stored choice nobody can act on
            # degrades to the repo default instead; the chat itself stays listable.
            return cls()
        return cls(profile, off)


@dataclass(frozen=True)
class LearningOption:
    key: str  # what the user types: "1", "2", "3"
    label: str
    choice: ChatLearning | None  # None: "pick another profile" (a second question follows)


def learning_options(
    active: str, active_lessons: int, others: Sequence[Profile]
) -> list[LearningOption]:
    """The start screen's choices. Option 2 is offered only when other profiles exist."""
    count = f"{active_lessons} lesson{'s' if active_lessons != 1 else ''}"
    options = [
        LearningOption("1", f"With learning: {active} ({count})", ChatLearning(profile=active))
    ]
    if others:
        options.append(LearningOption("2", "Other learning profile...", None))
    options.append(LearningOption("3", "Without learning", ChatLearning(off=True)))
    return options


def choose_learning(
    flag_profile: str | None,
    no_learning: bool,
    stored: ChatLearning | None = None,
    picked: ChatLearning | None = None,
) -> ChatLearning:
    """The chat's learning choice. Precedence: flags > the resumed chat's stored choice > what
    was picked on the start screen > the repo default."""
    if flag_profile is not None or no_learning:
        return ChatLearning(flag_profile, no_learning)
    if stored is not None:
        return stored
    if picked is not None:
        return picked
    return ChatLearning()


def effective_profile(
    choice: ChatLearning, store: Store, learning: Learning, config: JevConfig
) -> str | None:
    """The profile whose lessons the chat model sees, or None. Never creates a profile (unlike
    `resolve_learning`, which a launched run still goes through)."""
    if choice.off:
        return None
    if choice.profile is not None:
        return choice.profile if learning.get(choice.profile) is not None else None
    status = learning_status(store, config)
    if not status.enabled:
        return None
    profile = learning.get(status.profile)
    return profile.name if profile is not None and profile.enabled else None


def check_learning_choice(choice: ChatLearning, learning: Learning) -> None:
    """Raise `LearningError` if the choice names a profile that does not exist."""
    if choice.profile is not None and learning.get(check_profile_name(choice.profile)) is None:
        raise LearningError(
            f"no learning profile named {choice.profile!r}; "
            f"create it with `jev learning create {choice.profile}`"
        )


def chat_lessons(
    profile: str | None, learning: Learning, config: JevConfig, texts: Sequence[str] = ()
) -> list[Lesson]:
    """Planner lessons for the chat's system prompt (the same selection a run's planner gets).

    Only a profile that no longer exists yields no lessons: a chat saved with a profile
    that was later deleted still starts, just without lessons. Any other learning
    failure (a corrupt database, a missing table) surfaces to the caller instead of
    being swallowed — issue #202's old behaviour ran the chat with zero lessons and
    no warning, hiding the corruption.
    """
    if profile is None:
        return []
    if learning.get(profile) is None:
        return []
    lessons = learning.lessons(profile)
    terms = context_terms(texts)
    settings = config.learning
    return select_lessons(lessons, "planner", terms, settings.max_lessons, settings.max_chars)


__all__ = [
    "ChatLearning",
    "LearningOption",
    "chat_lessons",
    "check_learning_choice",
    "choose_learning",
    "effective_profile",
    "learning_options",
]
