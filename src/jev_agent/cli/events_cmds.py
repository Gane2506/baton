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
from jev_agent.core.events import Event, EventType
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


def summarize(event: Event) -> str:
    """One human-readable line for an event (payload fields only, never secrets)."""
    p = event.payload
    match event.type:
        case EventType.RUN_CREATED:
            idea = str(p.get("idea_text", ""))[:70]
            return f"run created: {idea}"
        case EventType.RUN_STATE_CHANGED:
            line = f"{p.get('from_state')} → {p.get('to_state')}"
            if p.get("reason"):
                line += f" ({p['reason']})"
            return line
        case EventType.RUN_RESUMED:
            return f"resumed from {p.get('from_state')} via {p.get('via')}"
        case EventType.PLAN_SET:
            tasks = p.get("plan", {}).get("tasks", [])
            return f"plan set: round {p.get('round')}, {len(tasks)} tasks"
        case EventType.DESIGN_SET:
            return "design doc set"
        case EventType.VALIDATION_RECORDED:
            issues = p.get("issues", [])
            return f"validation: {p.get('verdict')} ({len(issues)} issues)"
        case EventType.TASKS_UPSERTED:
            return f"tasks upserted: {', '.join(p.get('task_ids', []))}"
        case EventType.TASK_STATE_CHANGED:
            line = f"{p.get('task_id')}: {p.get('from_state')} → {p.get('to_state')}"
            if p.get("note"):
                line += f" — {str(p['note'])[:80]}"
            return line
        case EventType.TASK_ASSIGNED:
            return (
                f"{p.get('task_id')} assigned to {p.get('worker')}"
                f" ({p.get('model') or 'default model'}, {p.get('effort') or 'default effort'})"
            )
        case EventType.ATTEMPT_STARTED:
            return (
                f"attempt {p.get('attempt_no')} started: {p.get('role')}"
                f" on {p.get('worker') or '?'}"
            )
        case EventType.ATTEMPT_TESTED:
            return f"attempt {p.get('attempt_id')} tests: {p.get('tests')}"
        case EventType.ATTEMPT_FINISHED:
            line = f"attempt {p.get('attempt_id')} finished: {p.get('status')}"
            if p.get("cost_usd") is not None:
                line += f" (${p['cost_usd']:.2f})"
            summary = str(p.get("summary", ""))[:80]
            if summary:
                line += f" — {summary}"
            return line
        case EventType.DECISION_REQUESTED:
            questions = p.get("questions", {})
            asked = ", ".join(sorted(questions))
            return f"{p.get('point')} requested: {asked}"
        case EventType.DECISION_RECORDED:
            answers = p.get("answers", {})
            shown = ", ".join(f"{k}={v}" for k, v in answers.items())
            return f"{p.get('point')} → {shown} ({p.get('dispatcher')})"
        case EventType.GATE_CHECKED:
            verdict = "BLOCKED" if p.get("blocked") else "passed"
            scope = p.get("scope", "")
            task = f" {p['task_id']}" if p.get("task_id") else ""
            return f"gate {scope}{task}: {verdict}"
        case EventType.SHIP_RECORDED:
            return f"PR #{p.get('number')}: {p.get('state')} (ci: {p.get('ci_status') or '?'})"
        case EventType.SPEND_ADDED:
            task = f" on {p['task_id']}" if p.get("task_id") else ""
            return f"spend +${p.get('usd', 0):.2f}{task} (total ${p.get('running_total', 0):.2f})"
        case EventType.LEASE_CLAIMED:
            return f"lease claimed by {p.get('host')}:{p.get('pid')} ({p.get('reason')})"
        case EventType.LEASE_RELEASED:
            return "lease released"
        case EventType.LEASE_TAKEN_OVER:
            return f"lease taken over from {p.get('old_host')}:{p.get('old_pid')}"
        case EventType.HUMAN_APPROVED:
            return f"human approved {p.get('gate')}"
        case EventType.HUMAN_NOTE:
            return f"human note: {str(p.get('notes', ''))[:80]}"
    return event.type.value  # pragma: no cover - every type is matched above


def _event_line(event: Event) -> str:
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
            events = rctx.store.list_events(
                run_id, event_types=types, after_seq=after, limit=limit
            )
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
        console.print(_event_line(event))
