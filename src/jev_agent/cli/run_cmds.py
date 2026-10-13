"""`jev run`, `jev approve`, `jev resume` and `jev cancel`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from jev_agent.cli.repo import RepoOption
from jev_agent.cli.server_cmds import pick_profile_for_run
from jev_agent.cli.wiring import NO_WORKERS_MESSAGE, CliContext, cli_context, fail
from jev_agent.config import STATE_DIRNAME
from jev_agent.config.servers_schema import NO_PROFILE
from jev_agent.core.learning import Learning, LearningError, check_profile_name, resolve_learning
from jev_agent.core.lease import RunInUseError
from jev_agent.core.models import Idea, RunState
from jev_agent.core.pipeline import PipelineError
from jev_agent.core.store import NotFoundError, RunRecord
from jev_agent.quality.levels import LEVELS, QualityLevel, parse_level
from jev_agent.servers.profiles import (
    ProfileError,
    effective_name,
    get_profile,
    read_run_profile,
    snapshot,
    write_run_profile,
)

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
QualityOption = Annotated[
    str | None,
    typer.Option(
        "--quality",
        metavar="LEVEL",
        help=f"Quality gate level for the run ({' | '.join(LEVELS)}); overrides [quality] level.",
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


_STYLE = {
    RunState.DONE: "green",
    RunState.AWAITING_APPROVAL: "yellow",
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


def print_warnings(ctx: CliContext) -> None:
    for warning in ctx.warnings:
        console.print(f"[yellow]warning:[/yellow] {escape(warning)}")


def print_outcome(run: RunRecord) -> None:
    style = _STYLE.get(run.state, "red")
    console.print(f"run [bold]{run.id}[/bold]: [{style}]{run.state}[/{style}]")
    if run.reason:
        console.print(f"  {escape(run.reason)}")
    if run.state is RunState.AWAITING_APPROVAL:
        console.print(f"  Review the plan with `jev status {run.id}`, then `jev approve {run.id}`.")
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


def _learning(ctx: CliContext) -> Learning:
    assert ctx.learning is not None
    return ctx.learning


async def start_run(
    ctx: CliContext,
    idea: str,
    *,
    plan_only: bool = False,
    learning: str | None = None,
    no_learning: bool = False,
    quality: str | None = None,
    profile: str | None = None,
) -> RunRecord:
    """Start a run for `idea` and advance it as far as it goes (the body of `jev run`).

    `learning` / `no_learning` are the run flags; `resolve_learning` applies the precedence
    (flag > `jev learning` setting > jev.toml > default). Raises `LearningError` for an
    unknown profile or both flags at once. `quality` is the quality gate level (None: the
    config's `[quality] level`); ValueError for an unknown level. Reusable by other front
    ends (the chat, the scheduler).

    `profile` is the workspace profile (`none`: none); without it the repo default applies
    (`jev profile use`). The profile's `learning` and `quality` apply when no flag says
    otherwise. ProfileError (a ValueError) for an unknown profile.
    """
    workspace = effective_name(ctx.config, profile, ctx.root / STATE_DIRNAME)
    if workspace is not None:
        settings = get_profile(ctx.config, workspace)
        if learning is None and not no_learning and settings.learning is not None:
            if settings.learning is False:
                no_learning = True
            else:
                learning = settings.learning
        if quality is None and settings.quality is not None:
            quality = settings.quality
    level = None if quality is None else parse_level(quality)
    learning_profile = resolve_learning(
        ctx.store, _learning(ctx), ctx.config, learning, no_learning
    )
    return await ctx.pipeline.start(
        Idea(text=idea, repo_path=ctx.root),
        plan_only=plan_only,
        learning_profile=learning_profile,
        quality=level,
        profile=workspace,
    )


def set_run_profile(ctx: CliContext, run_id: str, profile: str | None) -> None:
    """`jev resume --profile NAME|none`: replace the run's workspace profile."""
    if profile is None:
        return
    ctx.store.get_run(run_id)  # NotFoundError for an unknown run
    copy = None if profile == NO_PROFILE else snapshot(ctx.config, profile)
    write_run_profile(ctx.root / STATE_DIRNAME, run_id, copy)


def set_run_learning(ctx: CliContext, run_id: str, learning: str | None, no_learning: bool) -> None:
    """Apply `--learning` / `--no-learning` to an existing run (from `jev resume`). Without
    either flag the run keeps the profile it started with."""
    if learning is not None and no_learning:
        raise LearningError("--learning and --no-learning cannot be used together")
    ctx.store.get_run(run_id)  # NotFoundError for an unknown run
    if no_learning:
        ctx.store.set_run_learning(run_id, None)
    elif learning is not None:
        if _learning(ctx).get(check_profile_name(learning)) is None:
            raise LearningError(
                f"no learning profile named {learning!r}; "
                f"create it with `jev learning create {learning}`"
            )
        ctx.store.set_run_learning(run_id, learning)


async def approve_run(ctx: CliContext, run_id: str) -> RunRecord:
    """Approve the run's plan and execute it when workers are available (the body of
    `jev approve`; the dashboard runs it in a background thread)."""
    return await ctx.pipeline.approve(run_id, execute=ctx.has_workers)


async def resume_run(
    ctx: CliContext,
    run_id: str,
    *,
    learning: str | None = None,
    no_learning: bool = False,
    quality: QualityLevel | None = None,
    profile: str | None = None,
) -> RunRecord:
    """Apply the resume flags, then continue the run (the body of `jev resume`)."""
    set_run_learning(ctx, run_id, learning, no_learning)
    set_run_profile(ctx, run_id, profile)
    if quality is not None:
        ctx.store.set_run_quality(run_id, quality)
    return await ctx.pipeline.resume(run_id)


def run(
    idea: Annotated[str, typer.Argument(help="What to build, in plain words.")],
    plan_only: Annotated[
        bool, typer.Option("--plan-only", help="Stop after the plan is validated.")
    ] = False,
    learning: LearningOption = None,
    no_learning: NoLearningOption = False,
    quality: QualityOption = None,
    profile: ProfileOption = None,
    repo: RepoOption = Path("."),
) -> None:
    """Take an idea through planning, validation, approval, execution and integration.

    Exit codes: 0 done or awaiting approval, 3 stopped short of DONE, 1 error."""
    level = quality_level(quality)
    with cli_context(repo) as ctx:
        if learning is not None and no_learning:
            raise fail("--learning and --no-learning cannot be used together")
        if not ctx.has_workers:
            raise fail(NO_WORKERS_MESSAGE)
        print_warnings(ctx)
        profile = pick_profile_for_run(ctx.config, ctx.root, profile)
        try:
            record = asyncio.run(
                start_run(
                    ctx,
                    idea,
                    plan_only=plan_only,
                    learning=learning,
                    no_learning=no_learning,
                    quality=level,
                    profile=profile,
                )
            )
        except (LearningError, ProfileError, RunInUseError) as exc:
            raise fail(str(exc)) from exc
        print_outcome(record)
    exit_for(record)


def approve(run_id: RunId, repo: RepoOption = Path(".")) -> None:
    """Approve a validated plan and execute it.

    Exit codes: 0 done or awaiting approval, 3 stopped short of DONE, 1 error."""
    with cli_context(repo) as ctx:
        if ctx.has_workers:
            print_warnings(ctx)
        try:
            record = asyncio.run(approve_run(ctx, run_id))
        except (NotFoundError, PipelineError, RunInUseError) as exc:
            raise fail(str(exc)) from exc
        if not ctx.has_workers:
            console.print(f"Approved. {escape(NO_WORKERS_MESSAGE)} Then run `jev resume {run_id}`.")
        print_outcome(record)
    exit_for(record)


def resume(
    run_id: RunId,
    learning: LearningOption = None,
    no_learning: NoLearningOption = False,
    quality: QualityOption = None,
    profile: ProfileOption = None,
    repo: RepoOption = Path("."),
) -> None:
    """Continue a run from its last saved state (with the learning profile and quality level
    it started with, unless --learning, --no-learning or --quality says otherwise).

    Exit codes: 0 done or awaiting approval, 3 stopped short of DONE, 1 error."""
    level = quality_level(quality)
    with cli_context(repo) as ctx:
        if learning is not None and no_learning:
            raise fail("--learning and --no-learning cannot be used together")
        if not ctx.has_workers:
            raise fail(NO_WORKERS_MESSAGE)
        print_warnings(ctx)
        try:
            record = asyncio.run(
                resume_run(
                    ctx,
                    run_id,
                    learning=learning,
                    no_learning=no_learning,
                    quality=level,
                    profile=profile,
                )
            )
        except (NotFoundError, PipelineError, LearningError, ProfileError, RunInUseError) as exc:
            raise fail(str(exc)) from exc
        print_outcome(record)
    exit_for(record)


def cancel(run_id: RunId, repo: RepoOption = Path(".")) -> None:
    """Cancel a run. Its branches and worktrees are kept for inspection."""
    with cli_context(repo) as ctx:
        try:
            record = ctx.pipeline.cancel(run_id)
        except (NotFoundError, PipelineError) as exc:
            raise fail(str(exc)) from exc
        print_outcome(record)
