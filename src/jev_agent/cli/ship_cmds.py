"""`jev ship` (open a GitHub PR for a DONE run, `jev ship sync`) and `jev kpi` (docs/ship.md)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jev_agent.cli.repo import RepoOption, find_git_root
from jev_agent.cli.wiring import STATE_DB_FILENAME, fail
from jev_agent.config import STATE_DIRNAME, ConfigError, JevConfig, load_config
from jev_agent.core.benchmarks import (
    DEFAULT_LAST,
    KPI_BY_KEY,
    format_value,
    summarize,
    trends,
)
from jev_agent.core.kpi import PrKpis, load_pr_kpis
from jev_agent.core.ship import (
    Gh,
    ShipError,
    ShipPlan,
    check_gh,
    execute_ship,
    prepare_ship,
    prs_to_sync,
    sync_pr,
)
from jev_agent.core.store import NotFoundError, ShippedPrRecord, Store

console = Console()


@dataclass
class ShipRepo:
    root: Path
    config: JevConfig
    store: Store


@contextmanager
def ship_repo(repo: Path) -> Iterator[ShipRepo]:
    """Config and store only: shipping needs no workers, dispatcher or model list."""
    root = find_git_root(repo)
    if root is None:
        raise fail(f"{repo.resolve()} is not inside a git repository")
    try:
        config = load_config(root)
    except ConfigError as exc:
        raise fail(str(exc)) from exc
    with Store.open(root / STATE_DIRNAME / STATE_DB_FILENAME) as store:
        yield ShipRepo(root, config, store)


def print_plan(plan: ShipPlan) -> None:
    table = Table(title=f"ship run {plan.run_id}", show_header=False)
    table.add_column("what", style="bold")
    table.add_column("value", overflow="fold")
    for name, value in plan.summary():
        table.add_row(name, escape(value))
    console.print(table)
    for warning in plan.warnings:
        console.print(f"[yellow]warning:[/yellow] {escape(warning)}")


def ship(
    target: Annotated[str, typer.Argument(metavar="RUN_ID | sync", help="Run to ship, or `sync`.")],
    run_id: Annotated[
        str | None, typer.Argument(metavar="[RUN_ID]", help="With `sync`: the run to sync.")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the PR body and commands; run nothing.")
    ] = False,
    force_new: Annotated[
        bool,
        typer.Option(
            "--force-new", help="Open another PR for an already shipped run, from a new branch."
        ),
    ] = False,
    all_: Annotated[bool, typer.Option("--all", help="With `sync`: every shipped PR.")] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Open a GitHub pull request for a DONE run (`jev ship RUN`), or read shipped PRs back
    from GitHub for the KPIs (`jev ship sync RUN` / `jev ship sync --all`).

    Needs ship enabled in jev.toml (docs/ship.md). Never force-pushes, never merges.
    """
    if target == "sync":
        sync(run_id, all_, repo)
        return
    if run_id is not None or all_:
        raise fail("usage: `jev ship RUN_ID` or `jev ship sync [RUN_ID | --all]`")
    with ship_repo(repo) as r:
        gh = Gh(r.root)
        try:
            plan = prepare_ship(
                r.root, r.config, r.store, target, gh=gh, force_new=force_new, dry_run=dry_run
            )
        except ShipError as exc:
            raise fail(str(exc)) from exc
        print_plan(plan)
        if dry_run:
            console.print("\n[bold]PR body[/bold]")
            console.print(plan.body, markup=False, highlight=False)
            console.print("[bold]Commands[/bold] (dry run: nothing was run)")
            if not plan.skip_push:
                console.print("  " + " ".join(plan.push_command()), markup=False)
            console.print("  " + _quoted(plan.create_command("BODY.md")), markup=False)
            return
        if not yes and not typer.confirm("Push this branch and open the PR?", default=False):
            console.print("Not shipped.")
            raise typer.Exit(1)
        try:
            record = execute_ship(plan, r.store, r.root, gh)
        except ShipError as exc:
            raise fail(str(exc)) from exc
        console.print(f"[green]opened[/green] {describe_pr(record)}")
        console.print(f"  read it back later with `jev ship sync {plan.run_id}`")


