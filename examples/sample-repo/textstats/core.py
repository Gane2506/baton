"""Word counting and word length."""

from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z0-9']+")


def words(text: str) -> list[str]:
    """Split `text` into words: runs of letters, digits and apostrophes."""
    return _WORD.findall(text)


def word_count(text: str) -> int:
    """Number of words in `text`."""
    return len(words(text))


def average_word_length(text: str) -> float:
    """Mean length of the words in `text`, or 0.0 if there are none."""
    found = words(text)
    if not found:
        return 0.0
    return sum(len(w) for w in found) / len(found)
