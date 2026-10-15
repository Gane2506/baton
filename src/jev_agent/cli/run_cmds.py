"""`jev run`, `jev approve`, `jev resume` and `jev cancel`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from jev_agent.app.context import NO_WORKERS_MESSAGE, AppContext
from jev_agent.app.errors import JevError
from jev_agent.app.format import format_answers
from jev_agent.app.runs import (
    RunOptions,
    approve_run,
    require_ship_enabled,
    resume_run,
)
from jev_agent.app.runs import start_run as start_run
from jev_agent.cli.repo import RepoOption
from jev_agent.cli.server_cmds import pick_profile_for_run
from jev_agent.cli.wiring import cli_context, fail
from jev_agent.config import STATE_DIRNAME
from jev_agent.config.schema import JevConfig, ShipMode
from jev_agent.core.budget import (
    approval_budget_block,
    estimate_verdict_for,
    format_estimate,
)
from jev_agent.core.learning import LearningError
from jev_agent.core.lease import RunInUseError
from jev_agent.core.models import RunState
from jev_agent.core.pipeline import PipelineError
from jev_agent.core.ship import ShipPreview, ship_preview
from jev_agent.core.store import NotFoundError, RunRecord, Store
from jev_agent.quality.levels import LEVELS, QualityLevel, parse_level
from jev_agent.servers.profiles import ProfileError, read_run_profile

console = Console()

RunId = Annotated[str, typer.Argument(help="Run id, as shown by `jev status`.")]
LearningOption = Annotated[
    str | None,
    typer.Option(
        "--learning",
        metavar="PROFILE",
        help="Use this learning profile for the run (overrides `jev learning` and jev.toml).",
    ),
]
NoLearningOption = Annotated[
    bool, typer.Option("--no-learning", help="Run without learning: no lessons in, none out.")
]
NoCacheOption = Annotated[
    bool,
    typer.Option(
        "--no-cache",
        help="Run without the decision/role cache: every dispatcher and role call goes live.",
    ),
]
QualityOption = Annotated[
    str | None,
    typer.Option(
        "--quality",
        metavar="LEVEL",
        help=f"Quality gate level for this run ({' | '.join(LEVELS)}); "
        "overrides the jev.toml \\[quality] level.",
    ),
]
AutoShipOption = Annotated[
    bool,
    typer.Option(
        "--auto-ship",
        help="After the plan passes both review votes, ship and merge without asking "
        "(overrides the jev.toml \\[ship] setting; see docs/decisions.md).",
    ),
]
AskBeforeShipOption = Annotated[
    bool,
    typer.Option(
        "--ask-before-ship",
        help="After the plan passes both review votes, wait for your ship decision "
        "(`jev ship approve|modify|stop`; overrides the jev.toml \\[ship] setting; "
        "see docs/decisions.md).",
    ),
]
ProfileOption = Annotated[
    str | None,
    typer.Option(
        "--profile",
        metavar="NAME",
        help="Workspace profile (servers, browser, learning) for the run; `none` for none.",
    ),
]


def quality_level(value: str | None) -> QualityLevel | None:
    """`--quality` as a level (None when not given); exits with an error for a bad value."""
    if value is None:
        return None
    try:
        return parse_level(value)
    except ValueError as exc:
        raise fail(str(exc)) from exc


def ship_mode_flag(config: JevConfig, auto_ship: bool, ask_before_ship: bool) -> ShipMode | None:
    """`--auto-ship` / `--ask-before-ship` as a ship mode (None: neither); exits with an error
    for both at once, or when shipping is off in the repo."""
    if auto_ship and ask_before_ship:
        raise fail("--auto-ship and --ask-before-ship cannot be used together")
    if not (auto_ship or ask_before_ship):
        return None
    flag = "--auto-ship" if auto_ship else "--ask-before-ship"
    try:
        return require_ship_enabled(config, "merge" if auto_ship else "ask", what=flag)
    except JevError as exc:
        raise fail(str(exc)) from exc


_STYLE = {
    RunState.DONE: "green",
    RunState.AWAITING_APPROVAL: "yellow",
    RunState.AWAITING_SHIP: "yellow",
    RunState.NEEDS_HUMAN: "yellow",
}


# Exit codes of `jev run`, `jev approve` and `jev resume` (the table in README "Quickstart"):
# 0 when the run is DONE, waits for approval or is left to continue; STOPPED_EXIT_CODE when it
# stopped short of DONE (`jev status RUN` says why); 1 for a command error, 2 for bad usage.
STOPPED_EXIT_CODE = 3
STOPPED_STATES = frozenset(
    {RunState.NEEDS_HUMAN, RunState.FAILED, RunState.BUDGET_EXCEEDED, RunState.CANCELLED}
)


def run_exit_code(state: RunState) -> int:
    """The exit code of a command that left a run in `state`."""
    return STOPPED_EXIT_CODE if state in STOPPED_STATES else 0


def exit_for(run: RunRecord) -> None:
    """End `jev run` / `approve` / `resume` with the run's exit code (see `run_exit_code`)."""
    code = run_exit_code(run.state)
    if code:
        raise typer.Exit(code)