def _quoted(command: list[str]) -> str:
    return " ".join(
        arg if arg and " " not in arg and '"' not in arg else json.dumps(arg) for arg in command
    )


def describe_pr(pr: ShippedPrRecord) -> str:
    number = f"PR #{pr.number}" if pr.number is not None else "a PR"
    where = pr.url or f"{pr.repo} ({pr.branch})"
    return f"{number}: {where}"


def sync(run_id: str | None, all_: bool, repo: Path) -> None:
    if (run_id is None) == (not all_):
        raise fail("give a run id or --all: `jev ship sync RUN_ID` / `jev ship sync --all`")
    with ship_repo(repo) as r:
        try:
            prs = prs_to_sync(r.store, run_id)
        except (ShipError, NotFoundError) as exc:
            raise fail(str(exc)) from exc
        if not prs:
            console.print("Nothing shipped yet. Open a PR with `jev ship RUN_ID`.")
            return
        gh = Gh(r.root)
        problem = check_gh(gh)
        if problem is not None:
            raise fail(problem)
        failed = 0
        for pr in prs:
            try:
                updated = sync_pr(r.store, gh, pr)
            except ShipError as exc:
                failed += 1
                console.print(f"[red]error:[/red] {escape(describe_pr(pr))}: {escape(str(exc))}")
                continue
            console.print(f"{escape(describe_pr(updated))}: {pr_status(updated)}", highlight=False)
        if failed:
            raise typer.Exit(1)


def pr_status(pr: ShippedPrRecord) -> str:
    """`merged after 5.2h, CI success, 3 review comments, 1 commit after open`."""
    parts = [pr.state + (" (draft)" if pr.draft and pr.state == "open" else "")]
    if pr.time_to_merge_h is not None:
        parts[0] += f" after {format_value(pr.time_to_merge_h, 'hours')}"
    if pr.ci_status:
        parts.append(f"CI {pr.ci_status}")
    if pr.review_comments is not None:
        parts.append(f"{pr.review_comments} review comment(s)")
    if pr.commits_after_open is not None:
        parts.append(f"{pr.commits_after_open} commit(s) after open")
    if pr.reverted:
        parts.append("REVERTED")
    return ", ".join(parts)


# ---- jev kpi -------------------------------------------------------------------------------


def _row_json(row: PrKpis) -> dict[str, Any]:
    pr = row.pr
    return {
        "pr": {
            "number": pr.number,
            "url": pr.url,
            "repo": pr.repo,
            "branch": pr.branch,
            "base": pr.base,
            "title": pr.title,
            "state": pr.state,
            "draft": pr.draft,
            "created_at": pr.created_at.isoformat(),
            "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
            "closed_at": pr.closed_at.isoformat() if pr.closed_at else None,
            "time_to_merge_h": pr.time_to_merge_h,
            "review_comments": pr.review_comments,
            "reviews": pr.reviews,
            "ci_status": pr.ci_status,
            "commits_after_open": pr.commits_after_open,
            "additions": pr.additions,
            "deletions": pr.deletions,
            "changed_files": pr.changed_files,
            "reverted": pr.reverted,
            "last_synced_at": pr.last_synced_at.isoformat() if pr.last_synced_at else None,
        },
        "run": row.run.model_dump(mode="json"),
    }


