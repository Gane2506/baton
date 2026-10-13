"""`jev scheduler start|status`: work through the idea queue unattended (docs/queue.md)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

from jev_agent.cli.repo import RepoOption
from jev_agent.cli.run_cmds import print_warnings, start_run
from jev_agent.cli.wiring import NO_WORKERS_MESSAGE, CliContext, cli_context, fail
from jev_agent.core.queue import queue_counts
from jev_agent.core.scheduler import (
    Scheduler,
    SchedulerLock,
    SchedulerLocked,
    SchedulerOptions,
    scheduler_lock,
    spent_today,
)
from jev_agent.core.store import QueueItem, RunRecord

console = Console()

scheduler_app = typer.Typer(
    name="scheduler",
    help="Run queued ideas one after another, in the foreground.",
    no_args_is_help=True,
)


def apply_profile(item: QueueItem) -> dict[str, Any]:
    """Extra keyword arguments for `start_run` that apply the item's workspace profile
    (docs/servers.md). Items without one use the repo default, like `jev run`; an unknown
    profile fails the item (ProfileError), never runs it with other access."""
    return {} if item.profile is None else {"profile": item.profile}


async def start_item(ctx: CliContext, item: QueueItem) -> RunRecord:
    """Start the run for a queue item: the same code path as `jev run`."""
    return await start_run(
        ctx,
        item.idea,
        plan_only=item.plan_only,
        learning=item.learning_profile,
        no_learning=item.no_learning,
        **({"quality": item.quality} if item.quality else {}),
        **apply_profile(item),
    )


def lock_for(ctx: CliContext) -> SchedulerLock:
    return scheduler_lock(ctx.root)


@scheduler_app.command("start")
def start(
    once: Annotated[
        bool, typer.Option("--once", help="Run at most one queued idea, then exit.")
    ] = False,
    max_runs: Annotated[
        int | None,
        typer.Option("--max-runs", min=1, metavar="N", help="Exit after starting N runs."),
    ] = None,
    poll: Annotated[
        float,
        typer.Option(
            "--poll", min=0.1, metavar="SECONDS", help="Wait this long when the queue is empty."
        ),
    ] = 30.0,
    daily_budget_usd: Annotated[
        float | None,
        typer.Option(
            "--daily-budget-usd",
            min=0.0,
            metavar="USD",
            help="Stop picking ideas once runs started today (UTC) have spent this much.",
        ),
    ] = None,
    repo: RepoOption = Path("."),
) -> None:
    """Start the scheduler in the foreground. Ctrl-C stops it; the interrupted run stays
    resumable with `jev resume`."""
    with cli_context(repo) as ctx:
        if not ctx.has_workers:
            raise fail(NO_WORKERS_MESSAGE)
        lock = lock_for(ctx)
        try:
            lock.acquire()
        except SchedulerLocked as exc:
            raise fail(str(exc)) from exc
        try:
            print_warnings(ctx)
            options = SchedulerOptions(
                once=once, max_runs=max_runs, poll_s=poll, daily_budget_usd=daily_budget_usd
            )
            console.print(
                "scheduler started"
                + (" (one item)" if once else f"; polling every {poll:g}s, Ctrl-C to stop")
            )

            async def _start(item: QueueItem) -> RunRecord:
                return await start_item(ctx, item)

            scheduler = Scheduler(
                ctx.store,
                _start,
                options,
                lock=lock,
                log=lambda message: console.print(escape(message), highlight=False),
            )
            summary = scheduler.run()
        finally:
            lock.release()
        console.print(
            f"scheduler stopped: {escape(summary.stopped_because)}; started {summary.runs} run(s)"
        )


@scheduler_app.command("status")
def status(repo: RepoOption = Path(".")) -> None:
    """Show whether a scheduler is running, what it works on, and queue counts."""
    with cli_context(repo) as ctx:
        holder = lock_for(ctx).read()
        if holder is None:
            console.print("scheduler: not running")
        else:
            console.print(
                f"scheduler: running (pid {holder.pid} on {escape(holder.host)}, "
                f"since {escape(holder.started_at)})"
            )
            if holder.current_item is not None:
                console.print(f"  current item: {holder.current_item}")
        counts = queue_counts(ctx.store)
        console.print("queue: " + ", ".join(f"{count} {name}" for name, count in counts.items()))
        console.print(
            f"spent today (UTC, all runs): ${spent_today(ctx.store, datetime.now(UTC)):.2f}"
        )
