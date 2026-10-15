"""`jev demo ...`: work with demo-seeded state (`jev init --demo`, docs/demo.md)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from jev_agent.cli.repo import RepoOption, find_git_root
from jev_agent.cli.wiring import fail
from jev_agent.config import STATE_DIRNAME
from jev_agent.demo import DEMO_MARKER_FILENAME, read_demo_marker

console = Console()

demo_app = typer.Typer(
    name="demo",
    help="Demo data seeded by `jev init --demo`: inspect it, remove it.",
    no_args_is_help=True,
)


@demo_app.command("reset")
def reset(
    repo: RepoOption = Path("."),
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Remove the demo data without asking.")
    ] = False,
) -> None:
    """Remove demo-seeded state from this repo, leaving a clean `.jev/` behind."""
    root = find_git_root(repo)
    if root is None:
        raise fail(f"{repo.resolve()} is not inside a git repository.")
    marker = read_demo_marker(root)
    if marker is None:
        raise fail(
            f"{root} has no demo data ({STATE_DIRNAME}/{DEMO_MARKER_FILENAME} not found): "
            "nothing to reset."
        )
    if not yes and not typer.confirm("Remove all demo data from this repo?"):
        raise typer.Abort()
    state_dir = root / STATE_DIRNAME
    shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"Removed demo data from {state_dir}/", markup=False)
    console.print(
        "The repo is clean again. Re-seed with [bold]jev init --demo --force[/bold]."
    )
