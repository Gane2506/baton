"""`jev chat`'s system prompt: the model's instructions (`roles/chat/prompt.md`), rendered with
a short repo summary and the chat profile's planner lessons, plus the `<brief>` markers the
model is asked to wrap the idea brief in."""

from __future__ import annotations

from collections.abc import Sequence
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, StrictUndefined

if TYPE_CHECKING:
    from jev_agent.core.learning import Lesson

BRIEF_OPEN = "<brief>"
BRIEF_CLOSE = "</brief>"
BRIEF_REQUEST = (
    "Write the idea brief for jev's planner now, from everything we agreed so far. Use the "
    "format from your instructions and put the whole brief between a line `<brief>` and a "
    "line `</brief>`. Where something is still open, choose the simplest reasonable option and "
    "list it under Assumptions."
)

README_HEAD_CHARS = 2000
TREE_MAX_ENTRIES = 60
_SKIP_DIRS = frozenset({".git", ".jev", "node_modules", "__pycache__", ".venv", "venv", "dist"})


def repo_tree(root: Path, max_entries: int = TREE_MAX_ENTRIES) -> str:
    """Top-level entries (directories end with `/`, with their direct children), truncated."""
    lines: list[str] = []

    def entries(path: Path) -> list[Path]:
        try:
            children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return []
        return [c for c in children if c.name not in _SKIP_DIRS and not c.name.startswith(".")]

    for entry in entries(root):
        if len(lines) >= max_entries:
            break
        if entry.is_dir():
            lines.append(f"{entry.name}/")
            for child in entries(entry):
                if len(lines) >= max_entries:
                    break
                lines.append(f"  {child.name}{'/' if child.is_dir() else ''}")
        else:
            lines.append(entry.name)
    if len(lines) >= max_entries:
        lines.append("... (truncated)")
    return "\n".join(lines)


def readme_head(root: Path, max_chars: int = README_HEAD_CHARS) -> str | None:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        path = root / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
            return text if len(text) <= max_chars else text[:max_chars] + "\n... (truncated)"
    return None


_env = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)


def render_system_prompt(
    root: Path, lessons: Sequence[Lesson], learning_profile: str | None
) -> str:
    """The chat model's instructions (`roles/chat/prompt.md`), with the repo summary and the
    planner lessons of the chat's profile."""
    source = files("jev_agent.roles").joinpath("chat", "prompt.md").read_text(encoding="utf-8")
    return _env.from_string(source).render(
        repo_path=str(root),
        repo_tree=repo_tree(root),
        readme=readme_head(root),
        lessons=list(lessons),
        learning_profile=learning_profile,
        brief_open=BRIEF_OPEN,
        brief_close=BRIEF_CLOSE,
    )


def extract_brief(reply: str) -> str:
    """The text between `<brief>` and `</brief>`; the whole reply if the markers are missing."""
    start = reply.find(BRIEF_OPEN)
    if start == -1:
        return reply.strip()
    body = reply[start + len(BRIEF_OPEN) :]
    end = body.find(BRIEF_CLOSE)
    return (body if end == -1 else body[:end]).strip()


__all__ = [
    "BRIEF_CLOSE",
    "BRIEF_OPEN",
    "BRIEF_REQUEST",
    "README_HEAD_CHARS",
    "TREE_MAX_ENTRIES",
    "_SKIP_DIRS",
    "_env",
    "extract_brief",
    "readme_head",
    "render_system_prompt",
    "repo_tree",
]
