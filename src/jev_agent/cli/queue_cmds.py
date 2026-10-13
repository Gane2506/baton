"""`jev queue ...`: add, list, reorder and remove queued ideas (docs/queue.md)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jev_agent.cli.repo import RepoOption
from jev_agent.cli.run_cmds import QualityOption, quality_level
from jev_agent.cli.wiring import cli_context, fail
from jev_agent.core.learning import LearningError, check_profile_name
from jev_agent.core.queue import (
    ACTIVE_STATUSES,
    QueueError,
    effective_status,
    move_item,
    run_states,
)
from jev_agent.core.store import NotFoundError, QueueItem

console = Console()

queue_app = typer.Typer(
    name="queue",
    help="Ideas waiting to be run by `jev scheduler`.",
    no_args_is_help=True,
)

ItemId = Annotated[int, typer.Argument(help="Queue item id, as shown by `jev queue list`.")]


class Priority(StrEnum):
    high = "high"
    normal = "normal"
    low = "low"


def short(text: str, width: int) -> str:
    """`text` on one line, cut to `width` characters with `...`."""
    line = " ".join(text.split())
    return line if len(line) <= width else line[: max(width - 3, 1)] + "..."


def item_flags(item: QueueItem) -> str:
    flags = []
    if item.plan_only:
        flags.append("plan-only")
    if item.no_learning:
        flags.append("no-learning")
    elif item.learning_profile:
        flags.append(f"learning={item.learning_profile}")
    if item.profile:
        flags.append(f"profile={item.profile}")
    if item.quality:
        flags.append(f"quality={item.quality}")
    return " ".join(flags)


@queue_app.command("add")
def add(
    idea: Annotated[str, typer.Argument(help="What to build, in plain words.")],
    priority: Annotated[
        Priority, typer.Option("--priority", "-p", help="Queue priority.")
    ] = Priority.normal,
    plan_only: Annotated[
        bool, typer.Option("--plan-only", help="Stop the run after the plan is validated.")
    ] = False,
    learning: Annotated[
        str | None,
        typer.Option("--learning", metavar="PROFILE", help="Learning profile for the run."),
    ] = None,
    no_learning: Annotated[
        bool, typer.Option("--no-learning", help="Run without learning.")
    ] = False,
    profile: Annotated[
        str | None,
        typer.Option("--profile", metavar="NAME", help="Workspace profile for the run."),
    ] = None,
    quality: QualityOption = None,
    repo: RepoOption = Path("."),
) -> None:
    """Add an idea to the queue."""
    level = quality_level(quality)
    with cli_context(repo) as ctx:
        if not idea.strip():
            raise fail("the idea is empty")
        if learning is not None and no_learning:
            raise fail("--learning and --no-learning cannot be used together")
        if learning is not None:
            assert ctx.learning is not None
            try:
                check_profile_name(learning)
            except LearningError as exc:
                raise fail(str(exc)) from exc
            if ctx.learning.get(learning) is None:
                raise fail(
                    f"no learning profile named {learning!r}; "
                    f"create it with `jev learning create {learning}`"
                )
        if profile is not None and not profile.strip():
            raise fail("--profile needs a name")
        item = ctx.store.add_queue_item(
            idea.strip(),
            priority.value,
            plan_only=plan_only,
            learning_profile=learning,
            no_learning=no_learning,
            profile=profile,
            quality=level,
        )
        console.print(
            f"queued item [bold]{item.id}[/bold] ({item.priority}): {escape(short(item.idea, 60))}"
        )


@queue_app.command("list")
def list_items(
    show_all: Annotated[
        bool, typer.Option("--all", help="Include done, failed and cancelled items.")
    ] = False,
    repo: RepoOption = Path("."),
) -> None:
    """List the queue in the order the scheduler takes it."""
    with cli_context(repo) as ctx:
        items = ctx.store.list_queue()
        states = run_states(ctx.store, items)
        rows = [(item, effective_status(item, states.get(item.run_id or ""))) for item in items]
        if not show_all:
            rows = [(item, s) for item, s in rows if s in ACTIVE_STATUSES]
        if not rows:
            console.print(
                'The queue is empty. Add an idea with `jev queue add "idea"`.'
                + ("" if show_all else " (`--all` shows finished items.)")
            )
            return
        table = Table(title="queue")
        table.add_column("id", no_wrap=True)
        for column in ("priority", "status", "run", "idea", "flags", "note"):
            table.add_column(column, overflow="fold")
        for item, status in rows:
            table.add_row(
                str(item.id),
                item.priority,
                status,
                item.run_id or "-",
                escape(short(item.idea, 60)),
                escape(item_flags(item)),
                escape(short(item.note or "", 60)),
            )
        console.print(table)


@queue_app.command("move")
def move(
    item_id: ItemId,
    up: Annotated[bool, typer.Option("--up", help="One place earlier.")] = False,
    down: Annotated[bool, typer.Option("--down", help="One place later.")] = False,
    top: Annotated[bool, typer.Option("--top", help="To the front of the queue.")] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Move a queued item. Crossing into another priority band takes that band's priority."""
    if sum((up, down, top)) != 1:
        raise fail("give exactly one of --up, --down or --top")
    with cli_context(repo) as ctx:
        try:
            before = ctx.store.get_queue_item(item_id)
            after = move_item(ctx.store, item_id, "up" if up else "down" if down else "top")
        except (NotFoundError, QueueError) as exc:
            raise fail(str(exc)) from exc
        place = [q.id for q in ctx.store.list_queue(["queued"])].index(item_id) + 1
        message = f"item {item_id} is now number {place} in the queue"
        if after.priority != before.priority:
            message += f" (priority {before.priority} -> {after.priority})"
        console.print(message)


@queue_app.command("remove")
def remove(item_id: ItemId, repo: RepoOption = Path(".")) -> None:
    """Remove an item from the queue. Its run, if it has one, is kept."""
    with cli_context(repo) as ctx:
        try:
            item = ctx.store.get_queue_item(item_id)
            if item.status == "running":
                raise fail(
                    f"queue item {item_id} is running; stop the scheduler (Ctrl-C) first, "
                    "or cancel its run with `jev cancel`"
                )
            ctx.store.delete_queue_item(item_id)
        except NotFoundError as exc:
            raise fail(str(exc)) from exc
        console.print(f"removed item {item_id}")


@queue_app.command("clear")
def clear(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Confirm.")] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Remove every queued item (items with a run are kept)."""
    if not yes:
        raise fail("this removes every queued idea; add --yes to confirm")
    with cli_context(repo) as ctx:
        console.print(f"removed {ctx.store.clear_queued()} queued item(s)")
