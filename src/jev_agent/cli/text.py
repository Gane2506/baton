"""Small text helpers shared by the terminal commands (and the dashboard)."""

from __future__ import annotations


def short(text: str, width: int) -> str:
    """`text` on one line, cut to `width` characters with `...`."""
    line = " ".join(text.split())
    return line if len(line) <= width else line[: max(width - 3, 1)] + "..."
