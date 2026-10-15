"""`jev replay` (verify mode) and `jev bisect` (docs/events.md).

`jev replay` is verify-only by design: it re-derives the deterministic decision
trace from the event log without any LLM calls. Cassette re-execution (Mode B in
the design doc) is deliberately not implemented — it is brittle by nature (any
prompt change invalidates the cassette) and is documented as future work.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from jev_agent.cli.events_cmds import RunIdArg, event_line
from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import fail, repo_context
from jev_agent.core.bisect import bisect_run, render_timeline_html
from jev_agent.core.replay import ReplayedState, verify_run
from jev_agent.core.store import NotFoundError, Store

console = Console()


def replay(
    run_id: RunIdArg,
    repo: RepoOption = Path("."),
    to: Annotated[
        int | None,
        typer.Option(
            "--to",
            help="Verify only up to this run_seq, then print the reconstructed state.",
        ),
    ] = None,
    step: Annotated[
        bool, typer.Option("--step", help="Interactive stepper: walk the log event by event.")
    ] = False,
    diff: Annotated[
        bool, typer.Option("--diff", help="Show expected-vs-recorded for every divergence.")
    ] = False,
) -> None:
    """Verify RUN's event log: re-derive the decision trace without any LLM calls.

    Checks envelope integrity (gapless run_seq), state-transition consistency,
    decision request/answer contracts, and that the log fully explains the
    snapshot tables. Exit 0 when everything matches, exit 2 on divergence.
    """
    with repo_context(repo, create=False) as rctx:
        if not rctx.store.events_enabled:
            raise fail("event log disabled for this repo ([events] enabled = false)")
        try:
            verification = verify_run(rctx.store, run_id, to_seq=to)
        except NotFoundError as exc:
            raise fail(str(exc)) from exc
        if step:
            _stepper(rctx.store, run_id, to)
            return
    if to is not None:
        _print_state(verification.state, to)
    if verification.ok:
        console.print(
            f"[green]verified {verification.events_checked} events: no divergences[/green]"
        )
        return
    console.print(
        f"[red]{len(verification.divergences)} divergence(s) in"
        f" {verification.events_checked} events:[/red]"
    )
    for div in verification.divergences:
        where = f"[{div.run_seq}]" if div.run_seq is not None else "[snapshot]"
        console.print(f"  {where} [yellow]{div.kind}[/yellow]: {escape(div.message)}")
        if diff and (div.expected or div.recorded):
            console.print(f"    expected: {escape(div.expected)}")
            console.print(f"    recorded: {escape(div.recorded)}")
    raise typer.Exit(2)


def _print_state(state: ReplayedState, to: int) -> None:
    console.print(f"[bold]reconstructed state at run_seq {to}:[/bold]")
    console.print(f"  run: {state.run_state}")
    for task_id, task_state in sorted(state.task_states.items()):
        console.print(f"  {task_id}: {task_state}")
    console.print(f"  spend: ${state.spend_usd:.2f}  decisions: {state.decisions}")


def _stepper(store: Store, run_id: str, to: int | None) -> None:
    """Interactive event-by-event debugger for a run's log."""
    events = store.list_events(run_id)
    if to is not None:
        events = [e for e in events if e.run_seq <= to]
    if not events:
        console.print("[dim]no events recorded for this run yet[/dim]")
        return
    pos = 0
    console.print("[dim]step | next <type> | until <seq> | state | verify | quit[/dim]")
    while True:
        console.print(event_line(events[pos]))
        try:
            raw = input("> ").strip()
        except EOFError:
            console.print()
            return
        if not raw:
            continue
        parts = raw.split(None, 1)
        cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
        if cmd in ("quit", "q", "exit"):
            return
        if cmd == "step":
            pos = min(pos + 1, len(events) - 1)
        elif cmd == "next":
            nxt = next(
                (i for i in range(pos + 1, len(events)) if events[i].type.value == arg),
                None,
            )
            if nxt is None:
                console.print(f"[yellow]no later event of type {escape(arg)}[/yellow]")
            else:
                pos = nxt
        elif cmd == "until":
            try:
                target = int(arg)
            except ValueError:
                console.print("[yellow]usage: until <seq>[/yellow]")
                continue
            nxt = next(
                (i for i in range(len(events)) if events[i].run_seq > target),
                len(events),
            )
            pos = min(max(nxt - 1, 0), len(events) - 1)
        elif cmd == "state":
            _print_state(verify_run(store, run_id, to_seq=events[pos].run_seq).state,
                         events[pos].run_seq)
        elif cmd == "verify":
            prefix = verify_run(store, run_id, to_seq=events[pos].run_seq)
            console.print(
                f"[dim]{len(prefix.divergences)} divergence(s) in the"
                f" {prefix.events_checked}-event prefix[/dim]"
            )
            for div in prefix.divergences:
                console.print(f"  [{div.run_seq}] {div.kind}: {escape(div.message)}")
        else:
            console.print("[yellow]unknown command[/yellow]")


def bisect(
    run_id: RunIdArg,
    repo: RepoOption = Path("."),
    gate: Annotated[
        bool, typer.Option("--gate", help='"Bad" = the first blocked quality gate.')
    ] = False,
    test: Annotated[
        bool, typer.Option("--test", help='"Bad" = the first failed tests (or failed attempt).')
    ] = False,
    task: Annotated[
        str | None,
        typer.Option(
            "--task",
            help=(
                '"Bad" = the first task.state_changed -> FAILED for TASK;'
                " use --task '' for any task."
            ),
        ),
    ] = None,
    mark: Annotated[
        int | None,
        typer.Option("--mark", help='"Bad" = the human-marked event at this run_seq.'),
    ] = None,
    good: Annotated[
        int | None,
        typer.Option("--good", help="Only scan events after this run_seq (known-good)."),
    ] = None,
    export: Annotated[
        Path | None,
        typer.Option("--export", help="Write a self-contained HTML timeline to this path."),
    ] = None,
) -> None:
    """Find where a run went bad: first-bad-event scan + causal-chain walk.

    "Bad" is the first event matching --gate, --test, --task or --mark (first
    match wins when combined). This is a scan, not a binary search: the predicate
    usually cannot be re-run (it needed the original LLM outputs). Prints the bad
    event and the blame path of parent events that led to it.
    """
    if not (gate or test or task is not None or mark is not None):
        raise fail("say what 'bad' means: --gate, --test, --task or --mark")
    with repo_context(repo, create=False) as rctx:
        if not rctx.store.events_enabled:
            raise fail("event log disabled for this repo ([events] enabled = false)")
        try:
            result = bisect_run(
                rctx.store,
                run_id,
                gate=gate,
                test=test,
                task=task if task else None,
                task_any=task == "",
                mark=mark,
                good_seq=good or 0,
            )
        except NotFoundError as exc:
            raise fail(str(exc)) from exc
        if export is not None:
            html_text = render_timeline_html(
                run_id, rctx.store.list_events(run_id), datetime.now().astimezone()
            )
            export.write_text(html_text)
            console.print(f"[green]timeline written to {escape(str(export))}[/green]")
    if result is None:
        console.print("[green]no bad event found: the run looks fine by the given criteria[/green]")
        return
    bad = result.bad
    console.print(f"[bold]first bad ({result.criterion}):[/bold]")
    console.print(f"  {event_line(bad)}")
    console.print("[bold]blame path:[/bold]")
    for event in result.chain:
        marker = "  [red]← the bad event[/red]" if event.run_seq == bad.run_seq else ""
        console.print(f"  {event_line(event)}{marker}")
