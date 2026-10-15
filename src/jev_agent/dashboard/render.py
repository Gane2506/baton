"""Rendering helpers: safe Markdown, and the server-side SVG bar charts (Spend) and trend
charts (KPIs)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from markdown_it import MarkdownIt
from markupsafe import Markup

from jev_agent.core.benchmarks import Trend

# CommonMark with raw HTML disabled: `<script>` and every other tag in the source is escaped,
# not passed through. markdown-it also refuses `javascript:`, `vbscript:`, `file:` and
# non-image `data:` link targets.
_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


def render_markdown(text: str) -> Markup:
    """Markdown (review.md, written partly by a model) as HTML that is safe to embed."""
    return Markup(_MARKDOWN.render(text))


# Width estimates for the 12px label and value fonts in style.css (`svg.chart`). Labels use the
# sans font, where most glyphs are narrower than this, so a label that fits by this estimate
# fits on screen; values use the mono font. Over-estimating only truncates a little early.
LABEL_CHAR_PX = 7.0
VALUE_CHAR_PX = 7.3
GAP = 6  # px between label and bar, and between bar and value
ELLIPSIS = "\u2026"


def fit_label(text: str, max_px: float, char_px: float = LABEL_CHAR_PX) -> str:
    """`text` on one line, cut with an ellipsis so it is at most `max_px` wide (estimated)."""
    line = " ".join(text.split())
    room = int(max_px // char_px)
    if len(line) <= room:
        return line
    if room <= 1:
        return ELLIPSIS if room == 1 else ""
    return line[: room - 1].rstrip() + ELLIPSIS


def usd_text(value: float) -> str:
    return f"${value:.2f}"


@dataclass(frozen=True)
class Bar:
    label: str  # the full label, used as the tooltip
    text: str  # the label as drawn: truncated to fit the label column
    value: float
    href: str | None
    y: int  # top of the bar's row
    x: int  # left edge of the bar
    width: float  # bar width in px
    value_x: float  # left edge of the value text: always after the bar's end

    @property
    def text_end(self) -> float:
        """Estimated right edge of the drawn label."""
        return len(self.text) * LABEL_CHAR_PX

    @property
    def value_text(self) -> str:
        return usd_text(self.value)


@dataclass(frozen=True)
class BarChart:
    bars: list[Bar]
    height: int
    width: int
    label_width: int
    bar_area: int
    row: int


def bar_chart(
    rows: Sequence[tuple[str, float, str | None]],
    *,
    width: int = 720,
    label_width: int = 190,
    row: int = 24,
) -> BarChart:
    """Horizontal bars, one per (label, value, link) row, scaled to the largest value.

    Labels are cut with an ellipsis to fit `label_width` (the full label stays in the tooltip),
    and each value is drawn after its bar's end, so a $0.00 bar never puts its value on top of
    the label and a long label never runs under the bar.
    """
    top = max((value for _, value, _ in rows), default=0.0)
    widest_value = max((len(usd_text(value)) for _, value, _ in rows), default=5)
    value_room = GAP + widest_value * VALUE_CHAR_PX + GAP
    bar_area = max(int(width - label_width - value_room), 0)
    bars = []
    for i, (label, value, href) in enumerate(rows):
        bar_width = (max(value, 0.0) / top * bar_area) if top > 0 else 0.0
        bars.append(
            Bar(
                label=" ".join(label.split()),
                text=fit_label(label, label_width - GAP),
                value=value,
                href=href,
                y=i * row,
                x=label_width,
                width=bar_width,
                value_x=label_width + bar_width + GAP,
            )
        )
    return BarChart(bars, max(len(bars), 1) * row, width, label_width, bar_area, row)


def render_bar_chart(chart: BarChart, empty: str = "No data yet.") -> Markup:
    """The chart as an inline SVG (or a muted paragraph when it has no bars); all text escaped."""
    if not chart.bars:
        return Markup('<p class="muted">{}</p>').format(empty)
    baseline = chart.row * 0.68
    parts = [
        Markup('<svg class="chart" viewBox="0 0 {} {}" role="img" aria-label="bar chart">').format(
            chart.width, chart.height
        )
    ]
    for bar in chart.bars:
        label = Markup('<text class="label" x="0" y="{:.1f}">{}</text>').format(
            bar.y + baseline, bar.text
        )
        if bar.href:
            label = Markup('<a href="{}">{}</a>').format(bar.href, label)
        parts.append(
            Markup(
                "<g><title>{}: {}</title>{}"
                '<rect class="bar" x="{}" y="{}" width="{:.1f}" height="{}" rx="2"></rect>'
                '<text class="value" x="{:.1f}" y="{:.1f}">{}</text></g>'
            ).format(
                bar.label,
                bar.value_text,
                label,
                bar.x,
                bar.y + 3,
                bar.width,
                chart.row - 6,
                bar.value_x,
                bar.y + baseline,
                bar.value_text,
            )
        )
    parts.append(Markup("</svg>"))
    return Markup("\n").join(parts)


# ---- daily spend columns (Spend page) ----------------------------------------------------


@dataclass(frozen=True)
class DayBar:
    day: date
    value: float
    x: float  # left edge of the column
    y: float  # top edge of the column
    width: float
    height: float


@dataclass(frozen=True)
class DailyBars:
    bars: list[DayBar]
    width: int
    height: int
    baseline: float  # y of the x axis
    max_value: float


def daily_bars(
    days: Sequence[tuple[date, float]],
    *,
    width: int = 720,
    plot_height: int = 170,
) -> DailyBars:
    """Vertical columns, one per (day, usd), oldest first.

    Every day gets a column even at $0.00 (a short stub), so the axis always reads
    as a full window and tooltips stay one-per-day. Columns scale to the largest
    value; a nonzero value always draws at least 3px so it stays visible.
    """
    rows = list(days)
    left, right, top_pad, bottom_pad = 6, 6, 26, 30
    plot_width = max(width - left - right, 1)
    baseline = top_pad + plot_height
    height = int(baseline + bottom_pad)
    top = max((usd for _, usd in rows), default=0.0)
    bars = []
    n = len(rows)
    for i, (day, usd) in enumerate(rows):
        slot = plot_width / n if n else 0.0
        gap = max(2.0, slot * 0.28)
        col_w = max(slot - gap, 1.0)
        x = left + i * slot + gap / 2
        col_h = (max(usd, 0.0) / top * plot_height) if top > 0 else 0.0
        if usd > 0:
            col_h = max(col_h, 3.0)
        elif n:
            col_h = 3.0  # the $0.00 stub: the day is still there
        bars.append(DayBar(day, usd, x, baseline - col_h, col_w, col_h))
    return DailyBars(bars, width, height, baseline, top)


def render_daily_spend(days: Sequence[tuple[date, float]]) -> Markup:
    """Template entry point: build the daily columns from (day, usd) pairs and render them."""
    return render_daily_bars(daily_bars(days))


def render_daily_bars(chart: DailyBars, empty: str = "No data yet.") -> Markup:
    """The daily columns as an inline SVG (or a muted paragraph when there are no days)."""
    if not chart.bars:
        return Markup('<p class="muted">{}</p>').format(empty)
    parts = [
        Markup(
            '<svg class="daily" viewBox="0 0 {} {}" role="img"'
            ' aria-label="Daily spend, last {} days">'
        ).format(chart.width, chart.height, len(chart.bars))
    ]
    parts.append(
        Markup('<line class="axis" x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}"/>').format(
            6, chart.baseline, chart.width - 6, chart.baseline
        )
    )
    if chart.max_value > 0:
        max_label = Markup('<text class="maxv" x="{}" y="16">{}</text>').format(
            6, usd_text(chart.max_value)
        )
        parts.append(max_label)
    for i, bar in enumerate(chart.bars):
        tick = bar.day.strftime("%b ") + str(bar.day.day)  # "Oct 5", never "Oct 05"
        tip = f"{bar.day.isoformat()}: {usd_text(bar.value)}"
        cls = "col zero" if bar.value == 0 else "col"
        col = Markup(
            '<g><title>{}</title><rect class="{}" x="{:.1f}" y="{:.1f}"'
            ' width="{:.1f}" height="{:.1f}" rx="2"/></g>'
        ).format(tip, cls, bar.x, bar.y, bar.width, bar.height)
        parts.append(col)
        if i % 7 == 0:
            # Keep the first/last tick inside the viewBox: anchor the edges inward.
            cx = bar.x + bar.width / 2
            anchor = "middle"
            if i == 0:
                cx, anchor = 6.0, "start"
            elif bar.x + bar.width / 2 + 24 > chart.width - 6:
                cx, anchor = float(chart.width - 6), "end"
            parts.append(
                Markup('<text class="tick" x="{:.1f}" y="{:.1f}"').format(cx, chart.baseline + 20)
                + Markup(' text-anchor="{}">{}</text>').format(anchor, tick)
            )
    parts.append(Markup("</svg>"))
    return Markup("\n").join(parts)


# ---- trend charts (KPIs page) ------------------------------------------------------------


@dataclass(frozen=True)
class TrendGeometry:
    """Where each point of a trend chart goes: x/y in px for the values and rolling medians."""

    width: int
    height: int
    left: int
    right: int
    top: int
    bottom: int
    low: float
    high: float
    xs: list[float]
    ys: list[float]
    medians: list[float]


def trend_geometry(
    values: Sequence[float],
    medians: Sequence[float],
    *,
    width: int = 360,
    height: int = 150,
    left: int = 58,
    right: int = 10,
    top: int = 10,
    bottom: int = 24,
) -> TrendGeometry:
    """Scale the points into the plot area: x evenly spaced, y from the lowest to the highest
    value (values and medians), higher values up. A flat series sits in the middle."""
    every = [*values, *medians]
    low = min(every, default=0.0)
    high = max(every, default=0.0)
    if high - low < 1e-12:
        low, high = low - 1.0, high + 1.0
    plot_w = width - left - right
    plot_h = height - top - bottom
    n = len(values)

    def x(i: int) -> float:
        return left + (plot_w / 2 if n == 1 else plot_w * i / (n - 1))

    def y(v: float) -> float:
        return top + plot_h * (1 - (v - low) / (high - low))

    return TrendGeometry(
        width,
        height,
        left,
        right,
        top,
        bottom,
        low,
        high,
        [x(i) for i in range(n)],
        [y(v) for v in values],
        [y(m) for m in medians],
    )


def render_trend_chart(trend: Trend, empty: str = "No data yet.") -> Markup:
    """A `core.benchmarks.Trend` as an inline SVG: the values as a line with points, the
    rolling median as a dashed line, regressions as larger red points with a tooltip. Every
    piece of text is escaped; styling comes from style.css classes (the CSP forbids inline
    styles)."""
    points = trend.points
    if not points:
        return Markup('<p class="muted small">{}</p>').format(empty)
    kpi = trend.kpi
    g = trend_geometry([p.value for p in points], [p.rolling for p in points])

    def poly(ys: Sequence[float]) -> str:
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(g.xs, ys, strict=True))

    bottom_y = g.height - g.bottom
    parts = [
        Markup(
            '<svg class="trend" viewBox="0 0 {} {}" role="img" aria-label="{} over {} PRs">'
        ).format(g.width, g.height, kpi.label, len(points)),
        Markup('<line class="axis" x1="{}" y1="{}" x2="{}" y2="{}"></line>').format(
            g.left, bottom_y, g.width - g.right, bottom_y
        ),
        Markup('<text class="tick" x="{}" y="{:.1f}" text-anchor="end">{}</text>').format(
            g.left - 6, g.top + 9, kpi.format(g.high)
        ),
        Markup('<text class="tick" x="{}" y="{:.1f}" text-anchor="end">{}</text>').format(
            g.left - 6, bottom_y, kpi.format(g.low)
        ),
        Markup('<text class="tick" x="{:.1f}" y="{}" text-anchor="start">{}</text>').format(
            g.xs[0], g.height - 6, points[0].label
        ),
    ]
    if len(points) > 1:
        parts.append(
            Markup('<text class="tick" x="{:.1f}" y="{}" text-anchor="end">{}</text>').format(
                g.xs[-1], g.height - 6, points[-1].label
            )
        )
        parts.append(
            Markup('<polyline class="median" points="{}"></polyline>').format(poly(g.medians))
        )
        parts.append(Markup('<polyline class="line" points="{}"></polyline>').format(poly(g.ys)))
    for p, x, y in zip(points, g.xs, g.ys, strict=True):
        tip = f"{p.label}: {kpi.format(p.value)} (rolling median {kpi.format(p.rolling)})"
        if p.regression and p.baseline is not None:
            tip += f"; regression vs. median {kpi.format(p.baseline)} of the PRs before"
        parts.append(
            Markup(
                '<circle class="{}" cx="{:.1f}" cy="{:.1f}" r="{}"><title>{}</title></circle>'
            ).format("pt bad" if p.regression else "pt", x, y, 5 if p.regression else 3, tip)
        )
    parts.append(Markup("</svg>"))
    return Markup("\n").join(parts)
