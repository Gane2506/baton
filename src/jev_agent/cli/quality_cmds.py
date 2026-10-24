"""`jev quality show RUN` and `jev quality checks` (docs/quality.md)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from jev_agent.app.quality import (
    CheckRow,
    ai_review_row,
    check_rows,
    missing_tools,
    review_path,
    review_text,
)
from jev_agent.cli.repo import RepoOption, find_git_root
from jev_agent.cli.run_cmds import RunId
from jev_agent.cli.wiring import fail, repo_context
from jev_agent.config import ConfigError, load_config
from jev_agent.core.store import NotFoundError
from jev_agent.quality.levels import LEVEL_SUMMARY, LEVELS

__all__ = ["CheckRow", "ai_review_row", "check_rows", "missing_tools", "review_path", "review_text"]

console = Console()

quality_app = typer.Typer(
    name="quality",
    help="The quality and security gate: reports and available checks.",
    no_args_is_help=True,
)


@quality_app.command("show")
def show(run_id: RunId, repo: RepoOption = Path(".")) -> None:
    """Print the run's quality review (review.md)."""
    with repo_context(repo, create=False) as ctx:
        try:
            run = ctx.store.get_run(run_id)
            reports = ctx.store.list_quality_reports(run_id)
        except NotFoundError as exc:
            raise fail(str(exc)) from exc
        if not reports:
            console.print(
                f"No quality gate has run for run {run_id} (level {run.quality_level}). "
                "Set a level with `jev run --quality LEVEL` or `\\[quality] level`."
            )
            return
        path = review_path(ctx.root, run_id)
        text = review_text(ctx.root, run, reports)
        console.print(text, markup=False, highlight=False, soft_wrap=True)
        console.print(f"(per-gate reports: {path.parent})", markup=False, highlight=False)


@quality_app.command("checks")
def checks(repo: RepoOption = Path(".")) -> None:
    """List the checks by level, whether each tool is installed and applies to this repo."""
    root = find_git_root(repo)
    if root is None:
        raise fail(f"{repo.resolve()} is not inside a git repository")
    try:
        config = load_config(root)
    except ConfigError as exc:
        raise fail(str(exc)) from exc
    table = Table(title=f"quality checks (this repo: level {config.quality.level})")
    for column in ("from level", "check", "tool", "installed", "applies here", "blocks from"):
        table.add_column(column, overflow="fold")
    # The AI review is not a registered check, so it is not in `check_rows` —
    # but its row is derived in `app.quality`, not hardcoded here (issue #223).
    for row in [*check_rows(root, config), ai_review_row(config)]:
        style = (
            "green" if row.installed.startswith(("yes", "built", "varies", "project")) else "yellow"
        )
        table.add_row(
            row.level,
            row.name,
            row.tool,
            f"[{style}]{row.installed}[/{style}]",
            "yes" if row.applies else "no",
            row.blocks_from,
        )
    console.print(table)
    for level in LEVELS:
        console.print(f"  {level}: {LEVEL_SUMMARY[level]}", highlight=False)
    console.print(
        "Missing tools are skipped (never fail a run) unless listed in \\[quality] required_tools."
    )
