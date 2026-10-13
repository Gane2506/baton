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


@dataclass(frozen=True)
class Bar:
    label: str
    value: float
    href: str | None
    y: int  # top of the bar's row
    width: float  # bar width in px


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
    """Horizontal bars, one per (label, value, link) row, scaled to the largest value."""
    bar_area = width - label_width - 80  # room for the value after the bar
    top = max((value for _, value, _ in rows), default=0.0)
    bars = [
        Bar(label, value, href, i * row, (value / top * bar_area) if top > 0 else 0.0)
        for i, (label, value, href) in enumerate(rows)
    ]
    return BarChart(bars, max(len(bars), 1) * row, width, label_width, bar_area, row)
