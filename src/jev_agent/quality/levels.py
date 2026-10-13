"""Quality levels. They are cumulative: each level runs everything the levels below it run."""

from __future__ import annotations

from typing import Literal, get_args

QualityLevel = Literal["none", "basic", "medium", "production", "scalable"]
LEVELS: tuple[QualityLevel, ...] = get_args(QualityLevel)

# What each level adds, for help texts and `jev quality checks`.
LEVEL_SUMMARY: dict[QualityLevel, str] = {
    "none": "the task's own test command only (no gate)",
    "basic": "lint/format, full test suite, AI code review",
    "medium": "+ secret scan, SAST, dependency audit, coverage >= 60%",
    "production": "+ type check, coverage >= 80%, licences, security checklist; "
    "major security findings block",
    "scalable": "+ performance/scalability review, container/IaC scans, threat model; "
    "major performance findings block",
}


def rank(level: QualityLevel) -> int:
    return LEVELS.index(level)


def at_least(level: QualityLevel, minimum: QualityLevel) -> bool:
    """True when `level` includes everything `minimum` runs."""
    return rank(level) >= rank(minimum)


def parse_level(value: str) -> QualityLevel:
    """`value` as a level (case-insensitive); raises ValueError naming the allowed levels."""
    text = value.strip().lower()
    for level in LEVELS:
        if level == text:
            return level
    raise ValueError(f"unknown quality level {value!r} (one of: {', '.join(LEVELS)})")
