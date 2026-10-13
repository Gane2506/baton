"""`jev init`: create jev.toml and .jev/, and ignore .jev/ in git (`--migrate`: from jao v0.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from jev_agent.cli.repo import RepoOption, find_git_root
from jev_agent.config import CONFIG_FILENAME, DEFAULT_CONFIG_TOML, STATE_DIRNAME

console = Console()

_IGNORE_ENTRY = f"{STATE_DIRNAME}/"


def init(
    repo: RepoOption = Path("."),
    force: Annotated[
        bool, typer.Option("--force", help=f"Overwrite an existing {CONFIG_FILENAME}.")
    ] = False,
    migrate: Annotated[
        bool,
        typer.Option(
            "--migrate",
            help="Move a repo set up with jao v0.1 to the jev names (jao.toml, .jao/, branches).",
        ),
    ] = False,
) -> None:
    """Create jev.toml and .jev/ in the repo, and add .jev/ to .gitignore."""
    root = find_git_root(repo, allow_legacy=migrate)
    if root is None:
        console.print(f"[red]error:[/red] {repo.resolve()} is not inside a git repository.")
        raise typer.Exit(1)
    if migrate:
        _migrate(root)
        return

    config_path = root / CONFIG_FILENAME
    if config_path.exists() and not force:
        console.print(
            f"[red]error:[/red] {config_path} already exists. Use --force to overwrite it."
        )
        raise typer.Exit(1)
    existed = config_path.exists()
    config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    console.print(f"{'Overwrote' if existed else 'Wrote'} {config_path}")

    state_dir = root / STATE_DIRNAME
    if state_dir.is_dir():
        console.print(f"{state_dir} already exists")
    else:
        state_dir.mkdir()
        console.print(f"Created {state_dir}/")

    if add_to_gitignore(root / ".gitignore", _IGNORE_ENTRY):
        console.print(f"Added {_IGNORE_ENTRY} to {root / '.gitignore'}")
    else:
        console.print(f"{_IGNORE_ENTRY} is already in {root / '.gitignore'}")

    console.print("[green]Done.[/green] Run [bold]jev doctor[/bold] to check your setup.")


def add_to_gitignore(gitignore: Path, entry: str) -> bool:
    """Append `entry` to `gitignore` unless an equivalent line exists. Return True if added."""
    text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    bare = entry.strip("/")
    equivalents = {bare, f"{bare}/", f"/{bare}", f"/{bare}/"}
    if any(line.strip() in equivalents for line in text.splitlines()):
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    gitignore.write_text(f"{text}{entry}\n", encoding="utf-8")
    return True


def _migrate(root: Path) -> None:
    from jev_agent.cli.migrate import MigrateError
    from jev_agent.cli.migrate import migrate as run_migrate

    try:
        report = run_migrate(root)
    except MigrateError as exc:
        console.print(f"[red]error:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from exc
    changed = (report.moved, report.renamed_refs, report.skipped_refs, report.gitignore)
    if not (any(changed) or report.db_rows or report.problems):
        console.print("Nothing to migrate: no jao.toml, .jao/ or jao/* branches found.")
        return
    for item in report.moved:
        console.print(f"Renamed {item}")
    for path in report.repaired_worktrees:
        console.print(f"Repaired worktree {path} (git now follows it to its new place)")
    if report.gitignore:
        console.print(f"Updated .gitignore: {report.gitignore}")
    if report.db_rows:
        console.print(f"Updated {report.db_rows} log path(s) in {STATE_DIRNAME}/state.db")
    if report.renamed_refs:
        console.print(f"Renamed {len(report.renamed_refs)} local branch(es)/ref(s) jao/* → jev/*")
    for ref, reason in report.skipped_refs:
        console.print(f"[yellow]skipped[/yellow] {ref}: {escape(reason)}; rename it yourself")
    for problem in report.problems:
        console.print(
            f"[yellow]not done[/yellow] {escape(problem)}; run `jev init --migrate` again"
        )
    if report.problems:
        raise typer.Exit(1)
    console.print("[green]Done.[/green] Nothing was pushed. Run [bold]jev doctor[/bold] to check.")
