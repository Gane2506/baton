"""`jev quality show RUN` and `jev quality checks` (docs/quality.md)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from jev_agent.cli.repo import RepoOption, find_git_root
from jev_agent.cli.run_cmds import RunId
from jev_agent.cli.wiring import cli_context, fail
from jev_agent.config import STATE_DIRNAME, ConfigError, JevConfig, load_config
from jev_agent.core.store import NotFoundError
from jev_agent.quality import tools
from jev_agent.quality.checks import CHECKS, QualityCheck
from jev_agent.quality.gate import RUN_REVIEW_FILENAME, quality_dir
from jev_agent.quality.levels import LEVEL_SUMMARY, LEVELS, QualityLevel, at_least
from jev_agent.quality.report import render_run_review

console = Console()

quality_app = typer.Typer(
    name="quality",
    help="The quality and security gate: reports and available checks.",
    no_args_is_help=True,
)


@quality_app.command("show")
def show(run_id: RunId, repo: RepoOption = Path(".")) -> None:
    """Print the run's quality review (review.md)."""
    with cli_context(repo) as ctx:
        try:
            run = ctx.store.get_run(run_id)
            reports = ctx.store.list_quality_reports(run_id)
        except NotFoundError as exc:
            raise fail(str(exc)) from exc
        if not reports:
            console.print(
                f"No quality gate has run for run {run_id} (level {run.quality_level}). "
                "Set a level with `jev run --quality LEVEL` or `[quality] level`."
            )
            return
        path = quality_dir(ctx.root / STATE_DIRNAME, run_id) / RUN_REVIEW_FILENAME
        if path.is_file():
            text = path.read_text(encoding="utf-8")
        else:  # removed by hand: rebuild it from the database
            text = render_run_review(run_id, run.quality_level, [r.result for r in reports])
        console.print(text, markup=False, highlight=False, soft_wrap=True)
        console.print(f"(per-gate reports: {path.parent})", markup=False, highlight=False)


@dataclass(frozen=True)
class CheckRow:
    level: QualityLevel  # the first level the check runs at
    name: str
    tool: str
    installed: str  # "yes" | "no" | "built in" | "fallback" | "varies"
    applies: bool
    blocks_from: str


def check_rows(
    root: Path, config: JevConfig, checks: list[QualityCheck] | None = None
) -> list[CheckRow]:
    """One row per registered check: where it starts, its tool and whether it is installed."""
    rows = []
    for check in CHECKS if checks is None else checks:
        tool = check.tool or "-"
        if check.tool is None:
            installed = "varies" if check.name == "tests" else "built in"
        elif check.tool == "pytest-cov":
            installed = "project dependency"
        elif tools.resolve_tool(check.tool, (root,), tools.which):
            installed = "yes"
        elif check.name == "secrets":
            installed = "no (built-in scan)"
        else:
            installed = "no"
        if check.tool and check.tool in config.quality.required_tools:
            installed += " (required)"
        rows.append(
            CheckRow(
                check.levels[0],
                check.name,
                tool,
                installed,
                check.applies(root),
                check.gate_from or "-",
            )
        )
    return rows


def missing_tools(root: Path, config: JevConfig, level: QualityLevel) -> list[str]:
    """Tools of checks that run at `level` and apply to the repo but are not installed."""
    return [
        row.tool
        for row in check_rows(root, config)
        if at_least(level, row.level)
        and row.applies
        and row.installed.startswith("no")
        and "built-in" not in row.installed
    ]


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
    for row in check_rows(root, config):
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
    table.add_row("basic+", "ai-review", "code_reviewer role", "-", "yes", "findings")
    console.print(table)
    for level in LEVELS:
        console.print(f"  {level}: {LEVEL_SUMMARY[level]}", highlight=False)
    console.print(
        "Missing tools are skipped (never fail a run) unless listed in [quality] required_tools."
    )
