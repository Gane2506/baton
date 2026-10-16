"""`jev capbench run|grade|report` - the manual trigger for the capability benchmark.

Manual trigger only: there is deliberately no scheduled or recurring run. The
~$3-7/night model spend needs the maintainer's explicit go-ahead (design §5),
so `run` spends money only when a person asks it to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jev_agent.capbench.grading import run_grade_script
from jev_agent.capbench.models import (
    DEFAULT_PER_RUN_USD,
    DEFAULT_PROFILE,
    SuiteResult,
    load_task,
    load_tasks,
)
from jev_agent.capbench.report import write_scoreboard
from jev_agent.capbench.runner import BenchmarkConfig, run_suite
from jev_agent.cli.repo import RepoOption, find_git_root
from jev_agent.cli.wiring import fail

console = Console()

capbench_app = typer.Typer(
    name="capbench",
    help="The capability benchmark: fixed tasks run through Baton (docs/benchmarks.md).",
    no_args_is_help=True,
)

RepoArg = RepoOption


def _baton_root(repo: Path) -> Path:
    root = find_git_root(repo)
    if root is None:
        raise fail(f"{repo.resolve()} is not inside a git repository")
    return root


def _tasks_dir(root: Path) -> Path:
    tasks = root / "benchmarks" / "tasks"
    if not tasks.is_dir():
        raise fail(f"no benchmark tasks at {tasks}")
    return tasks


@capbench_app.command("run")
def run(
    task: Annotated[
        list[str] | None,
        typer.Option("--task", help="Run this task (repeatable). Default: all tasks."),
    ] = None,
    all_: Annotated[bool, typer.Option("--all", help="Run every benchmark task.")] = False,
    profile: Annotated[
        str,
        typer.Option("--profile", help="Worker-profile label recorded with the results."),
    ] = DEFAULT_PROFILE,
    budget: Annotated[
        float,
        typer.Option("--budget", min=0.01, help="Per-run budget cap in USD."),
    ] = DEFAULT_PER_RUN_USD,
    timeout_min: Annotated[
        float | None,
        typer.Option(
            "--timeout-min",
            min=0.01,
            help="Timeout per task in minutes (default: none; a run may take 30-90 min).",
        ),
    ] = None,
    workdir: Annotated[
        Path | None,
        typer.Option("--workdir", help="Where to build the sandboxes (default: temp dir)."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing capbench-suite.json in the workdir.",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print what would run; spend nothing.")
    ] = False,
    repo: RepoArg = Path("."),
) -> None:
    """Run benchmark tasks through Baton (one `jev run` per task) and grade them.

    Manual trigger only - this command spends real model money (up to --budget
    per task) and creates no schedule. Results print as a table; the raw suite
    JSON is written to the workdir for `jev capbench report`. The suite JSON is
    rewritten after every task (a crash keeps the completed results), and the
    command refuses to overwrite an existing capbench-suite.json unless --force
    is passed, so a previous suite's raw data is never silently clobbered.
    """
    root = _baton_root(repo)
    tasks_dir = _tasks_dir(root)
    specs = load_tasks(tasks_dir)
    wanted = [spec.id for spec in specs] if (all_ or not task) else list(task)
    unknown = [t for t in wanted if t not in {spec.id for spec in specs}]
    if unknown:
        raise fail(f"unknown benchmark task(s): {', '.join(unknown)}")
    config = BenchmarkConfig(
        profile=profile,
        per_run_usd=budget,
        workdir=workdir,
        keep_sandbox=True,
        timeout_min=timeout_min,
    )
    if dry_run:
        console.print(
            f"would run {len(wanted)} task(s) ({', '.join(wanted)}) under profile "
            f"{profile!r}, up to ${budget:.2f} per task, basic quality level. "
            "No model spend in a dry run."
        )
        return
    out = (config.workdir or Path.cwd()) / "capbench-suite.json"
    if out.exists() and not force:
        raise fail(
            f"{out} already exists - refusing to overwrite a previous suite's results. "
            "Pass --force to overwrite it, or use --workdir to write the new suite elsewhere."
        )
    console.print(
        f"Running {len(wanted)} benchmark task(s) ({', '.join(wanted)}); "
        f"up to ${budget:.2f} per task. This spends real model money; "
        "no schedule is created - rerun manually."
    )
    try:
        suite = run_suite(wanted, config, tasks_dir, out_path=out)
    except ValueError as exc:
        raise fail(str(exc)) from exc
    console.print(f"suite JSON: {out}")
    _print_suite(suite)


def _print_suite(suite: SuiteResult) -> None:
    table = Table(title=f"capbench suite ({suite.profile})")
    for column in ("task", "verdict", "correctness", "ship", "cost", "time", "composite"):
        table.add_column(column, overflow="fold", justify="right" if column != "task" else "left")
    for task in suite.tasks:
        verdict = "pass" if task.passed else ("not run" if not suite.evaluated else "fail")
        table.add_row(
            task.task_id,
            verdict,
            f"{task.correctness:.0f}",
            "n/a" if task.ship_score is None else str(task.ship_score),
            f"${task.cost_usd:.2f}",
            f"{task.duration_min:.1f}m",
            f"{task.composite:.0f}",
        )
    score = suite.score
    table.add_row("suite", "", "", "", "", "", f"{score:.0f}" if score is not None else "-")
    console.print(table)


@capbench_app.command("grade")
def grade(
    task_id: Annotated[str, typer.Argument(help="Benchmark task id, e.g. t1-cli-flag.")],
    result: Annotated[Path, typer.Option("--result", help="Checkout of the result to grade.")],
    run_json: Annotated[Path, typer.Option("--run-json", help="Run record JSON (ship score).")],
    diff: Annotated[Path, typer.Option("--diff", help="Unified diff of the result.")],
    repo: RepoArg = Path("."),
) -> None:
    """Grade one result with the task's `grade.py`; print its JSON report.

    Debugging aid for graders: runs the same `--result/--run-json/--diff`
    protocol the runner uses. Exit 0 when the grader passes, 1 when it fails.
    """
    root = _baton_root(repo)
    tasks_dir = _tasks_dir(root)
    try:
        spec = load_task(task_id, tasks_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise fail(str(exc)) from exc
    try:
        report = run_grade_script(spec.grade_py, result, run_json, diff)
    except RuntimeError as exc:
        raise fail(f"grader harness error: {exc}") from exc
    console.print(report.model_dump_json(indent=2))
    if not report.passed:
        raise typer.Exit(1)


@capbench_app.command("report")
def report(
    suite_json: Annotated[Path, typer.Option("--suite-json", help="Suite result JSON.")],
    md_out: Annotated[
        Path | None,
        typer.Option("--md-out", help="Scoreboard path (default: docs/benchmarks.md)."),
    ] = None,
    json_out: Annotated[
        Path | None,
        typer.Option("--json-out", help="Badge JSON path (default: docs/benchmarks.json)."),
    ] = None,
    repo: RepoArg = Path("."),
) -> None:
    """Regenerate `docs/benchmarks.md` and `docs/benchmarks.json` from a suite JSON."""
    root = _baton_root(repo)
    try:
        suite = SuiteResult.model_validate_json(suite_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise fail(f"cannot read suite JSON {suite_json}: {exc}") from exc
    md_path = md_out or root / "docs" / "benchmarks.md"
    json_path = json_out or root / "docs" / "benchmarks.json"
    write_scoreboard(
        suite, md_path=md_path, json_path=json_path, tasks_dir=root / "benchmarks" / "tasks"
    )
    console.print(f"wrote {escape(str(md_path))} and {escape(str(json_path))}")
    if not suite.evaluated:
        console.print(
            "[yellow]template:[/yellow] the suite is unevaluated - the scoreboard "
            "shows no scores until a real `jev capbench run` happens."
        )


@capbench_app.command("tasks")
def list_tasks(repo: RepoArg = Path(".")) -> None:
    """List the benchmark tasks and their grading bands."""
    root = _baton_root(repo)
    specs = load_tasks(_tasks_dir(root))
    table = Table(title="capability benchmark tasks")
    for column in ("task", "title", "max added", "max files"):
        table.add_column(column, overflow="fold")
    for spec in specs:
        table.add_row(spec.id, escape(spec.title), str(spec.max_added_lines), str(spec.max_files))
    console.print(table)
    console.print(
        "Run one with `jev capbench run --task <id>` (manual trigger; spends model money)."
    )
