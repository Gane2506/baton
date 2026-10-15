"""`jev board`: a kanban view of the queue and every run (docs/queue.md).

The data model (`build_board`, `load_board`, `Card`, the column maps) lives in
`jev_agent.app.board` (issue #92); this module keeps the rich rendering and the command.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from jev_agent.app.board import (
    COLUMN_FOR_STATE,
    COLUMNS,
    DONE_LIMIT,
    RUN_STATE_PHRASE,
    TASK_STATE_PHRASE,
    Card,
    build_board,
    current_task,
    load_board,
    state_legend,
)
from jev_agent.app.format import format_age
from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import repo_context

__all__ = [
    "COLUMNS",
    "COLUMN_FOR_STATE",
    "DONE_LIMIT",
    "RUN_STATE_PHRASE",
    "TASK_STATE_PHRASE",
    "Card",
    "build_board",
    "current_task",
    "format_age",
    "load_board",
    "state_legend",
]

console = Console()

WIDE_MIN_WIDTH = 140  # below this, columns are stacked vertically
_STYLE = {"Done": "green", "Awaiting approval": "yellow", "Blocked": "red", "Running": "cyan"}


def _card_text(card: Card) -> Text:
    """Title, then the reference, then the detail, then spend and age, one per line."""
    text = Text()
    text.append(card.title, style="bold")
    text.append("\n" + card.ref, style="dim")
    if card.detail:
        text.append("\n" + card.detail)
    meta = [f"${card.spend_usd:.2f}"] if card.spend_usd is not None else []
    if card.age:
        meta.append(card.age)
    if meta:
        text.append("\n" + " | ".join(meta), style="dim")
    return text


def _column_body(cards: list[Card]) -> RenderableType:
    if not cards:
        return Text("-", style="dim")
    parts: list[RenderableType] = []
    for i, card in enumerate(cards):
        if i:
            parts.append(Text(""))
        parts.append(_card_text(card))
    return Group(*parts)


def render_board(board: dict[str, list[Card]], width: int) -> RenderableType:
    """Columns side by side when `width` allows, else one panel per column, stacked."""
    if width >= WIDE_MIN_WIDTH:
        table = Table(expand=True, show_lines=False, padding=(0, 1))
        for name in COLUMNS:
            style = _STYLE.get(name, "")
            table.add_column(
                f"[{style}]{name}[/{style}] ({len(board[name])})"
                if style
                else f"{name} ({len(board[name])})",
                ratio=1,
                overflow="fold",
                vertical="top",
            )
        table.add_row(*(_column_body(board[name]) for name in COLUMNS))
        return table
    panels: list[RenderableType] = []
    for name in COLUMNS:
        style = _STYLE.get(name, "")
        panels.append(
            Panel(
                _column_body(board[name]),
                title=escape(f"{name} ({len(board[name])})"),
                title_align="left",
                border_style=style or "none",
            )
        )
    return Group(*panels)


def board(
    watch: Annotated[
        bool, typer.Option("--watch", "-w", help="Keep redrawing until Ctrl-C.")
    ] = False,
    interval: Annotated[
        float,
        typer.Option("--interval", min=0.2, metavar="SECONDS", help="Redraw interval."),
    ] = 2.0,
    show_all: Annotated[
        bool, typer.Option("--all", help=f"Show every done run (default: last {DONE_LIMIT}).")
    ] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Kanban board: queued ideas and every run by stage."""
    with repo_context(repo, create=False) as ctx:

        def view() -> RenderableType:
            data = load_board(ctx.store, datetime.now(UTC), show_all=show_all)
            return render_board(data, console.width)

        if not watch:
            console.print(view())
            return
        try:
            with Live(view(), console=console, auto_refresh=False, screen=False) as live:
                while True:
                    time.sleep(interval)
                    live.update(view(), refresh=True)
        except KeyboardInterrupt:
            pass
