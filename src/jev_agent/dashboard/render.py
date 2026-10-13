"""Rendering helpers: safe Markdown, and the server-side SVG bar charts (Spend) and trend
charts (KPIs)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

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
