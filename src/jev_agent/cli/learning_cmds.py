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

from jev_agent.app.learning import (
    active_profile,
    create_profile,
    use_learning_profile,
)
from jev_agent.app.learning import (
    delete as delete_learning_profile,
)
from jev_agent.app.learning import (
    disable as disable_learning_profile,
)
from jev_agent.app.learning import (
    enable as enable_learning_profile,
)
from jev_agent.app.learning import (
    flush as flush_learning_profile,
)
from jev_agent.app.learning import (
    forget as forget_lesson,
)
from jev_agent.app.learning import (
    graph_link as link_graph_edge,
)
from jev_agent.app.learning import (
    graph_query as query_lesson_graph,
)
from jev_agent.app.learning import (
    graph_show as show_lesson_neighborhood,
)
from jev_agent.app.learning import (
    graph_stats as lesson_graph_stats,
)
from jev_agent.app.learning import (
    graph_unlink as unlink_graph_edge,
)
from jev_agent.app.learning import (
    lessons as profile_lessons,
)
from jev_agent.app.learning import (
    overview as learning_overview,
)
from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import RepoContext, fail, repo_context
from jev_agent.core.learning import GRAPH_EDGE_TYPES, Learning, LearningError
from jev_agent.core.store import NotFoundError

console = Console()

learning_app = typer.Typer(
    name="learning",
    help="Lessons from past runs that feed future runs: profiles, on/off, delete.",
    no_args_is_help=True,
)

ProfileArg = Annotated[str, typer.Argument(help="Profile name.")]
OptionalProfileArg = Annotated[
    str | None, typer.Argument(help="Profile name (default: the active profile).")
]
YesOption = Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")]


@contextmanager
def _context(repo: Path, *, create: bool = True) -> Iterator[tuple[RepoContext, Learning]]:
    """Config, state DB and profiles; `create=False` for the read-only commands (#26)."""
    with repo_context(repo, create=create) as ctx:
        try:
            yield ctx, ctx.learning
        except LearningError as exc:
            raise fail(str(exc)) from exc


def _active(ctx: RepoContext) -> str:
    return active_profile(ctx.store, ctx.config)


@learning_app.command("list")
def list_profiles(repo: RepoOption = Path(".")) -> None:
    """List profiles, with scope, on/off, lesson count and the active one."""
    with _context(repo, create=False) as (ctx, learning):
        view = learning_overview(ctx.store, ctx.config, learning)
        status = view.status
        state = "[green]on[/green]" if status.enabled else "off"
        console.print(
            f"learning is {state} for this repo ({status.enabled_source}); "
            f"active profile: [bold]{escape(status.profile)}[/bold] ({status.profile_source})"
        )
        profiles = view.profiles
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
    with _context(repo, create=False) as (ctx, learning):
        name = profile or _active(ctx)
        lessons = profile_lessons(learning, name, include_retired)
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
        profile = create_profile(learning, name, shared=shared)
        where = learning.shared_path if shared else "this repo's .jev/state.db"
        console.print(f"Created profile {escape(profile.name)} ({profile.scope}: {where}).")


@learning_app.command("use")
def use(name: ProfileArg, repo: RepoOption = Path(".")) -> None:
    """Make a profile the active one for this repo."""
    with _context(repo) as (ctx, learning):
        use_learning_profile(ctx.store, learning, name)
        status = learning_overview(ctx.store, ctx.config, learning).status
        note = "" if status.enabled else " Learning is off; `jev learning enable` turns it on."
        console.print(f"Active profile: {escape(name)}.{note}")


@learning_app.command("enable")
def enable(name: OptionalProfileArg = None, repo: RepoOption = Path(".")) -> None:
    """Turn learning on for this repo (no NAME), or re-enable one profile."""
    with _context(repo) as (ctx, learning):
        active = enable_learning_profile(ctx.store, ctx.config, learning, name)
        if name is None:
            console.print(
                f"Learning is on for this repo, with profile {escape(active)}. Each run now "
                "ends with one learner call when it has something to learn."
            )
        else:
            console.print(f"Profile {escape(name)} is enabled.")


