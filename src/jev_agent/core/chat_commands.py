"""`jev chat`'s commands: `/brief`, `/plan`, `/run`, `/queue`, ... or plain text."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from jev_agent.core.chat_learning import ChatLearning
from jev_agent.core.chat_session import ChatError
from jev_agent.core.learning import check_profile_name

Priority = Literal["high", "normal", "low"]
PRIORITIES: tuple[Priority, ...] = ("high", "normal", "low")

COMMANDS: dict[str, str] = {
    "brief": "Have the model write the current idea brief, and show it.",
    "plan": "Start a plan-only run from the brief (`jev run --plan-only`).",
    "run": "Start a run from the brief; it stops at plan approval if jev.toml asks for it.",
    "queue": "Add the brief to the idea queue: /queue [high|normal|low] (default normal).",
    "learning": "Show the learning choice; change it: /learning on|off|default|PROFILE.",
    "quality": "Show the quality gate level for runs; change it: /quality LEVEL|default.",
    "status": "Show this chat: backend, learning, brief, runs launched.",
    "cost": "Show what this chat has spent, and its budget.",
    "help": "List the commands.",
    "quit": "Leave the chat (it is saved; `jev chat --resume ID` continues it).",
}
_ALIASES = {"exit": "quit", "q": "quit", "?": "help"}


@dataclass(frozen=True)
class Command:
    name: str  # a key of COMMANDS, or "" for an unknown command
    args: tuple[str, ...] = ()
    raw: str = ""


@dataclass(frozen=True)
class Message:
    text: str


def parse_input(line: str) -> Command | Message | None:
    """One line of input: a `/command`, a message for the model, or None (blank). A line that
    starts with `//` is a message starting with `/`."""
    text = line.strip()
    if not text:
        return None
    if text.startswith("//"):
        return Message(text[1:])
    if not text.startswith("/"):
        return Message(text)
    parts = text[1:].split()
    if not parts:
        return Command("", raw=text)
    name = _ALIASES.get(parts[0].lower(), parts[0].lower())
    return Command(name if name in COMMANDS else "", tuple(parts[1:]), text)


def parse_priority(args: Sequence[str]) -> Priority:
    """`/queue` arguments to a priority; raises `ChatError` for anything else."""
    if not args:
        return "normal"
    value = args[0].lower()
    if len(args) > 1 or value not in PRIORITIES:
        raise ChatError("usage: /queue [high|normal|low]")
    return value


def parse_learning_arg(arg: str, active: str) -> ChatLearning:
    """`/learning on|off|default|PROFILE` to a choice (`on` is the repo's active profile)."""
    value = arg.strip()
    if value.lower() == "off":
        return ChatLearning(off=True)
    if value.lower() == "default":
        return ChatLearning()
    if value.lower() == "on":
        return ChatLearning(profile=active)
    return ChatLearning(profile=check_profile_name(value))


def brief_title(brief: str | None) -> str:
    """The brief's first non-empty line, without a Markdown heading mark."""
    for line in (brief or "").splitlines():
        if line.strip():
            return line.strip().lstrip("#").strip()
    return ""


__all__ = [
    "COMMANDS",
    "PRIORITIES",
    "_ALIASES",
    "Command",
    "Message",
    "Priority",
    "brief_title",
    "parse_input",
    "parse_learning_arg",
    "parse_priority",
]