def print_warnings(ctx: AppContext) -> None:
    for warning in ctx.warnings:
        console.print(f"[yellow]warning:[/yellow] {escape(warning)}")


def print_ship_preview(preview: ShipPreview) -> None:
    """What a person reviews before `jev ship approve | modify | stop`."""
    console.print(f"  PR title: {escape(preview.title)}")
    console.print(f"  branch: {preview.branch}")
    if preview.diff is not None:
        d = preview.diff
        console.print(f"  diff: {d.files} files, +{d.additions} / -{d.deletions}")
    if preview.vote is not None:
        vote = preview.vote.decision
        line = format_answers(vote.answers, vote.details)
        console.print(f"  D6 vote ({vote.dispatcher}): {escape(line)}")


def print_estimate(store: Store, config: JevConfig, run: RunRecord) -> None:
    """The pre-approval cost estimate: a range with its basis, plus any budget warning."""
    pair = estimate_verdict_for(store, config.budget, config.dispatcher.cost, run)
    if pair is None:
        return
    estimate, verdict = pair
    console.print(f"  estimated total cost: {format_estimate(estimate)}")
    if verdict.message is not None:
        console.print(f"  [yellow]budget: {escape(verdict.message)}[/yellow]")


def print_outcome(
    run: RunRecord, store: Store | None = None, config: JevConfig | None = None
) -> None:
    """The run's state and what to do next. With `store`, a run waiting to ship also shows
    the PR it would open, Jev's and Laya's D6 votes and the diff summary."""
    style = _STYLE.get(run.state, "red")
    console.print(f"run [bold]{run.id}[/bold]: [{style}]{run.state}[/{style}]")
    if run.reason:
        console.print(f"  {escape(run.reason)}")
    if run.state is RunState.AWAITING_APPROVAL:
        console.print(f"  Review the plan with `jev status {run.id}`, then `jev approve {run.id}`.")
    if run.state is RunState.AWAITING_SHIP and store is not None:
        print_ship_preview(ship_preview(run.idea.repo_path, store, run.id))
    if run.learning_outcome:
        console.print(f"  learning: {escape(run.learning_outcome)}")
    if run.quality_level != "none":
        console.print(f"  quality: {run.quality_level}; report: `jev quality show {run.id}`")
    try:
        workspace = read_run_profile(run.idea.repo_path / STATE_DIRNAME, run.id)
    except ProfileError:
        workspace = None
    if workspace is not None:
        console.print(f"  workspace profile: {escape(workspace.name)}")
    console.print(f"  spent ${run.spend_usd:.2f}; timeline: `jev log {run.id}`")
    if store is not None and config is not None:
        print_estimate(store, config, run)