@learning_app.command("disable")
def disable(name: OptionalProfileArg = None, repo: RepoOption = Path(".")) -> None:
    """Turn learning off for this repo (no NAME), or disable one profile."""
    with _context(repo) as (ctx, learning):
        disable_learning_profile(ctx.store, learning, name)
        if name is None:
            console.print("Learning is off for this repo. Lessons are kept.")
        else:
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
        count = flush_learning_profile(learning, target)
        console.print(f"Deleted {count} lesson(s) from {escape(target)}.")


@learning_app.command("delete")
def delete(name: ProfileArg, yes: YesOption = False, repo: RepoOption = Path(".")) -> None:
    """Delete a profile and all its lessons."""
    with _context(repo) as (ctx, learning):
        learning.db_for(name)  # unknown profile: error before asking
        if not yes and not typer.confirm(f"Delete profile {name} and all its lessons?"):
            raise typer.Exit(1)
        delete_learning_profile(ctx.store, learning, name)
        console.print(f"Deleted profile {escape(name)}.")


@learning_app.command("forget")
def forget(
    lesson_id: Annotated[str, typer.Argument(help="Lesson id, e.g. L12 (see `show`).")],
    repo: RepoOption = Path("."),
) -> None:
    """Retire one lesson so it is never used again."""
    with _context(repo) as (_, learning):
        lesson = forget_lesson(learning, lesson_id)
        console.print(f"Retired {lesson.id} from {escape(lesson.profile)}.")


graph_app = typer.Typer(
    name="graph",
    help="The lesson graph: full-text search, neighborhoods, and manual edges.",
    no_args_is_help=True,
)
learning_app.add_typer(graph_app)

GraphProfileOption = Annotated[
    str | None,
    typer.Option("--profile", help="Profile name (default: the active profile)."),
]


def _run_note(ctx: RepoContext, run_id: str | None) -> str:
    """`run <id> (<state> · <date>)` for a source run, or just the id when the run is gone
    (shared profiles point at runs of other repos)."""
    if not run_id:
        return ""
    try:
        run = ctx.store.get_run(run_id)
    except NotFoundError:
        return f"run {run_id}"
    return f"run {run_id} ({run.state.value}, {run.created_at.date().isoformat()})"


@graph_app.command("query")
def graph_query(
    text: Annotated[str, typer.Argument(help="Full-text query over lesson text and tags.")],
    module: Annotated[
        str | None,
        typer.Option("--module", help="Only lessons applying to this module (prefix match)."),
    ] = None,
    from_runs: Annotated[
        str | None,
        typer.Option("--from-runs", help="Only lessons from runs in this state, e.g. failed."),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only lessons from runs created on/after YYYY-MM-DD."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Most lessons to show.")] = 20,
    profile: GraphProfileOption = None,
    repo: RepoOption = Path("."),
) -> None:
    """Search lessons: full text, filtered by module and by the state/date of the runs
    they came from."""
    with _context(repo, create=False) as (ctx, learning):
        name = profile or _active(ctx)
        lessons = query_lesson_graph(
            learning, name, text, module=module, run_state=from_runs, since=since, limit=limit
        )
        if not lessons:
            console.print("No lessons match.")
            return
        table = Table(title=f"lessons matching {escape(text)}")
        for column in ("id", "score", "uses", "lesson"):
            table.add_column(column, overflow="fold")
        db = learning.db_for(name)
        for lesson in lessons:
            edges = db.edges_for_lesson(name, lesson.number)
            notes = [_run_note(ctx, lesson.source_run)] if lesson.source_run else []
            notes += [f"applies-to: {v.other}" for v in edges if v.edge == "applies-to"]
            foes = [v.other for v in edges if v.edge == "contradicts"]
            if foes:
                notes.append(f"⚠ contradicts {', '.join(sorted(set(foes)))}")
            cell = escape(lesson.text)
            if notes:
                cell += "\n[dim]" + escape(" · ".join(n for n in notes if n)) + "[/dim]"
            table.add_row(lesson.id, f"{lesson.score:g}", str(lesson.uses), cell)
        console.print(table)