def kpi_report(rows: list[PrKpis], last: int) -> dict[str, Any]:
    """`jev kpi --json`: every PR with its run KPIs, medians, trends and regressions."""
    summary = summarize(rows)
    found = trends(rows, last)
    return {
        "prs": [_row_json(row) for row in rows],
        "summary": {
            "prs": summary.prs,
            "merged": summary.merged,
            "merged_share": summary.merged_share,
            "median_cost_usd": summary.median_cost_usd,
            "median_time_to_merge_h": summary.median_time_to_merge_h,
            "median_first_pass_rate": summary.median_first_pass_rate,
            "median_fallback_rate": summary.median_fallback_rate,
            "median_attempts_per_task": summary.median_attempts_per_task,
            "median_wall_clock_h": summary.median_wall_clock_h,
        },
        "trends": [
            {
                "kpi": t.kpi.key,
                "label": t.kpi.label,
                "points": [
                    {
                        "pr": p.label,
                        "value": p.value,
                        "rolling_median": p.rolling,
                        "baseline": p.baseline,
                        "regression": p.regression,
                    }
                    for p in t.points
                ],
                "regression": t.regressed,
            }
            for t in found
        ],
        "regressions": [t.message() for t in found if t.regressed],
    }


def _fmt(key: str, value: float | None) -> str:
    return KPI_BY_KEY[key].format(value)


def kpi(
    last: Annotated[
        int, typer.Option("--last", min=1, help="How many of the latest shipped PRs.")
    ] = DEFAULT_LAST,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    repo: RepoOption = Path("."),
) -> None:
    """KPIs of the PRs opened by `jev ship`: one row per PR, medians, and regressions against
    the rolling median. Refresh GitHub data first with `jev ship sync --all`."""
    with ship_repo(repo) as r:
        rows = load_pr_kpis(r.store, last)
    if as_json:
        print(json.dumps(kpi_report(rows, last), indent=2))
        return
    if not rows:
        console.print(
            "No shipped PRs yet. Open one for a DONE run with `jev ship RUN_ID` "
            "(needs `[ship] enabled = true`; see docs/ship.md)."
        )
        return
    table = Table(title=f"KPIs of the last {len(rows)} shipped PR(s)")
    for column in ("PR", "run", "state", "cost", "time", "att/task", "1st pass", "findings"):
        table.add_column(column, overflow="fold", justify="right" if column != "run" else "left")
    for column in ("fallback", "merged in", "CI"):
        table.add_column(column, overflow="fold", justify="right")
    for row in rows:
        pr = row.pr
        state = pr.state + ("*" if pr.reverted else "")
        table.add_row(
            row.label,
            pr.run_id,
            state,
            _fmt("cost_usd", row.value("cost_usd")),
            _fmt("wall_clock_h", row.value("wall_clock_h")),
            _fmt("attempts_per_task", row.value("attempts_per_task")),
            _fmt("first_pass_rate", row.value("first_pass_rate")),
            _fmt("findings_total", row.value("findings_total")),
            _fmt("fallback_rate", row.value("fallback_rate")),
            _fmt("time_to_merge_h", row.value("time_to_merge_h")),
            pr.ci_status or "-",
        )
    s = summarize(rows)
    merged = "-" if s.merged_share is None else f"{s.merged}/{s.prs} merged"
    table.add_section()
    table.add_row(
        "median",
        "",
        merged,
        _fmt("cost_usd", s.median_cost_usd),
        _fmt("wall_clock_h", s.median_wall_clock_h),
        _fmt("attempts_per_task", s.median_attempts_per_task),
        _fmt("first_pass_rate", s.median_first_pass_rate),
        "",
        _fmt("fallback_rate", s.median_fallback_rate),
        _fmt("time_to_merge_h", s.median_time_to_merge_h),
        "",
        style="bold",
    )
    console.print(table)
    if any(row.pr.reverted for row in rows):
        console.print("* reverted after merging", highlight=False)
    flagged = [t for t in trends(rows, last) if t.regressed]
    if not flagged:
        console.print("No regressions: every KPI's latest value is near its rolling median.")
    for t in flagged:
        console.print(f"[red]regression:[/red] {escape(t.message())}", highlight=False)
    never = sum(1 for row in rows if row.pr.last_synced_at is None)
    if never:
        console.print(
            f"{never} PR(s) never synced: run `jev ship sync --all` for their GitHub data.",
            highlight=False,
        )
