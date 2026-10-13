"""Rendering helpers: safe Markdown and the geometry of the server-side SVG bar charts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markupsafe import Markup

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