@graph_app.command("show")
def graph_show(
    lesson_id: Annotated[str, typer.Argument(help="Lesson id, e.g. L12.")],
    repo: RepoOption = Path("."),
) -> None:
    """Show a lesson and its 1-hop graph neighborhood: lineage, contradictions, modules,
    the run it came from."""
    with _context(repo, create=False) as (ctx, learning):
        view = show_lesson_neighborhood(learning, lesson_id, ctx.store.connection)
        lesson = view.lesson
        console.print(
            f"[bold]{lesson.id}[/bold] {escape(lesson.text)}"
            f"  [dim]score {lesson.score:g} · uses {lesson.uses}"
            f" · profile {escape(lesson.profile)}"
            f"{' · retired' if lesson.retired else ''}[/dim]"
        )
        by_kind: dict[str, list[str]] = {}
        for edge in view.edges:
            if edge.edge == "derived-from":
                label = _run_note(ctx, edge.other)
            elif edge.other_type == "lesson":
                try:
                    _, other = learning.find_lesson(edge.other)
                    label = f"{edge.other} {escape(other.text[:60])} (score {other.score:g})"
                except LearningError:
                    label = edge.other
            else:
                label = escape(edge.other)
            key = edge.edge if edge.direction == "out" else f"{edge.edge}-by"
            if edge.edge == "contradicts":
                key = "contradicts"  # symmetric in display
            by_kind.setdefault(key, []).append(label)
        for kind in ("derived-from", "applies-to", "supersedes", "supersedes-by", "contradicts"):
            for label in by_kind.get(kind, []):
                console.print(f"  [dim]{kind:13}[/dim] {label}")
        for other_id, times in view.co_occurs:
            console.print(f"  [dim]co-occurs     [/dim] {other_id} (injected together {times}x)")


@graph_app.command("link")
def graph_link(
    src: Annotated[str, typer.Argument(help="Lesson id, e.g. L31.")],
    edge: Annotated[str, typer.Argument(help=f"Edge type: {', '.join(GRAPH_EDGE_TYPES)}.")],
    dst: Annotated[str, typer.Argument(help="Lesson id, or a module path for `applies-to`.")],
    repo: RepoOption = Path("."),
) -> None:
    """Record a graph edge by hand: `link L31 supersedes L12`, `link L4 contradicts L9`,
    `link L12 applies-to src/auth/`."""
    with _context(repo) as (_, learning):
        if edge not in GRAPH_EDGE_TYPES:
            raise fail(f"unknown edge {edge!r} (use: {', '.join(GRAPH_EDGE_TYPES)})")
        recorded = link_graph_edge(learning, src, edge, dst)
        note = (
            " (advisory: the old lesson stays active until a human retires it)"
            if edge == "supersedes"
            else " (advisory: both lessons stay active)"
            if edge == "contradicts"
            else ""
        )
        console.print(f"Recorded: {recorded.src} {recorded.edge} {escape(recorded.dst)}.{note}")


@graph_app.command("unlink")
def graph_unlink(
    src: Annotated[str, typer.Argument(help="Lesson id, e.g. L31.")],
    edge: Annotated[str, typer.Argument(help=f"Edge type: {', '.join(GRAPH_EDGE_TYPES)}.")],
    dst: Annotated[str, typer.Argument(help="Lesson id, or a module path for `applies-to`.")],
    repo: RepoOption = Path("."),
) -> None:
    """Remove a recorded graph edge (`contradicts` matches in either direction)."""
    with _context(repo) as (_, learning):
        if unlink_graph_edge(learning, src, edge, dst):
            console.print(f"Removed: {src} {edge} {escape(dst)}.")
        else:
            console.print(f"No such edge: {src} {edge} {escape(dst)}.")


@graph_app.command("stats")
def graph_stats(
    profile: GraphProfileOption = None,
    repo: RepoOption = Path("."),
) -> None:
    """Lesson and edge counts, open contradictions, and the most-applied-to modules."""
    with _context(repo, create=False) as (ctx, learning):
        name = profile or _active(ctx)
        stats = lesson_graph_stats(learning, name)
        edges = ", ".join(f"{kind} {count}" for kind, count in sorted(stats.edges.items()))
        retired = f" ({stats.retired} retired)" if stats.retired else ""
        console.print(f"lessons {stats.lessons}{retired} · edges {edges or 'none'}")
        console.print(f"open contradictions: {stats.contradictions_open}")
        if stats.top_modules:
            console.print(
                "top modules: "
                + ", ".join(f"{escape(path)} ({n})" for path, n in stats.top_modules)
            )