def run(
    idea: Annotated[str, typer.Argument(help="What to build, in plain words.")],
    plan_only: Annotated[
        bool, typer.Option("--plan-only", help="Stop after the plan is validated.")
    ] = False,
    learning: LearningOption = None,
    no_learning: NoLearningOption = False,
    quality: QualityOption = None,
    profile: ProfileOption = None,
    auto_ship: AutoShipOption = False,
    ask_before_ship: AskBeforeShipOption = False,
    repo: RepoOption = Path("."),
    no_cache: NoCacheOption = False,
) -> None:
    """Take an idea through planning, validation, approval, execution and integration.

    Exit codes: 0 done or awaiting approval, 3 stopped short of DONE, 1 error."""
    try:
        options = RunOptions(
            plan_only=plan_only,
            learning=learning,
            no_learning=no_learning,
            quality=quality,
            profile=profile,
        )
    except JevError as exc:
        raise fail(str(exc)) from exc
    with cli_context(repo) as ctx:
        if no_cache:
            ctx.config.cache.enabled = False
        mode = ship_mode_flag(ctx.config, auto_ship, ask_before_ship)
        if not ctx.has_workers:
            raise fail(NO_WORKERS_MESSAGE)
        print_warnings(ctx)
        profile = pick_profile_for_run(ctx.config, ctx.root, options.profile)
        try:
            record = asyncio.run(
                start_run(
                    ctx,
                    idea,
                    plan_only=options.plan_only,
                    learning=options.learning,
                    no_learning=options.no_learning,
                    quality=options.quality,
                    profile=profile,
                    ship_mode=mode,
                )
            )
        except (LearningError, ProfileError, RunInUseError) as exc:
            raise fail(str(exc)) from exc
        print_outcome(record, ctx.store, ctx.config)
    exit_for(record)


def approve(
    run_id: RunId,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Approve even if the cost estimate exceeds the run budget.",
        ),
    ] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Approve a validated plan and execute it — or approve a run waiting to ship
    (`jev ship approve`), which ships and merges it.

    Exit codes: 0 done or awaiting approval, 3 stopped short of DONE, 1 error."""
    with cli_context(repo) as ctx:
        if ctx.has_workers:
            print_warnings(ctx)
        try:
            pending = ctx.store.get_run(run_id)
        except NotFoundError as exc:
            raise fail(str(exc)) from exc
        # The estimate is advisory about future spend: when the cap is already hit,
        # the hard budget check owns the outcome (BUDGET_EXCEEDED), not the estimate.
        _, verdict, block = approval_budget_block(
            ctx.store, ctx.config.budget, ctx.config.dispatcher.cost, pending, force=force
        )
        if block is not None:
            raise fail(f"{block} (pass --force to approve anyway)")
        if verdict is not None and verdict.level == "warn" and verdict.message:
            console.print(f"[yellow]warning: {escape(verdict.message)}[/yellow]")
        try:
            record = asyncio.run(approve_run(ctx, run_id))
        except (NotFoundError, PipelineError, RunInUseError) as exc:
            raise fail(str(exc)) from exc
        if not ctx.has_workers:
            console.print(f"Approved. {escape(NO_WORKERS_MESSAGE)} Then run `jev resume {run_id}`.")
        print_outcome(record, ctx.store, ctx.config)
    exit_for(record)


def resume(
    run_id: RunId,
    learning: LearningOption = None,
    no_learning: NoLearningOption = False,
    quality: QualityOption = None,
    profile: ProfileOption = None,
    repo: RepoOption = Path("."),
    no_cache: NoCacheOption = False,
) -> None:
    """Continue a run from its last saved state (with the learning profile and quality level
    it started with, unless --learning, --no-learning or --quality says otherwise).

    Exit codes: 0 done or awaiting approval, 3 stopped short of DONE, 1 error."""
    try:
        options = RunOptions(
            learning=learning,
            no_learning=no_learning,
            quality=quality,
            profile=profile,
        )
    except JevError as exc:
        raise fail(str(exc)) from exc
    with cli_context(repo) as ctx:
        if no_cache:
            ctx.config.cache.enabled = False
        if not ctx.has_workers:
            raise fail(NO_WORKERS_MESSAGE)
        print_warnings(ctx)
        try:
            record = asyncio.run(
                resume_run(
                    ctx,
                    run_id,
                    learning=options.learning,
                    no_learning=options.no_learning,
                    quality=parse_level(options.quality) if options.quality is not None else None,
                    profile=options.profile,
                )
            )
        except (NotFoundError, PipelineError, LearningError, ProfileError, RunInUseError) as exc:
            raise fail(str(exc)) from exc
        print_outcome(record, ctx.store, ctx.config)
    exit_for(record)


def cancel(run_id: RunId, repo: RepoOption = Path(".")) -> None:
    """Cancel a run. Its branches and worktrees are kept for inspection."""
    with cli_context(repo) as ctx:
        try:
            record = ctx.pipeline.cancel(run_id)
        except (NotFoundError, PipelineError) as exc:
            raise fail(str(exc)) from exc
        print_outcome(record, ctx.store)
