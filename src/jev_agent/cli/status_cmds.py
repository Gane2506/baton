"""`jev status` and `jev log`: read-only views of runs in the state database."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jev_agent.cli.repo import RepoOption
from jev_agent.cli.run_cmds import RunId, estimate_verdict_for
from jev_agent.cli.wiring import fail, repo_context
from jev_agent.config.schema import JevConfig
from jev_agent.core.budget import ESTIMATE_STATES, estimate_run_cost, format_estimate
from jev_agent.core.store import (
    AttemptRecord,
    DecisionRecord,
    NotFoundError,
    QualityReportRecord,
    RunRecord,
    Store,
)

console = Console()


def format_time(value: datetime) -> str:
    """UTC with an explicit `Z`, like run ids (which are UTC too): `2026-10-12 15:30:12Z`."""
    if value.tzinfo is None:  # stored times are UTC; never let the local zone creep in
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


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
        verdict = estimate_verdict_for(store, config, run)
        if verdict is not None and verdict.message is not None:
            console.print(f"[yellow]budget: {escape(verdict.message)}[/yellow]")
    if run.reason:
        console.print(f"reason: {escape(run.reason)}")
    console.print(f"learning: {escape(learning_line(run))}")
    console.print(f"quality: {escape(quality_line(run, store.list_quality_reports(run.id)))}")
    console.print(f"idea: {escape(run.idea.text)}")
    if run.plan is not None:
        console.print(f"plan (round {run.plan_round}): {escape(run.plan.summary)}")
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


def learning_line(run: RunRecord) -> str:
    """`profile default (learned into default: 1 added, ...)`, or `off`."""
    if run.learning_profile is None:
        return "off"
    outcome = f" ({run.learning_outcome})" if run.learning_outcome else ""
    return f"profile {run.learning_profile}{outcome}"


def quality_line(run: RunRecord, reports: list[QualityReportRecord]) -> str:
    """`production; T1 #2 passed, score 96/85, integration #1 blocked, score 72/85 (...);
    `jev quality show RUN``, `production (no gate has run yet)`, or `none`. The score is
    the gate's 0-100 ship score over the `[quality] score_threshold`."""
    if run.quality_level == "none" and not reports:
        return "none"
    latest: dict[str, QualityReportRecord] = {}
    for record in reports:
        key = record.result.task_id or "integration"
        latest.pop(key, None)
        latest[key] = record
    if not latest:
        return f"{run.quality_level} (no gate has run yet)"
    parts = []
    for key, record in latest.items():
        r = record.result
        verdict = "blocked" if r.blocked else "passed"
        counts = r.counts()
        serious = ", ".join(f"{counts[s]} {s}" for s in ("critical", "major") if counts[s])
        score = f", score {r.score.total}/{r.score.threshold}" if r.score is not None else ""
        parts.append(f"{key} #{r.number} {verdict}{score}" + (f" ({serious})" if serious else ""))
    return f"{run.quality_level}; {', '.join(parts)}; report: `jev quality show {run.id}`"


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
    line = (
        f"decision {d.point} [{scope}] by {d.dispatcher}"
        f"{f' ({d.model})' if d.model else ''}: {answers}"
        f"  cost {_usd(d.cost_usd)}  {d.latency_ms}ms"
    )
    if record.fallback_reason:
        line += f"  FALLBACK: {record.fallback_reason}"
    return line


def _number(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def format_answers(answers: dict[str, str], details: dict[str, Any]) -> str:
    """`worker=codex (0.91, Laya +0.45)`: each answer, with Jev's confidence and Laya's margin."""
    jev_votes = details.get("answers")
    laya_votes = details.get("laya")
    if not isinstance(jev_votes, dict):
        jev_votes = {}
    if not isinstance(laya_votes, dict):
        laya_votes = {}
    parts = []
    for name, value in answers.items():
        part = f"{name}={value}"
        jev = jev_votes.get(name)
        laya = laya_votes.get(name)
        confidence = _number(jev.get("confidence")) if isinstance(jev, dict) else None
        margin = _number(laya.get("margin")) if isinstance(laya, dict) else None
        notes = []
        if confidence is not None:
            notes.append(f"{confidence:.2f}")
        if margin is not None:
            notes.append(f"Laya {margin:+.2f}")
        if notes:
            part += f" ({', '.join(notes)})"
        parts.append(part)
    return " ".join(parts)


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
