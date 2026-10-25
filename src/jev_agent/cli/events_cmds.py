"""`jev events`: inspect a run's event-sourced execution log (docs/events.md)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from click import Command, Context
from rich.console import Console
from rich.markup import escape
from typer.core import TyperGroup

from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import fail, repo_context
from jev_agent.core.events import Event, EventType, summarize
from jev_agent.core.store import NotFoundError

console = Console()


class _DefaultTimelineGroup(TyperGroup):
    """`jev events RUN` reads as the timeline: a first argument that is not a subcommand
    name is routed to the `timeline` command (`jev events backfill RUN` still works)."""

    def resolve_command(  # type: ignore[override]
        self, ctx: Context, args: list[str]
    ) -> tuple[str | None, Command, list[str]]:
        if args and args[0] not in self.commands:
            args = ["timeline", *args]
        return super().resolve_command(ctx, args)  # type: ignore[arg-type, return-value]


events_app = typer.Typer(
    cls=_DefaultTimelineGroup,
    help="Inspect a run's event log: the timeline behind `jev replay` and `jev bisect`.",
    no_args_is_help=True,
)

RunIdArg = Annotated[str, typer.Argument(help="The run id, e.g. 20261015-093012-ab12.")]


def event_line(event: Event) -> str:
    ts = event.ts.strftime("%H:%M:%S")
    marker = " ~" if event.synthetic else ""
    parent = f" ←{event.parent_run_seq}" if event.parent_run_seq else ""
    return (
        f"[dim]{event.run_seq:>4} {ts}[/dim] "
        f"[cyan]{event.type.value}[/cyan]{marker}{parent}  "
        f"{escape(summarize(event))}  [dim]({event.actor})[/dim]"
    )


@events_app.command("timeline")
def timeline(
    run_id: RunIdArg,
    repo: RepoOption = Path("."),
    type_filter: Annotated[
        list[str] | None,
        typer.Option("--type", "-t", help="Only show these event types (repeatable)."),
    ] = None,
    after: Annotated[int, typer.Option("--after", help="Only events after this run_seq.")] = 0,
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Show at most this many events.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Raw JSON lines instead of the timeline.")
    ] = False,
) -> None:
    """Show RUN's event timeline: every state change the run made, in order."""
    types: set[EventType] | None = None
    if type_filter:
        try:
            types = {EventType(t) for t in type_filter}
        except ValueError as exc:
            raise fail(f"unknown event type: {exc}") from exc
    with repo_context(repo, create=False) as rctx:
        if not rctx.store.events_enabled:
            raise fail("event log disabled for this repo ([events] enabled = false)")
        try:
            events = rctx.store.list_events(run_id, event_types=types, after_seq=after, limit=limit)
        except NotFoundError as exc:
            raise fail(str(exc)) from exc
    if not events:
        console.print("[dim]no events recorded for this run yet[/dim]")
        return
    if as_json:
        for event in events:
            typer.echo(
                json.dumps(
                    {
                        "run_seq": event.run_seq,
                        "type": event.type.value,
                        "ts": event.ts.isoformat(),
                        "actor": event.actor,
                        "parent_run_seq": event.parent_run_seq,
                        "synthetic": event.synthetic,
                        "payload": event.payload,
                    }
                )
            )
        return
    for event in events:
        console.print(event_line(event))


@events_app.command("backfill")
def backfill(
    run_id: RunIdArg,
    repo: RepoOption = Path("."),
) -> None:
    """Synthesize events for a run that predates the event log.

    Reads the snapshot tables (runs, tasks, attempts, decisions, quality reports)
    and writes one synthetic event per recorded fact, ordered by timestamp. Synthetic
    events are marked `~` in the timeline and carry no causal links: snapshots record
    what is, not what happened. Runs once per run; refuses runs that already have
    events.
    """
    from jev_agent.core.backfill import BackfillError, backfill_run

    with repo_context(repo, create=False) as rctx:
        try:
            count = backfill_run(rctx.store, run_id)
        except NotFoundError as exc:
            raise fail(str(exc)) from exc
        except BackfillError as exc:
            raise fail(str(exc)) from exc
    console.print(f"[green]backfilled {count} synthetic events for run {escape(run_id)}[/green]")
