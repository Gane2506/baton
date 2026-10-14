"""`jev queue ...`: add, list, reorder and remove queued ideas (docs/queue.md)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jev_agent.cli.repo import RepoOption
from jev_agent.cli.run_cmds import (
    AskBeforeShipOption,
    AutoShipOption,
    QualityOption,
    quality_level,
    ship_mode_flag,
)
from jev_agent.cli.text import short
from jev_agent.cli.wiring import RepoContext, fail, repo_context
from jev_agent.config.schema import ShipMode
from jev_agent.config.servers_schema import NO_PROFILE
from jev_agent.core.learning import LearningError, check_profile_name
from jev_agent.core.queue import (
    ACTIVE_STATUSES,
    QueueError,
    add_idea,
    effective_status,
    move_item,
    remove_item,
    run_states,
)
from jev_agent.core.recurrence import (
    ScheduleError,
    describe,
    local_timezone_name,
    next_due,
    parse_schedule,
)
from jev_agent.core.store import NotFoundError, QueueItem, QueuePriority, RecurringTask
from jev_agent.quality.levels import QualityLevel
from jev_agent.servers.profiles import ProfileError, get_profile

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
    if item.ship_mode:
        flags.append("auto-ship" if item.ship_mode == "merge" else "ask-before-ship")
    return " ".join(flags)


def _format_local(moment: datetime, tz: ZoneInfo) -> str:
    """`moment` as `2026-10-15 09:30 EDT` in the user's local timezone."""
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=tz)
    return aware.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _recurring_table(tasks: list[RecurringTask]) -> Table:
    """Rich table of recurring tasks, shared by `recurring list` and `queue list`."""
    tz = ZoneInfo(local_timezone_name())
    table = Table(title="Recurring")
    table.add_column("id", no_wrap=True)
    for column in ("brief", "schedule", "next due", "status", "last run"):
        table.add_column(column, overflow="fold")
    for task in tasks:
        last = _format_local(task.last_run_at, tz) if task.last_run_at is not None else "-"
        table.add_row(
            str(task.id),
            escape(short(task.brief, 40)),
            escape(describe(parse_schedule(task.schedule_source))),
            _format_local(task.next_due_at, tz),
            "enabled" if task.enabled else "paused",
            last,
        )
    return table


def _add_recurring(
    ctx: RepoContext,
    brief: str,
    repeat: str,
    priority: QueuePriority,
    plan_only: bool,
    learning_profile: str | None,
    no_learning: bool,
    profile: str | None,
    quality: QualityLevel | None,
    ship_mode: ShipMode | None,
) -> None:
    """Create a recurring task (`--repeat`); mirrors add_idea's option checks."""
    text = brief.strip()
    if not text:
        raise fail("the brief is empty")
    if learning_profile is not None and no_learning:
        raise fail("--learning and --no-learning cannot be used together")
    if learning_profile is not None:
        try:
            check_profile_name(learning_profile)
        except LearningError as exc:
            raise fail(str(exc)) from exc
        if ctx.learning.get(learning_profile) is None:
            raise fail(
                f"no learning profile named {learning_profile!r}; "
                f"create it with `jev learning create {learning_profile}`"
            )
    if profile is not None and not profile.strip():
        raise fail("--profile needs a name")
    try:
        schedule = parse_schedule(repeat)
    except ScheduleError as exc:
        raise fail(str(exc)) from exc
    tz_name = local_timezone_name()
    tz = ZoneInfo(tz_name)
    due = next_due(schedule, datetime.now(tz), tz)
    task = ctx.store.add_recurring_task(
        text,
        schedule,
        tz_name,
        due,
        priority=priority,
        plan_only=plan_only,
        learning_profile=learning_profile,
        no_learning=no_learning,
        profile=profile,
        quality=quality,
        ship_mode=ship_mode,
    )
    console.print(
        f"recurring task [bold]#{task.id}[/bold]: {escape(describe(schedule))} "
        f"(next run {_format_local(due, tz)})"
    )


