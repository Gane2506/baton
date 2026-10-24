"""Benchmarks (issue #201 note: `core/benchmarks.py` had no dedicated test file).

Pure arithmetic on KPI series: medians, rolling medians, regression flags,
trend messages and summaries. `PrKpis` rows here are built directly from
`RunKpis`/`ShippedPrRecord` — no store needed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jev_agent.core.benchmarks import (
    KPI_BY_KEY,
    format_value,
    is_worse,
    median,
    rolling_median,
    summarize,
    trend,
    trends,
)
from jev_agent.core.kpi import PrKpis, RunKpis
from jev_agent.core.store import ShippedPrRecord


def _row(
    number: int, *, cost: float, merged: bool = True, first_pass: float | None = None
) -> PrKpis:
    return PrKpis(
        ShippedPrRecord(
            id=number,
            run_id=f"r{number}",
            remote="origin",
            repo="o/r",
            branch=f"jev/r{number}/result",
            base="main",
            number=number,
            url=None,
            title=f"PR {number}",
            draft=False,
            head_oid=None,
            pushed_commits=1,
            created_at=datetime.now(UTC),
            state="merged" if merged else "open",
        ),
        RunKpis(
            run_id=f"r{number}",
            total_cost_usd=cost,
            wall_clock_s=3600.0,
            tasks=1,
            tasks_done=1,
            attempts_total=1,
            attempts_per_task=1.0,
            first_pass_rate=first_pass,
            retries=0,
            escalations=0,
            reassigns=0,
            fix_rounds=0,
            quality_level="standard",
        ),
    )


def test_median_and_rolling_median() -> None:
    assert median([]) is None
    assert median([2.0, 1.0, 3.0]) == 2.0
    assert rolling_median([1.0, 2.0, 3.0, 4.0], window=2) == [1.0, 1.5, 2.5, 3.5]
    assert rolling_median([]) == []


def test_is_worse_ratio_and_rate() -> None:
    cost = KPI_BY_KEY["cost_usd"]  # ratio, higher worse, factor 1.5
    assert is_worse(1.6, 1.0, cost) is True
    assert is_worse(1.5, 1.0, cost) is False  # exactly at the factor: not worse
    assert is_worse(0.5, 1.0, KPI_BY_KEY["wall_clock_h"]) is False
    assert is_worse(0.0, 0.0, cost) is False  # never flagged against a 0 median
    rate = KPI_BY_KEY["first_pass_rate"]  # rate, lower worse, 0.15 points
    assert is_worse(0.70, 0.90, rate) is True
    assert is_worse(0.80, 0.90, rate) is False


def test_format_value_units() -> None:
    assert format_value(None, "usd") == "-"
    assert format_value(1.5, "usd") == "$1.50"
    assert format_value(0.5, "hours") == "30m"
    assert format_value(2.0, "hours") == "2.0h"
    assert format_value(0.85, "percent") == "85%"
    assert format_value(0.923, "score") == "0.92"
    assert format_value(3.0, "count") == "3"


def test_trend_flags_a_regression_only_with_enough_history() -> None:
    cost = KPI_BY_KEY["cost_usd"]
    short = trend(cost, [("#1", 1.0), ("#2", 1.0), ("#3", 9.0)])
    assert short.points[2].baseline is None  # only 2 points before: no flag
    assert short.points[2].regression is False
    full = trend(cost, [("#1", 1.0), ("#2", 1.0), ("#3", 1.0), ("#4", 9.0)])
    assert full.points[3].baseline == 1.0
    assert full.points[3].regression is True
    assert full.regressed is True
    assert "Cost per PR" in full.message() and "#4" in full.message()
    assert trend(cost, [("#1", 1.0), ("#2", 1.0), ("#3", 1.0), ("#4", 1.1)]).message() == ""


def test_trend_skips_missing_values() -> None:
    cost = KPI_BY_KEY["cost_usd"]
    result = trend(cost, [("#1", 1.0), ("#2", None), ("#3", 1.0), ("#4", 1.0), ("#5", 9.0)])
    assert [p.label for p in result.points] == ["#1", "#3", "#4", "#5"]
    assert result.points[-1].regression is True


def test_trends_and_summarize_over_rows() -> None:
    rows = [_row(1, cost=1.0), _row(2, cost=2.0), _row(3, cost=3.0, merged=False)]
    all_trends = trends(rows)
    assert {t.kpi.key for t in all_trends} == {k.key for k in KPI_BY_KEY.values()}
    # Every KPI gets one trend; only KPIs with values get points (cost has all 3,
    # value-less KPIs like time-to-merge have none here).
    cost_trend = next(t for t in all_trends if t.kpi.key == "cost_usd")
    assert len(cost_trend.points) == 3
    assert all(len(t.points) <= 3 for t in all_trends)
    summary = summarize(rows)
    assert summary.prs == 3
    assert summary.merged == 2
    assert summary.merged_share == 2 / 3
    assert summary.median_cost_usd == 2.0
    assert summarize([]).merged_share is None
    assert summarize([]).median_cost_usd is None
