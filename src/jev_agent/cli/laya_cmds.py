"""`jev laya ...`: tune Laya's matrix weights from run outcomes (issue #69, docs/learning.md).

`tune` only shows a suggestion; `--apply` saves it on a learning profile, and runs that learn
with that profile use it. Weights set in jev.toml (`[dispatcher.laya] weights`) always win, so
`--apply` refuses while they are set.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import RepoContext, fail, repo_context
from jev_agent.core.learning import LearningError, learning_status
from jev_agent.core.tuning import MIN_OUTCOMES, collect_samples, suggest

console = Console()

laya_app = typer.Typer(
    name="laya",
    help="Laya's decision matrix: tune its weights from how past decisions turned out.",
    no_args_is_help=True,
)

ProfileOption = Annotated[
    str | None,
    typer.Option("--profile", "-p", help="Learning profile (default: the active profile)."),
]


@contextmanager
def _profile(repo: Path, name: str | None, *, create: bool) -> Iterator[tuple[RepoContext, str]]:
    """The repo context and an existing profile's name (`name`, else the active profile)."""
    with repo_context(repo, create=create) as ctx:
        try:
            profile = name or learning_status(ctx.store, ctx.config).profile
            ctx.learning.db_for(profile)  # raises when there is no such profile
            yield ctx, profile
        except LearningError as exc:
            raise fail(str(exc)) from exc


def _set_in_config(ctx: RepoContext) -> bool:
    return "weights" in ctx.config.dispatcher.laya.model_fields_set


@laya_app.command("tune")
def tune(
    profile: ProfileOption = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Save the suggested weights on the profile.")
    ] = False,
    min_outcomes: Annotated[
        int,
        typer.Option(
            "--min-outcomes", min=1, help="Passed and failed outcomes needed for a suggestion."
        ),
    ] = MIN_OUTCOMES,
    repo: RepoOption = Path("."),
) -> None:
    """Suggest Laya weights from how the work chosen by past consensus decisions turned out."""
    with _profile(repo, profile, create=apply) as (ctx, name):
        configured = _set_in_config(ctx)
        tuned = None if configured else ctx.learning.laya_weights(name)
        current = ctx.config.dispatcher.laya.weights if tuned is None else tuned
        source = "jev.toml" if configured else "defaults" if tuned is None else f"profile {name}"
        result = suggest(collect_samples(ctx.store), current, min_outcomes)
        console.print(
            f"current weights: {escape(source)}; outcomes: {result.passed} passed, "
            f"{result.failed} failed"
        )
        if result.weights is None:
            console.print(f"No suggestion: {result.reason}.")
            if apply:
                raise fail("nothing to apply")
            return
        table = Table(title="Laya weights")
        for column in ("criterion", "passed avg", "failed avg", "current", "suggested"):
            table.add_column(column, overflow="fold")
        for e in result.evidence:
            table.add_row(
                e.criterion,
                f"{e.passed_mean:.2f}",
                f"{e.failed_mean:.2f}",
                f"{e.current:.3f}",
                f"{e.suggested:.3f}",
            )
        console.print(table)
        if not apply:
            console.print("Not applied. `jev laya tune --apply` saves them on the profile.")
            return
        if configured:
            raise fail(
                "jev.toml sets [dispatcher.laya] weights, which win over a profile's; "
                "copy the suggestion there instead"
            )
        try:
            ctx.learning.set_laya_weights(name, result.weights)
        except LearningError as exc:
            raise fail(str(exc)) from exc
        console.print(
            f"Saved on profile {escape(name)}: runs that learn with it use these weights. "
            "`jev laya reset` goes back to the configured ones."
        )


@laya_app.command("reset")
def reset(profile: ProfileOption = None, repo: RepoOption = Path(".")) -> None:
    """Remove the tuned weights from a profile, so its runs use the configured ones."""
    with _profile(repo, profile, create=True) as (ctx, name):
        # Cleared without reading them first, so weights that no longer validate can be reset.
        ctx.learning.set_laya_weights(name, None)
        console.print(
            f"Profile {escape(name)} has no tuned weights: its runs use the configured ones."
        )
