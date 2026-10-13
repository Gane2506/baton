"""`jev learning ...`: manage learning profiles and their lessons (docs/learning.md).

Settings made here (on/off for the repo, the active profile) are stored in `.jev/state.db`, so
they need no edit to jev.toml and win over it. `--learning` / `--no-learning` on `jev run` win
over both.
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
from jev_agent.cli.wiring import CliContext, cli_context, fail
from jev_agent.core.learning import (
    SETTING_PROFILE,
    Learning,
    LearningError,
    check_profile_name,
    learning_status,
    set_active_profile,
    set_repo_learning,
)

console = Console()

learning_app = typer.Typer(
    name="learning",
    help="Lessons from past runs that feed future runs: profiles, on/off, flush.",
    no_args_is_help=True,
)

ProfileArg = Annotated[str, typer.Argument(help="Profile name.")]
OptionalProfileArg = Annotated[
    str | None, typer.Argument(help="Profile name (default: the active profile).")
]
YesOption = Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")]


@contextmanager
def _context(repo: Path) -> Iterator[tuple[CliContext, Learning]]:
    with cli_context(repo) as ctx:
        assert ctx.learning is not None
        try:
            yield ctx, ctx.learning
        except LearningError as exc:
            raise fail(str(exc)) from exc


def _active(ctx: CliContext) -> str:
    return learning_status(ctx.store, ctx.config).profile


@learning_app.command("list")
def list_profiles(repo: RepoOption = Path(".")) -> None:
    """List profiles, with scope, on/off, lesson count and the active one."""
    with _context(repo) as (ctx, learning):
        status = learning_status(ctx.store, ctx.config)
        state = "[green]on[/green]" if status.enabled else "off"
        console.print(
            f"learning is {state} for this repo ({status.enabled_source}); "
            f"active profile: [bold]{escape(status.profile)}[/bold] ({status.profile_source})"
        )
        profiles = learning.profiles()
        if not profiles:
            console.print("No profiles yet. `jev learning enable` creates the active one.")
            return
        table = Table(title="learning profiles")
        for column in ("profile", "scope", "enabled", "lessons", "active"):
            table.add_column(column, overflow="fold")
        for p in profiles:
            table.add_row(
                escape(p.name),
                p.scope,
                "yes" if p.enabled else "no",
                str(p.lessons),
                "*" if p.name == status.profile else "",
            )
        console.print(table)


@learning_app.command("show")
def show(
    profile: OptionalProfileArg = None,
    include_retired: Annotated[
        bool, typer.Option("--all", help="Include retired lessons.")
    ] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Show a profile's lessons."""
    with _context(repo) as (ctx, learning):
        name = profile or _active(ctx)
        lessons = learning.lessons(name, include_retired)
        if not lessons:
            console.print(f"Profile {escape(name)} has no lessons.")
            return
        table = Table(title=f"lessons in {escape(name)}")
        columns = ["id", "roles", "tags", "score", "uses", "lesson"]
        if include_retired:
            columns.insert(1, "retired")
        for column in columns:
            table.add_column(column, overflow="fold")
        for lesson in lessons:
            row = [
                lesson.id,
                ",".join(lesson.roles),
                escape(", ".join(lesson.tags)),
                f"{lesson.score:g}",
                str(lesson.uses),
                escape(lesson.text),
            ]
            if include_retired:
                row.insert(1, "yes" if lesson.retired else "")
            table.add_row(*row)
        console.print(table)


@learning_app.command("create")
def create(
    name: ProfileArg,
    shared: Annotated[
        bool,
        typer.Option("--shared", help="Store it in the shared database, usable from any repo."),
    ] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Create a profile (repo scope unless --shared)."""
    with _context(repo) as (_, learning):
        profile = learning.create(name, shared=shared)
        where = learning.shared_path if shared else "this repo's .jev/state.db"
        console.print(f"Created profile {escape(profile.name)} ({profile.scope}: {where}).")


@learning_app.command("use")
def use(name: ProfileArg, repo: RepoOption = Path(".")) -> None:
    """Make a profile the active one for this repo."""
    with _context(repo) as (ctx, learning):
        if learning.get(check_profile_name(name)) is None:
            raise LearningError(f"no learning profile named {name!r}; create it first")
        set_active_profile(ctx.store, name)
        status = learning_status(ctx.store, ctx.config)
        note = "" if status.enabled else " Learning is off; `jev learning enable` turns it on."
        console.print(f"Active profile: {escape(name)}.{note}")


@learning_app.command("enable")
def enable(name: OptionalProfileArg = None, repo: RepoOption = Path(".")) -> None:
    """Turn learning on for this repo (no NAME), or re-enable one profile."""
    with _context(repo) as (ctx, learning):
        if name is None:
            active = _active(ctx)
            learning.ensure(active)
            learning.set_enabled(active, True)
            set_repo_learning(ctx.store, True)
            console.print(
                f"Learning is on for this repo, with profile {escape(active)}. Each run now "
                "ends with one learner call when it has something to learn."
            )
        else:
            learning.set_enabled(name, True)
            console.print(f"Profile {escape(name)} is enabled.")


@learning_app.command("disable")
def disable(name: OptionalProfileArg = None, repo: RepoOption = Path(".")) -> None:
    """Turn learning off for this repo (no NAME), or disable one profile."""
    with _context(repo) as (ctx, learning):
        if name is None:
            set_repo_learning(ctx.store, False)
            console.print("Learning is off for this repo. Lessons are kept.")
        else:
            learning.set_enabled(name, False)
            console.print(
                f"Profile {escape(name)} is disabled: runs only use it with --learning {name}."
            )


@learning_app.command("flush")
def flush(
    name: OptionalProfileArg = None, yes: YesOption = False, repo: RepoOption = Path(".")
) -> None:
    """Delete every lesson of a profile (the profile stays)."""
    with _context(repo) as (ctx, learning):
        target = name or _active(ctx)
        learning.db_for(target)  # unknown profile: error before asking
        if not yes and not typer.confirm(f"Delete all lessons of profile {target}?"):
            raise typer.Exit(1)
        count = learning.flush(target)
        console.print(f"Deleted {count} lesson(s) from {escape(target)}.")


@learning_app.command("delete")
def delete(name: ProfileArg, yes: YesOption = False, repo: RepoOption = Path(".")) -> None:
    """Delete a profile and all its lessons."""
    with _context(repo) as (ctx, learning):
        learning.db_for(name)
        if not yes and not typer.confirm(f"Delete profile {name} and all its lessons?"):
            raise typer.Exit(1)
        learning.delete(name)
        if ctx.store.get_setting(SETTING_PROFILE) == name:
            set_active_profile(ctx.store, None)  # back to jev.toml / the default
        console.print(f"Deleted profile {escape(name)}.")


@learning_app.command("forget")
def forget(
    lesson_id: Annotated[str, typer.Argument(help="Lesson id, e.g. L12 (see `show`).")],
    repo: RepoOption = Path("."),
) -> None:
    """Retire one lesson so it is never used again."""
    with _context(repo) as (_, learning):
        lesson = learning.forget(lesson_id)
        console.print(f"Retired {lesson.id} from {escape(lesson.profile)}.")
