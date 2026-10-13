"""Benchmarks: each KPI as a series over the last shipped PRs, its rolling median, and
regressions (docs/ship.md "Benchmarks").

A point is a regression when it is clearly worse than the median of the points before it:

- for counts, costs and times (`kind` "ratio"): more than `RATIO_FACTOR` (1.5) times that
  median when higher is worse, or less than the median divided by it when lower is worse
  (never flagged against a median of 0, where any change would be "infinitely" worse);
- for rates and scores between 0 and 1 (`kind` "rate"): more than `RATE_POINTS` (0.15, i.e.
  15 percentage points) past that median in the bad direction.

The median is over the up to `window` (5) previous values, and needs at least `min_history`
(3) of them. Everything here is pure arithmetic on lists; `trends` applies it to `PrKpis`.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from jev_agent.core.kpi import PrKpis

Direction = Literal["higher_worse", "lower_worse"]
Kind = Literal["ratio", "rate"]
Unit = Literal["usd", "hours", "count", "percent", "score"]

WINDOW = 5
MIN_HISTORY = 3
RATIO_FACTOR = 1.5
RATE_POINTS = 0.15
DEFAULT_LAST = 20


@dataclass(frozen=True)
class KpiDef:
    key: str  # `PrKpis.value(key)`
    label: str
    unit: Unit
    direction: Direction
    kind: Kind

    def format(self, value: float | None) -> str:
        return format_value(value, self.unit)


KPI_DEFS: tuple[KpiDef, ...] = (
    KpiDef("cost_usd", "Cost per PR", "usd", "higher_worse", "ratio"),
    KpiDef("wall_clock_h", "Run time", "hours", "higher_worse", "ratio"),
    KpiDef("attempts_per_task", "Attempts per task", "count", "higher_worse", "ratio"),
    KpiDef("first_pass_rate", "First-pass rate", "percent", "lower_worse", "rate"),
    KpiDef("fallback_rate", "Rules fallback rate", "percent", "higher_worse", "rate"),
    KpiDef("mean_jev_confidence", "Mean Jev confidence", "score", "lower_worse", "rate"),
    KpiDef("findings_total", "Quality findings", "count", "higher_worse", "ratio"),
    KpiDef("time_to_merge_h", "Time to merge", "hours", "higher_worse", "ratio"),
    KpiDef("review_comments", "Review comments", "count", "higher_worse", "ratio"),
)
KPI_BY_KEY = {kpi.key: kpi for kpi in KPI_DEFS}


def format_value(value: float | None, unit: Unit) -> str:
    if value is None:
        return "-"
    if unit == "usd":
        return f"${value:.2f}"
    if unit == "hours":
        return f"{value * 60:.0f}m" if value < 1 else f"{value:.1f}h"
    if unit == "percent":
        return f"{value * 100:.0f}%"
    if unit == "score":
        return f"{value:.2f}"
    return f"{value:.1f}" if value != int(value) else f"{value:.0f}"


def median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def rolling_median(values: Sequence[float], window: int = WINDOW) -> list[float]:
    """Point i: the median of values[i-window+1 .. i] (fewer at the start)."""
    return [statistics.median(values[max(0, i - window + 1) : i + 1]) for i in range(len(values))]


def is_worse(value: float, baseline: float, kpi: KpiDef) -> bool:
    """`value` is a regression against `baseline` (see the module docstring)."""
    if kpi.kind == "rate":
        if kpi.direction == "higher_worse":
            return value > baseline + RATE_POINTS + 1e-9
        return value < baseline - RATE_POINTS - 1e-9
    if baseline <= 0:
        return False
    if kpi.direction == "higher_worse":
        return value > baseline * RATIO_FACTOR + 1e-9
    return value < baseline / RATIO_FACTOR - 1e-9


@dataclass(frozen=True)
class TrendPoint:
    label: str  # "#12"
    value: float
    rolling: float  # rolling median up to and including this point
    baseline: float | None  # median of the previous points it was compared with
    regression: bool


@dataclass(frozen=True)
class Trend:
    kpi: KpiDef
    points: list[TrendPoint]

    @property
    def latest(self) -> TrendPoint | None:
        return self.points[-1] if self.points else None

    @property
    def regressed(self) -> bool:
        """The latest point is a regression."""
        return self.latest is not None and self.latest.regression

    def message(self) -> str:
        """`Cost per PR: #21 $1.92 is worse than the median $0.61 of the 5 PRs before`."""
        point = self.latest
        if point is None or not point.regression or point.baseline is None:
            return ""
        return (
            f"{self.kpi.label}: {point.label} {self.kpi.format(point.value)} is worse than the "
            f"median {self.kpi.format(point.baseline)} of the PRs before it"
        )


def trend(
    kpi: KpiDef,
    series: Sequence[tuple[str, float | None]],
    window: int = WINDOW,
    min_history: int = MIN_HISTORY,
) -> Trend:
    """The KPI's points (labels with values; None values are left out), rolling medians and
    regression flags."""
    known = [(label, value) for label, value in series if value is not None]
    values = [value for _, value in known]
    rolling = rolling_median(values, window)
    points: list[TrendPoint] = []
    for i, (label, value) in enumerate(known):
        previous = values[max(0, i - window) : i]
        baseline = statistics.median(previous) if len(previous) >= min_history else None
        flagged = baseline is not None and is_worse(value, baseline, kpi)
        points.append(TrendPoint(label, value, rolling[i], baseline, flagged))
    return Trend(kpi, points)


def trends(rows: Sequence[PrKpis], last: int = DEFAULT_LAST) -> list[Trend]:
    """One trend per KPI over the last `last` shipped PRs (oldest first)."""
    recent = list(rows)[-last:] if last > 0 else []
    return [trend(kpi, [(row.label, row.value(kpi.key)) for row in recent]) for kpi in KPI_DEFS]


@dataclass(frozen=True)
class Summary:
    prs: int
    merged: int
    median_cost_usd: float | None
    median_time_to_merge_h: float | None
    median_first_pass_rate: float | None
    median_fallback_rate: float | None
    median_attempts_per_task: float | None
    median_wall_clock_h: float | None

    @property
    def merged_share(self) -> float | None:
        return self.merged / self.prs if self.prs else None


def summarize(rows: Sequence[PrKpis]) -> Summary:
    """Medians over `rows` (each KPI over the PRs that have it) and the merged count."""

    def med(key: str) -> float | None:
        return median([v for row in rows if (v := row.value(key)) is not None])

    return Summary(
        prs=len(rows),
        merged=sum(1 for row in rows if row.pr.state == "merged"),
        median_cost_usd=med("cost_usd"),
        median_time_to_merge_h=med("time_to_merge_h"),
        median_first_pass_rate=med("first_pass_rate"),
        median_fallback_rate=med("fallback_rate"),
        median_attempts_per_task=med("attempts_per_task"),
        median_wall_clock_h=med("wall_clock_h"),
    )