@queue_app.command("add")
def add(
    idea: Annotated[str, typer.Argument(help="What to build, in plain words.")],
    repeat: Annotated[
        str | None,
        typer.Option(
            "--repeat",
            metavar="SCHEDULE",
            help="Schedule this as a recurring task instead of queueing it once: "
            "cron like `0 9 * * *`, or hourly, daily, weekly, monthly, "
            "daily@HH:MM, weekly@day-HH:MM.",
        ),
    ] = None,
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
    auto_ship: AutoShipOption = False,
    ask_before_ship: AskBeforeShipOption = False,
    repo: RepoOption = Path("."),
) -> None:
    """Add an idea to the queue, or schedule it to repeat with --repeat."""
    level = quality_level(quality)
    with repo_context(repo) as ctx:
        mode = ship_mode_flag(ctx.config, auto_ship, ask_before_ship)
        if repeat is not None:
            _add_recurring(
                ctx,
                idea,
                repeat,
                priority.value,
                plan_only,
                learning,
                no_learning,
                profile,
                level,
                mode,
            )
            return
        if profile is not None and profile.strip() and profile != NO_PROFILE:
            try:
                get_profile(ctx.config, profile)
            except ProfileError as exc:
                raise fail(str(exc)) from exc
        try:
            item = add_idea(
                ctx.store,
                ctx.learning,
                idea,
                priority.value,
                plan_only=plan_only,
                learning_profile=learning,
                no_learning=no_learning,
                profile=profile,
                quality=level,
                ship_mode=mode,
            )
        except QueueError as exc:
            raise fail(str(exc)) from exc
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
    with repo_context(repo, create=False) as ctx:
        items = ctx.store.list_queue()
        states = run_states(ctx.store, items)
        rows = [(item, effective_status(item, states.get(item.run_id or ""))) for item in items]
        if not show_all:
            rows = [(item, s) for item, s in rows if s in ACTIVE_STATUSES]
        if rows:
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
        else:
            console.print(
                'The queue is empty. Add an idea with `jev queue add "idea"`.'
                + ("" if show_all else " (`--all` shows finished items.)")
            )
        recurring = ctx.store.list_recurring_tasks()
        if recurring:
            console.print(_recurring_table(recurring))


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
    with repo_context(repo) as ctx:
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
    with repo_context(repo) as ctx:
        try:
            remove_item(ctx.store, item_id)
        except QueueError as exc:
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
    with repo_context(repo) as ctx:
        console.print(f"removed {ctx.store.clear_queued()} queued item(s)")


recurring_app = typer.Typer(
    name="recurring",
    help="Manage recurring scheduled tasks.",
    no_args_is_help=True,
)
queue_app.add_typer(recurring_app)

RecurringId = Annotated[
    int,
    typer.Argument(help="Recurring task id, as shown by `jev queue recurring list`."),
]


@recurring_app.command("list")
def recurring_list(repo: RepoOption = Path(".")) -> None:
    """List recurring scheduled tasks."""
    with repo_context(repo, create=False) as ctx:
        tasks = ctx.store.list_recurring_tasks()
        if not tasks:
            console.print(
                'No recurring tasks. Add one with `jev queue add "brief" --repeat <schedule>`.'
            )
            return
        console.print(_recurring_table(tasks))


@recurring_app.command("pause")
def recurring_pause(task_id: RecurringId, repo: RepoOption = Path(".")) -> None:
    """Pause a recurring task; its schedule is kept."""
    with repo_context(repo) as ctx:
        try:
            ctx.store.set_recurring_enabled(task_id, False)
        except NotFoundError as exc:
            raise fail(str(exc)) from exc
        console.print(f"paused recurring task {task_id}")


@recurring_app.command("resume")
def recurring_resume(task_id: RecurringId, repo: RepoOption = Path(".")) -> None:
    """Resume a paused recurring task."""
    with repo_context(repo) as ctx:
        try:
            ctx.store.set_recurring_enabled(task_id, True)
        except NotFoundError as exc:
            raise fail(str(exc)) from exc
        console.print(f"resumed recurring task {task_id}")


@recurring_app.command("remove")
def recurring_remove(task_id: RecurringId, repo: RepoOption = Path(".")) -> None:
    """Remove a recurring task."""
    with repo_context(repo) as ctx:
        try:
            ctx.store.remove_recurring_task(task_id)
        except NotFoundError as exc:
            raise fail(str(exc)) from exc
        console.print(f"removed recurring task {task_id}")
