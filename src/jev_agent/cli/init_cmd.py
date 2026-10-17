"""`jev init`: create jev.toml and .jev/, and ignore .jev/ in git (`--migrate`: from jao v0.1)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from jev_agent.cli.repo import RepoOption, find_git_root
from jev_agent.cli.wiring import fail
from jev_agent.config import CONFIG_FILENAME, DEFAULT_CONFIG_TOML, STATE_DIRNAME

console = Console()

_IGNORE_ENTRY = f"{STATE_DIRNAME}/"


def is_interactive() -> bool:
    """Whether to show the first-run welcome screen (tests patch this)."""
    return sys.stdin.isatty()


def print_welcome() -> None:
    """Branded first-run orientation, shown only on interactive `jev init`."""
    console.print()
    console.print("[bold]Welcome to Baton![/bold]")
    console.print("Baton turns an idea into reviewed, tested code.")
    console.print("Happy path:")
    console.print("  1. [bold]jev doctor[/bold] — check your setup")
    console.print('  2. [bold]jev run "your idea"[/bold] — plan, build, review, test')
    console.print("  3. [bold]jev dashboard[/bold] — watch it happen")
    console.print("Need help? [bold]jev --help[/bold] or the docs at github.com/Gane2506/baton")


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
    demo: Annotated[
        bool,
        typer.Option(
            "--demo",
            help="Seed a demo project: sample runs, queue items, lessons and spend data, "
            "clearly marked as demo. Touches nothing outside this repo.",
        ),
    ] = False,
) -> None:
    """Create jev.toml and .jev/ in the repo, and add .jev/ to .gitignore."""
    root = find_git_root(repo, allow_legacy=migrate)
    if root is None:
        raise fail(f"{repo.resolve()} is not inside a git repository.")
    if migrate and demo:
        raise fail("Cannot combine --demo with --migrate.")
    if migrate:
        _migrate(root)
        return

    if demo:
        # Fail before writing anything: the seeder's own guard runs after jev.toml and
        # .gitignore are already changed, which would leave residue behind (audit 2026-10-16).
        _demo_precheck(root)

    config_path = root / CONFIG_FILENAME
    if config_path.exists() and not force:
        raise fail(f"{config_path} already exists. Use --force to overwrite it.")
    existed = config_path.exists()
    config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    console.print(f"{'Overwrote' if existed else 'Wrote'} {config_path}", markup=False)

    state_dir = root / STATE_DIRNAME
    if state_dir.is_dir():
        console.print(f"{state_dir} already exists", markup=False)
    else:
        state_dir.mkdir()
        console.print(f"Created {state_dir}/", markup=False)

    if add_to_gitignore(root / ".gitignore", _IGNORE_ENTRY):
        console.print(f"Added {_IGNORE_ENTRY} to {root / '.gitignore'}", markup=False)
    else:
        console.print(f"{_IGNORE_ENTRY} is already in {root / '.gitignore'}", markup=False)

    if demo:
        _seed_demo(root)
    console.print("[green]Done.[/green] Run [bold]jev doctor[/bold] to check your setup.")
    if is_interactive():
        print_welcome()


def _demo_precheck(root: Path) -> None:
    """`fail` when `--demo` cannot seed this repo; called before `init` writes anything."""
    from jev_agent.demo import STATE_DB_FILENAME, is_demo_repo

    state_dir = root / STATE_DIRNAME
    if (state_dir / STATE_DB_FILENAME).exists() or is_demo_repo(root):
        raise fail(
            f"{state_dir} already holds state: --demo seeds a fresh repo only. "
            "Start from a clean repo instead."
        )


def _seed_demo(root: Path) -> None:
    """Run the demo seeder against the freshly initialised repo and mark the data as demo."""
    from jev_agent.demo import marker_path, write_demo_marker
    from jev_agent.demo.seeder import seed

    _demo_precheck(root)  # re-checked: the state could have changed since `init` checked
    try:
        print(seed(root, demo_config=False))
    except SystemExit as exc:
        raise fail(
            f"demo seeding failed: {exc} (jev.toml and .gitignore were already updated; "
            f"delete {root / STATE_DIRNAME}/ and retry, or start from a clean repo)"
        ) from exc
    except Exception as exc:  # CLI boundary: report the failure, don't traceback
        raise fail(
            f"demo seeding failed unexpectedly: {exc} (jev.toml and .gitignore were already "
            f"updated; delete {root / STATE_DIRNAME}/ and retry, or start from a clean repo)"
        ) from exc
    write_demo_marker(root, seeded_by="jev init --demo")
    console.print(
        f"Demo data seeded and marked as demo in {escape(str(marker_path(root)))} "
        f"(logs are labeled {escape('[demo]')})."
    )
    console.print(
        "Explore it with [bold]jev board[/bold] or [bold]jev dashboard[/bold]; "
        "remove it with [bold]jev demo reset[/bold]."
    )


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
        raise fail(str(exc)) from exc
    changed = (report.moved, report.renamed_refs, report.skipped_refs, report.gitignore)
    if not (any(changed) or report.db_rows or report.problems):
        console.print("Nothing to migrate: no jao.toml, .jao/ or jao/* branches found.")
        return
    for item in report.moved:
        console.print(f"Renamed {item}", markup=False)
    for path in report.repaired_worktrees:
        console.print(
            f"Repaired worktree {path} (git now follows it to its new place)", markup=False
        )
    if report.gitignore:
        console.print(f"Updated .gitignore: {report.gitignore}", markup=False)
    if report.db_rows:
        console.print(f"Updated {report.db_rows} log path(s) in {STATE_DIRNAME}/state.db")
    if report.renamed_refs:
        console.print(f"Renamed {len(report.renamed_refs)} local branch(es)/ref(s) jao/* → jev/*")
    for ref, reason in report.skipped_refs:
        console.print(
            f"[yellow]skipped[/yellow] {escape(ref)}: {escape(reason)}; rename it yourself"
        )
    for problem in report.problems:
        console.print(
            f"[yellow]not done[/yellow] {escape(problem)}; run `jev init --migrate` again"
        )
    if report.problems:
        raise typer.Exit(1)
    console.print("[green]Done.[/green] Nothing was pushed. Run [bold]jev doctor[/bold] to check.")
