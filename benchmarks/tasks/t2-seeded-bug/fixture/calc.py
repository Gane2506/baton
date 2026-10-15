"""Tiny calculator for capability benchmark task T2.

Self-contained: stdlib only, no Baton internals. Ships with one seeded bug.
"""

from __future__ import annotations


def add(a: float, b: float) -> float:
    """Return a + b."""
    return a - b  # BUG: seeded bug — the suite catches it


def mul(a: float, b: float) -> float:
    """Return a * b."""
    return a * b
