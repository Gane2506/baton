"""`jev status` and `jev log`: read-only views of runs in the state database."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jev_agent.app.format import (
    format_answers,
    format_time,
    learning_line,
    quality_line,
)
from jev_agent.cli.repo import RepoOption
from jev_agent.cli.run_cmds import RunId
from jev_agent.cli.wiring import fail, repo_context
from jev_agent.config.schema import JevConfig
from jev_agent.core.budget import (
    ESTIMATE_STATES,
    estimate_run_cost,
    estimate_verdict_for,
    format_estimate,
)
from jev_agent.core.store import (
    AttemptRecord,
    DecisionRecord,
    NotFoundError,
    QualityReportRecord,
    RunRecord,
    Store,
)

console = Console()


def _usd(value: float | None) -> str:
    return "-" if value is None else f"${value:.2f}"


def status(
    run_id: Annotated[str | None, typer.Argument(help="Show one run in detail.")] = None,
    repo: RepoOption = Path("."),
) -> None:
    """List runs, or show one run with its plan and tasks."""
    with repo_context(repo, create=False) as ctx:
        if run_id is None:
            _print_runs(ctx.store.list_runs())
            return
        try:
            _print_run(ctx.store, ctx.store.get_run(run_id), ctx.config)
        except NotFoundError as exc:
            raise fail(str(exc)) from exc


def _print_runs(runs: list[RunRecord]) -> None:
    if not runs:
        console.print('No runs yet. Start one with `jev run "idea"`.')
        return
    table = Table(title="runs")
    table.add_column("run", no_wrap=True)
    table.add_column("state", no_wrap=True, overflow="ellipsis")
    for column in ("spent", "updated", "idea"):
        table.add_column(column, overflow="fold")
    for r in runs:
        idea = r.idea.text if len(r.idea.text) <= 60 else r.idea.text[:57] + "..."
        table.add_row(
            r.id, str(r.state), _usd(r.spend_usd), format_time(r.updated_at), escape(idea)
        )
    console.print(table)


def _print_run(store: Store, run: RunRecord, config: JevConfig) -> None:
    console.print(f"[bold]run {run.id}[/bold]  {run.state}  spent {_usd(run.spend_usd)}")
    if run.state in ESTIMATE_STATES:
        estimate = estimate_run_cost(store, run, config.dispatcher.cost)
        console.print(f"estimated total: {format_estimate(estimate)}")
        pair = estimate_verdict_for(store, config.budget, config.dispatcher.cost, run)
        if pair is not None and pair[1].message is not None:
            console.print(f"[yellow]budget: {escape(pair[1].message)}[/yellow]")
    if run.reason:
        console.print(f"reason: {escape(run.reason)}")
    console.print(f"learning: {escape(learning_line(run))}")
    console.print(f"quality: {escape(quality_line(run, store.list_quality_reports(run.id)))}")
    console.print(f"idea: {escape(run.idea.text)}")
    if run.plan is not None:
        console.print(f"plan (round {run.plan_round}): {escape(run.plan.summary)}")
    if run.design is not None:
        console.print(f"design: {escape(run.design.title)}")
        if run.design.summary:
            console.print(f"  {escape(run.design.summary)}")
    if run.validation is not None:
        console.print(f"validation: {run.validation.verdict}")
        for issue in run.validation.issues:
            console.print(f"  - {escape(issue)}")
    tasks = store.list_tasks(run.id)
    if not tasks:
        return
    table = Table(title="tasks")
    for column in ("task", "title", "state", "tries", "worker", "model", "effort", "spent", "note"):
        table.add_column(column, overflow="fold")
    for t in tasks:
        note = (t.note or "").splitlines()[0] if t.note else ""
        table.add_row(
            t.id,
            escape(t.card.title),
            str(t.state),
            str(t.attempts),
            t.worker or "-",
            t.model or "-",
            t.effort or "-",
            _usd(t.spend_usd),
            escape(note),
        )
    console.print(table)


def log(run_id: RunId, repo: RepoOption = Path(".")) -> None:
    """Show the run's timeline: every decision and every worker attempt."""
    with repo_context(repo, create=False) as ctx:
        try:
            run = ctx.store.get_run(run_id)
            decisions = ctx.store.list_decisions(run_id)
            attempts = ctx.store.list_attempts(run_id)
            reports = ctx.store.list_quality_reports(run_id)
        except NotFoundError as exc:
            raise fail(str(exc)) from exc
        console.print(f"[bold]run {run.id}[/bold]  {run.state}  spent {_usd(run.spend_usd)}")
        for when, line in timeline(decisions, attempts, reports):
            console.print(f"{format_time(when)}  {line}", markup=False, highlight=False)
        if run.reason:
            console.print(f"reason: {run.reason}", markup=False)
        console.print(f"learning: {learning_line(run)}", markup=False, highlight=False)
        console.print(f"quality: {quality_line(run, reports)}", markup=False, highlight=False)


def timeline(
    decisions: list[DecisionRecord],
    attempts: list[AttemptRecord],
    reports: list[QualityReportRecord] | None = None,
) -> list[tuple[datetime, str]]:
    """Decisions, attempts and quality gates as (time, line) pairs, oldest first."""
    events = [(d.created_at, _decision_line(d)) for d in decisions]
    events += [(a.started_at, _attempt_line(a)) for a in attempts]
    events += [(r.created_at, quality_gate_line(r)) for r in reports or []]
    return sorted(events, key=lambda event: event[0])


def quality_gate_line(record: QualityReportRecord) -> str:
    """`quality [T1] #2 production: blocked (...); ruff=fail tests=pass bandit=skipped;
    score 96/85`. The score is the gate's 0-100 ship score over its threshold."""
    r = record.result
    checks = " ".join(f"{c.name}={c.status}" for c in r.checks)
    line = f"quality [{r.task_id or 'integration'}] #{r.number} {r.level}: {r.summary_line()}"
    if checks:
        line += f"; {checks}"
    if r.score is not None:
        line += f"; score {r.score.total}/{r.score.threshold}"
    if r.blocked:
        line += f"; blocked by: {'; '.join(r.reasons)}"
    return line


def _decision_line(record: DecisionRecord) -> str:
    d = record.decision
    scope = record.task_id or "run"
    answers = format_answers(d.answers, d.details)
    cached = " [cached]" if d.details.get("cache_hit") else ""
    line = (
        f"decision {d.point} [{scope}] by {d.dispatcher}{cached}"
        f"{f' ({d.model})' if d.model else ''}: {answers}"
        f"  cost {_usd(d.cost_usd)}  {d.latency_ms}ms"
    )
    if record.fallback_reason:
        line += f"  FALLBACK: {record.fallback_reason}"
    return line


def _attempt_line(a: AttemptRecord) -> str:
    scope = a.task_id or "run"
    status = a.result.status if a.result else "running"
    cost = a.result.cost_usd if a.result else None
    line = (
        f"attempt {a.role} [{scope}] #{a.attempt_no} {a.worker}"
        f" model={a.model or 'default'} effort={a.effort or 'default'}"
        f" -> {status}  cost {_usd(cost)}"
    )
    if a.lesson_ids:
        line += f"  lessons {','.join(a.lesson_ids)}"
    return line
