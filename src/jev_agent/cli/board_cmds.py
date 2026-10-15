"""`jev board`: a kanban view of the queue and every run (docs/queue.md).

`build_board` is a pure function from runs, tasks and queue items to columns of cards;
`render_board` turns that into rich renderables (side by side when the terminal is wide,
stacked when it is narrow). `--watch` redraws from the store until Ctrl-C.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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

from jev_agent.cli.repo import RepoOption
from jev_agent.cli.text import short
from jev_agent.cli.wiring import repo_context
from jev_agent.core.models import RunState, TaskState
from jev_agent.core.store import QueueItem, RunRecord, Store, TaskRecord

console = Console()

COLUMNS: tuple[str, ...] = (
    "Backlog",
    "Planning",
    "Awaiting approval",
    "Running",
    "Done",
    "Blocked",
)
COLUMN_FOR_STATE: dict[RunState, str] = {
    RunState.INTAKE: "Planning",
    RunState.PLANNING: "Planning",
    RunState.VALIDATING: "Planning",
    RunState.AWAITING_APPROVAL: "Awaiting approval",
    # ASK_EVERY_STEP's per-step gates: each one waits on the user, like AWAITING_APPROVAL.
    RunState.AWAITING_PLANNING: "Awaiting approval",
    RunState.AWAITING_VALIDATING: "Awaiting approval",
    RunState.AWAITING_INTEGRATING: "Awaiting approval",
    RunState.AWAITING_SHIPPING: "Awaiting approval",
    RunState.EXECUTING: "Running",
    RunState.INTEGRATING: "Running",
    RunState.AWAITING_SHIP: "Awaiting approval",
    RunState.SHIPPING: "Running",
    RunState.DONE: "Done",
    RunState.NEEDS_HUMAN: "Blocked",
    RunState.FAILED: "Blocked",
    RunState.BUDGET_EXCEEDED: "Blocked",
    RunState.CANCELLED: "Blocked",
}
DONE_LIMIT = 10  # Done shows the most recent runs only, unless --all
WIDE_MIN_WIDTH = 140  # below this, columns are stacked vertically
_STYLE = {"Done": "green", "Awaiting approval": "yellow", "Blocked": "red", "Running": "cyan"}

# Plain-language phrases for run states, used on board cards instead of the
# enum names (no state-enum shouting). Every RunState must appear here.
RUN_STATE_PHRASE: dict[RunState, str] = {
    RunState.INTAKE: "intake",
    RunState.PLANNING: "planning",
    RunState.VALIDATING: "validating the plan",
    RunState.AWAITING_APPROVAL: "waiting for your approval",
    RunState.AWAITING_PLANNING: "waiting for your approval to plan",
    RunState.AWAITING_VALIDATING: "waiting for your approval to validate",
    RunState.AWAITING_INTEGRATING: "waiting for your approval to integrate",
    RunState.AWAITING_SHIPPING: "waiting for your approval to ship",
    RunState.EXECUTING: "executing",
    RunState.INTEGRATING: "integrating",
    RunState.AWAITING_SHIP: "waiting for your approval to ship",
    RunState.SHIPPING: "shipping",
    RunState.DONE: "done",
    RunState.NEEDS_HUMAN: "waiting on you",
    RunState.FAILED: "failed",
    RunState.BUDGET_EXCEEDED: "over budget",
    RunState.CANCELLED: "cancelled",
}

# Plain-language phrases for task states, for the same reason.
TASK_STATE_PHRASE: dict[TaskState, str] = {
    TaskState.PENDING: "pending",
    TaskState.READY: "ready",
    TaskState.DISPATCHED: "dispatched",
    TaskState.RUNNING: "running",
    TaskState.TESTING: "testing",
    TaskState.REVIEWING: "in review",
    TaskState.RETRY: "retrying",
    TaskState.ESCALATE: "escalated",
    TaskState.REASSIGN: "reassigning",
    TaskState.DONE: "done",
    TaskState.FAILED: "failed",
    TaskState.NEEDS_HUMAN: "needs you",
}


def state_legend() -> list[tuple[str, str]]:
    """One line per board column for the dashboard's states legend popover:

    `(column, "plain phrases of the run states that land in it")`.
    """
    lines: list[tuple[str, str]] = [("Backlog", "queued ideas, not started yet")]
    for column in COLUMNS[1:]:
        phrases = [RUN_STATE_PHRASE[s] for s, c in COLUMN_FOR_STATE.items() if c == column]
        lines.append((column, ", ".join(phrases)))
    return lines


@dataclass(frozen=True)
class Card:
    title: str  # the idea, shortened
    ref: str  # run id, or `queue #N` for backlog items
    detail: str = ""  # current task and its state, run state for blocked runs, or flags
    spend_usd: float | None = None
    age: str = ""


def format_age(since: datetime, now: datetime) -> str:
    """`45s`, `12m`, `3h`, `2d`."""
    seconds = max(0, int((now - since).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def current_task(tasks: Sequence[TaskRecord]) -> str:
    """`T2 running`: the first task (in plan order) that is not done, or ``."""
    for task in tasks:
        if task.state is not TaskState.DONE:
            return f"{task.id} {TASK_STATE_PHRASE[task.state]}"
    return ""


def build_board(
    runs: Sequence[RunRecord],
    tasks: Mapping[str, Sequence[TaskRecord]],
    queued: Sequence[QueueItem],
    now: datetime,
    *,
    show_all: bool = False,
    title_width: int = 48,
) -> dict[str, list[Card]]:
    """Group the queue's queued items and all runs into the board's columns.

    `runs` should be newest first (as `Store.list_runs` returns them); `tasks` maps run id
    to its tasks in plan order (runs without an entry show no current task); `queued` holds
    the items still waiting to be picked, in queue order.
    """
    board: dict[str, list[Card]] = {name: [] for name in COLUMNS}
    for item in queued:
        flags = [item.priority] + (["plan-only"] if item.plan_only else [])
        board["Backlog"].append(
            Card(
                short(item.idea, title_width),
                f"queue #{item.id}",
                " ".join(flags),
                None,
                format_age(item.created_at, now),
            )
        )
    for run in runs:
        column = COLUMN_FOR_STATE[run.state]
        if column == "Done" and not show_all and len(board["Done"]) >= DONE_LIMIT:
            continue
        if column == "Blocked":
            detail = RUN_STATE_PHRASE[run.state]
        elif column == "Done":
            detail = ""
        else:
            detail = current_task(tasks.get(run.id, ())) or RUN_STATE_PHRASE[run.state]
        board[column].append(
            Card(
                short(run.idea.text, title_width),
                run.id,
                detail,
                run.spend_usd,
                format_age(run.created_at, now),
            )
        )
    return board


def load_board(
    store: Store, now: datetime, show_all: bool = False, title_width: int = 48
) -> dict[str, list[Card]]:
    runs = store.list_runs()
    tasks = {
        r.id: store.list_tasks(r.id)
        for r in runs
        if COLUMN_FOR_STATE[r.state] in ("Planning", "Running", "Awaiting approval")
    }
    return build_board(
        runs, tasks, store.list_queue(["queued"]), now, show_all=show_all, title_width=title_width
    )


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
