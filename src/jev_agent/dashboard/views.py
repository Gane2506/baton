"""What each dashboard page shows, built from the same store and functions as the CLI.

Every function here takes an open `Repo` and returns plain data for a template; nothing
writes. The board uses `jev board`'s `load_board`, the run page `jev status` / `jev log` /
`jev quality show` helpers, the queue page `core.queue`, and so on.
"""

from __future__ import annotations

import os
import re
import sqlite3
import statistics
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jev_agent.app.board import COLUMN_FOR_STATE, COLUMNS, Card, load_board
from jev_agent.app.chat import chat_title
from jev_agent.app.context import FAKE_WORKERS_ENV, STATE_DB_FILENAME
from jev_agent.app.doctor import Check, run_checks
from jev_agent.app.errors import JevError
from jev_agent.app.format import format_answers, learning_line, quality_line
from jev_agent.app.quality import review_text
from jev_agent.app.queue import item_flags
from jev_agent.config import STATE_DIRNAME, JevConfig, load_config
from jev_agent.core.benchmarks import DEFAULT_LAST, Summary, Trend, summarize, trends
from jev_agent.core.budget import (
    CostEstimate,
    EstimateVerdict,
    estimate_verdict_for,
    format_estimate,
)
from jev_agent.core.chat import ChatMeta, list_chats, read_transcript
from jev_agent.core.harness import DEFAULT_TIMEOUT_S
from jev_agent.core.kpi import PrKpis, compute_run_kpis, load_pr_kpis
from jev_agent.core.learning import Learning, LearningStatus, Lesson, Profile, learning_status
from jev_agent.core.lease import describe_holder, live_holder
from jev_agent.core.migrations import SchemaVersionError
from jev_agent.core.models import RunState, TaskState
from jev_agent.core.pipeline import STOPPED
from jev_agent.core.queue import effective_status, queue_counts, run_states
from jev_agent.core.recurrence import describe, local_timezone_name, parse_schedule
from jev_agent.core.scheduler import LockInfo, scheduler_lock, spent_today
from jev_agent.core.ship import ShipPreview, after_approval, ship_preview
from jev_agent.core.store import (
    AttemptRecord,
    NotFoundError,
    QualityReportRecord,
    QueueItem,
    QueueStatus,
    RecurringTask,
    RunRecord,
    ShippedPrRecord,
    Store,
    TaskRecord,
)
from jev_agent.core.workspace import result_branch
from jev_agent.dashboard.render import BarChart, bar_chart, render_markdown
from jev_agent.quality.report import latest_per_gate
from jev_agent.servers.access import server_grants
from jev_agent.servers.profiles import ProfileError, read_run_profile

RUN_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
MAX_LOG_FILES = 500


@dataclass
class Repo:
    root: Path
    config: JevConfig
    store: Store
    learning: Learning

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIRNAME


@contextmanager
def open_repo(root: Path) -> Iterator[Repo]:
    """Config, store and learning profiles for one request (the same files as the CLI).

    A state database this jev cannot read (written by a newer jev, or corrupt) becomes a
    `JevError` with its message, like the CLI's own commands (issue #25), so the dashboard
    renders a readable error page instead of a bare 500.
    """
    config = load_config(root)
    try:
        with Store.open(root / STATE_DIRNAME / STATE_DB_FILENAME) as store:
            learning = Learning.for_store(store)
            try:
                yield Repo(root, config, store, learning)
            finally:
                learning.close()
    except SchemaVersionError as exc:
        raise JevError(str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        raise JevError(f"cannot read the state database: {exc}") from exc


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID.match(run_id))


# ---- board -------------------------------------------------------------------------------


@dataclass(frozen=True)
class BoardCard:
    card: Card
    run_id: str | None  # None for queue items
    quality: str  # short gate status ("basic: passed"), or ""
    item_id: int | None = None  # the queue item's id, for the board's Remove button


def quality_badge(run: RunRecord, reports: list[QualityReportRecord]) -> str:
    """`medium: passed`, `medium: blocked`, `medium: pending`, or `` without a gate."""
    if run.quality_level == "none" and not reports:
        return ""
    if not reports:
        return f"{run.quality_level}: pending"
    latest: dict[str, QualityReportRecord] = {}
    for record in reports:
        latest[record.result.task_id or "integration"] = record
    blocked = any(r.result.blocked for r in latest.values())
    level = run.quality_level if run.quality_level != "none" else reports[-1].result.level
    return f"{level}: {'blocked' if blocked else 'passed'}"


def board(repo: Repo, show_all: bool = False) -> list[tuple[str, list[BoardCard]]]:
    """`jev board`'s columns, with each run card's quality status; queue cards carry their
    item id so the board can remove them."""
    columns = load_board(repo.store, datetime.now(UTC), show_all=show_all, title_width=90)
    runs = {r.id: r for r in repo.store.list_runs()}
    queue_ids = {f"queue #{item.id}": item.id for item in repo.store.list_queue()}
    out: list[tuple[str, list[BoardCard]]] = []
    for name in COLUMNS:
        cards: list[BoardCard] = []
        for card in columns[name]:
            run = runs.get(card.ref)
            if run is None:
                cards.append(BoardCard(card, None, "", queue_ids.get(card.ref)))
                continue
            reports = repo.store.list_quality_reports(run.id)
            cards.append(BoardCard(card, run.id, quality_badge(run, reports)))
        out.append((name, cards))
    return out


# ---- run detail --------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionRow:
    at: datetime
    point: str
    scope: str
    dispatcher: str
    model: str | None
    answers: str  # with Jev's confidences
    fallback_reason: str | None
    cost_usd: float | None
    latency_ms: int


@dataclass(frozen=True)
class AttemptRow:
    record: AttemptRecord
    status: str
    cost_usd: float | None
    summary: str
    log: str | None  # path relative to the run's directory, for the file link


@dataclass(frozen=True)
class LogFile:
    path: str  # relative to `.jev/runs/<run_id>/`, with `/`
    size: int


@dataclass
class RunView:
    run: RunRecord
    tasks: list[TaskRecord]
    decisions: list[DecisionRow]
    attempts: list[AttemptRow]
    learning: str
    quality: str
    review_html: Any  # Markup, or None when no gate has run
    branch: str | None
    files: list[LogFile]
    can_approve: bool
    can_resume: bool
    can_cancel: bool
    column: str
    # Why another jev process may be running this run (issue #15); "" when nothing suggests
    # it. While set, Resume needs an explicit force.
    resume_risk: str = ""
    reports: list[QualityReportRecord] = field(default_factory=list)
    # `jev ship`: PRs opened for this run, and whether the "Ship as PR" button shows.
    shipped: list[ShippedPrRecord] = field(default_factory=list)
    ship_enabled: bool = False
    can_ship: bool = False
    ship_note: str = ""  # why it cannot be shipped (yet), when ship is enabled
    # AWAITING_SHIP: the PR it would open, the D6 votes and the diff, for approve/modify/stop.
    ship_preview: ShipPreview | None = None
    ship_vote: str = ""  # the D6 answers with Jev's confidence and Laya's margin
    # AWAITING_APPROVAL: the pre-approval cost estimate and its budget verdict, shown on
    # the approval card and enforced when approving (the same gate as `jev approve`).
    estimate: CostEstimate | None = None
    estimate_verdict: EstimateVerdict | None = None
    estimate_line: str = ""  # "Estimated cost: $1.20-$3.40 (basis) of $5.00 run budget"
    approve_confirm: str = "Approve this plan and start executing it?"


def run_dir(repo: Repo, run_id: str) -> Path:
    return repo.state_dir / "runs" / run_id


def log_files(base: Path) -> list[LogFile]:
    """Every regular file under the run's directory (logs, quality reports), sorted."""
    if not base.is_dir():
        return []
    files: list[LogFile] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            files.append(LogFile(path.relative_to(base).as_posix(), path.stat().st_size))
            if len(files) >= MAX_LOG_FILES:
                return files
    return files


def can_resume(run: RunRecord) -> bool:
    """What `Pipeline.resume` accepts: an unfinished run, or NEEDS_HUMAN with a resume point."""
    if run.state in (RunState.AWAITING_APPROVAL, RunState.AWAITING_SHIP):
        return False
    if run.state is RunState.NEEDS_HUMAN:
        return run.resume_state is not None
    return run.state not in STOPPED


# How long past its worker's timeout an attempt without a result still counts as running: the
# harness kills the worker at the timeout, then records the result.
RESUME_GRACE_S = 300


def resume_risk(repo: Repo, run: RunRecord, now: datetime | None = None) -> str:
    """Why another jev process (a terminal `jev run`/`jev resume`, the scheduler) may be
    executing `run` right now, or "" when nothing suggests it. Resuming such a run starts a
    second pipeline on the same worktrees, so the dashboard asks for an explicit force.

    Signals: a live run lease (`core/lease.py`; resuming is then refused anyway, also when
    forced), an attempt without a result that started less than its worker's timeout plus
    `RESUME_GRACE_S` ago (an older one is from a process that died), and a live scheduler lock
    whose current queue item is this run's.
    """
    if run.state in STOPPED or run.state in (RunState.AWAITING_APPROVAL, RunState.AWAITING_SHIP):
        return ""
    now = now or datetime.now(UTC)
    lease = live_holder(repo.store, run.id, now)
    if lease is not None:
        return f"another jev process, {describe_holder(lease)}, is driving this run"
    for attempt in repo.store.list_attempts(run.id):
        if attempt.result is not None:
            continue
        settings = getattr(repo.config.workers, attempt.worker, None)
        timeout = getattr(settings, "timeout_s", 0) or DEFAULT_TIMEOUT_S
        age = (now - attempt.started_at).total_seconds()
        if age < timeout + RESUME_GRACE_S:
            where = f"task {attempt.task_id}" if attempt.task_id else "the run"
            return (
                f"{attempt.role} attempt for {where} on {attempt.worker} started "
                f"{max(int(age), 0)} s ago and has no result yet "
                f"(the worker timeout is {timeout} s)"
            )
    holder = scheduler_lock(repo.root).read()
    if holder is not None and holder.current_item is not None:
        try:
            item = repo.store.get_queue_item(holder.current_item)
        except NotFoundError:
            return ""
        claimed = item.updated_at.replace(microsecond=0)
        if item.status == "running" and (
            item.run_id == run.id
            or (item.run_id is None and item.idea == run.idea.text and run.created_at >= claimed)
        ):
            return (
                f"the scheduler (pid {holder.pid} on {holder.host}) is working on "
                f"queue item #{item.id}, which started this run"
            )
    return ""


def run_view(repo: Repo, run_id: str) -> RunView:
    """Everything `jev status RUN`, `jev log RUN` and `jev quality show RUN` print."""
    store = repo.store
    run = store.get_run(run_id)  # NotFoundError for an unknown run
    reports = store.list_quality_reports(run_id)
    base = run_dir(repo, run_id)
    decisions = [
        DecisionRow(
            d.created_at,
            d.decision.point,
            d.task_id or "run",
            d.decision.dispatcher,
            d.decision.model,
            format_answers(d.decision.answers, d.decision.details),
            d.fallback_reason,
            d.decision.cost_usd,
            d.decision.latency_ms,
        )
        for d in store.list_decisions(run_id)
    ]
    attempts = []
    for a in store.list_attempts(run_id):
        log = None
        if a.log_path is not None:
            try:
                log = a.log_path.resolve().relative_to(base.resolve()).as_posix()
            except ValueError:
                log = None
        attempts.append(
            AttemptRow(
                a,
                a.result.status if a.result else "running",
                a.result.cost_usd if a.result else None,
                a.result.summary if a.result else "",
                log,
            )
        )
    integrated = run.state in (RunState.INTEGRATING, RunState.DONE) or any(
        d.point == "D6" for d in decisions
    )
    pair = estimate_verdict_for(store, repo.config.budget, repo.config.dispatcher.cost, run)
    estimate, verdict = pair if pair is not None else (None, None)
    if estimate is not None:
        cap = repo.config.budget.per_run_usd
        estimate_line = f"Estimated cost: {format_estimate(estimate)} of ${cap:.2f} run budget"
    else:
        estimate_line = ""
    if verdict is not None and verdict.level == "warn" and verdict.message:
        approve_confirm = f"{verdict.message} Approve this plan and start executing it?"
    else:
        approve_confirm = "Approve this plan and start executing it?"
    return RunView(
        run=run,
        tasks=store.list_tasks(run_id),
        decisions=decisions,
        attempts=attempts,
        learning=learning_line(run),
        quality=quality_line(run, reports),
        review_html=render_markdown(review_text(repo.root, run, reports)) if reports else None,
        branch=result_branch(run_id) if integrated else None,
        files=log_files(base),
        can_approve=run.state is RunState.AWAITING_APPROVAL,
        can_resume=can_resume(run),
        resume_risk=resume_risk(repo, run),
        can_cancel=run.state not in STOPPED,
        column=COLUMN_FOR_STATE[run.state],
        reports=reports,
        estimate=estimate,
        estimate_verdict=verdict,
        estimate_line=estimate_line,
        approve_confirm=approve_confirm,
        **ship_state(repo, run, reports),
    )


def ship_state(repo: Repo, run: RunRecord, reports: list[QualityReportRecord]) -> dict[str, Any]:
    """The run page's ship fields. The button shows for a DONE, not yet shipped run whose
    quality gate did not block; `jev ship` itself checks everything again (remote, gh, ...)."""
    shipped = repo.store.list_shipped_prs(run.id)
    settings = repo.config.ship
    note = ""
    if not settings.enabled:
        note = "shipping is off (`[ship] enabled = false`)"
    elif run.state is not RunState.DONE:
        note = "only a DONE run can be shipped"
    elif shipped:
        note = "already shipped; `jev ship RUN --force-new` opens another PR"
    elif settings.require_quality_pass and any(
        g.blocked for g in latest_per_gate([r.result for r in reports])
    ):
        note = "the quality gate blocked this run"
    preview = None
    vote = ""
    if run.state is RunState.AWAITING_SHIP:
        preview = ship_preview(repo.root, repo.store, run.id)
        if preview.vote is not None:
            d = preview.vote.decision
            vote = f"{d.dispatcher}: {format_answers(d.answers, d.details)}"
    return {
        "shipped": shipped,
        "ship_enabled": settings.enabled,
        "can_ship": settings.enabled and not note,
        "ship_note": note,
        "ship_preview": preview,
        "ship_vote": vote,
    }


def safe_run_file(repo: Repo, run_id: str, relative: str) -> Path | None:
    """The file `relative` under `.jev/runs/<run_id>/`, or None if it is not a regular file
    strictly inside that directory (`..`, absolute paths and symlinks out are refused)."""
    if not valid_run_id(run_id) or not relative or "\x00" in relative:
        return None
    base = run_dir(repo, run_id).resolve()
    if not base.is_dir():
        return None
    candidate = (base / relative).resolve()
    if candidate == base or not candidate.is_relative_to(base) or not candidate.is_file():
        return None
    return candidate


# ---- queue -------------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueRow:
    item: QueueItem
    status: QueueStatus
    flags: str
    movable: bool


@dataclass(frozen=True)
class RecurringRow:
    task: RecurringTask
    schedule_plain: str  # describe(parse_schedule(task.schedule_source)), in plain words
    next_due_local: datetime  # next_due_at in the viewer's local timezone
    last_run_local: datetime | None  # last_run_at in the viewer's local timezone, if any


@dataclass
class QueueView:
    rows: list[QueueRow]
    counts: dict[QueueStatus, int]
    scheduler: LockInfo | None
    spent_today: float
    profiles: list[Profile]
    hidden: int  # finished items not shown without show_all
    recurring: list[RecurringRow]  # recurring tasks, next-due first


def recurring_rows(repo: Repo) -> list[RecurringRow]:
    """The recurring tasks (`jev queue add --repeat`), next-due first, with each schedule in
    plain words and each run time in the viewer's local timezone."""
    local = ZoneInfo(local_timezone_name())
    return [
        RecurringRow(
            task=task,
            schedule_plain=describe(parse_schedule(task.schedule_source)),
            next_due_local=task.next_due_at.astimezone(local),
            last_run_local=(
                task.last_run_at.astimezone(local) if task.last_run_at is not None else None
            ),
        )
        for task in sorted(repo.store.list_recurring_tasks(), key=lambda t: t.next_due_at)
    ]


def queue_view(repo: Repo, show_all: bool = False) -> QueueView:
    """`jev queue list` (with effective statuses) plus `jev scheduler status`."""
    items = repo.store.list_queue()
    states = run_states(repo.store, items)
    rows = [
        QueueRow(
            item,
            effective_status(item, states.get(item.run_id or "")),
            item_flags(item),
            item.status == "queued",
        )
        for item in items
    ]
    shown = rows if show_all else [r for r in rows if r.status in ("queued", "running", "waiting")]
    return QueueView(
        rows=shown,
        counts=queue_counts(repo.store),
        scheduler=scheduler_lock(repo.root).read(),
        spent_today=spent_today(repo.store, datetime.now(UTC)),
        profiles=repo.learning.profiles(),
        hidden=len(rows) - len(shown),
        recurring=recurring_rows(repo),
    )


# ---- learning ----------------------------------------------------------------------------


@dataclass
class LearningView:
    status: LearningStatus
    profiles: list[Profile]
    selected: str | None
    lessons: list[Lesson]
    shared_path: Path


def learning_view(repo: Repo, profile: str | None, include_retired: bool) -> LearningView:
    """`jev learning list` and `jev learning show PROFILE [--all]`."""
    status = learning_status(repo.store, repo.config)
    profiles = repo.learning.profiles()
    names = [p.name for p in profiles]
    selected = (
        profile if profile in names else (status.profile if status.profile in names else None)
    )
    lessons = repo.learning.lessons(selected, include_retired) if selected else []
    return LearningView(status, profiles, selected, lessons, repo.learning.shared_path)


# ---- chats -------------------------------------------------------------------------------


def chats(repo: Repo, query: str = "") -> list[tuple[ChatMeta, str]]:
    """Saved chats, newest first, titled by their brief or their opening message. `query`
    filters on title, chat id, backend and model, case-insensitively."""
    rows = [(meta, chat_title(meta, repo.root)) for meta in list_chats(repo.root)]
    needle = query.strip().lower()
    if not needle:
        return rows
    return [
        (meta, title)
        for meta, title in rows
        if needle in title.lower()
        or needle in meta.chat_id.lower()
        or needle in meta.backend.lower()
        or needle in (meta.model or "").lower()
    ]


# ---- repo KPIs ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LatestRun:
    run: RunRecord
    tasks_done: int
    tasks: int
    branch: str
    quality: str


@dataclass(frozen=True)
class RepoKpis:
    """The repo's own KPIs from every run (the KPIs page's "This repo" section)."""

    total: int
    done: int
    unfinished: int  # runs that ended without reaching DONE (not a RunState)
    by_state: list[tuple[str, int]]
    median_cost_usd: float | None
    median_wall_clock_h: float | None
    median_first_pass: float | None
    fallback_rate: float | None
    quality_pass_rate: float | None
    latest: LatestRun | None


def product_kpis(repo: Repo) -> RepoKpis:
    """Aggregates over all of the repo's runs; the PR KPIs (`jev kpi`) are a separate view."""
    runs = repo.store.list_runs()
    done = [r for r in runs if r.state is RunState.DONE]
    unfinished_states = {
        RunState.NEEDS_HUMAN,
        RunState.FAILED,
        RunState.BUDGET_EXCEEDED,
        RunState.CANCELLED,
    }

    def median(values: list[float]) -> float | None:
        return float(statistics.median(values)) if values else None

    first_pass: list[float] = []
    for run in done:
        value = compute_run_kpis(repo.store, run.id).first_pass_rate
        if value is not None:
            first_pass.append(float(value))
    asked = fallbacks = 0
    for run in runs:
        for decision in repo.store.list_decisions(run.id):
            asked += 1
            fallbacks += 1 if decision.fallback_reason else 0
    quality_runs = [r for r in runs if r.quality_level != "none"]
    quality_ok = sum(
        1
        for r in quality_runs
        if quality_badge(r, repo.store.list_quality_reports(r.id)).endswith("passed")
    )
    latest = None
    if done:
        newest = max(done, key=lambda r: r.updated_at)
        tasks = repo.store.list_tasks(newest.id)
        latest = LatestRun(
            newest,
            sum(1 for t in tasks if t.state is TaskState.DONE),
            len(tasks),
            result_branch(newest.id),
            quality_badge(newest, repo.store.list_quality_reports(newest.id)),
        )
    counts = Counter(str(r.state) for r in runs)
    return RepoKpis(
        total=len(runs),
        done=len(done),
        unfinished=sum(1 for r in runs if r.state in unfinished_states),
        by_state=sorted(counts.items(), key=lambda item: (-item[1], item[0])),
        median_cost_usd=median([r.spend_usd for r in runs]),
        median_wall_clock_h=median(
            [(r.updated_at - r.created_at).total_seconds() / 3600 for r in runs]
        ),
        median_first_pass=median(first_pass),
        fallback_rate=(fallbacks / asked) if asked else None,
        quality_pass_rate=(quality_ok / len(quality_runs)) if quality_runs else None,
        latest=latest,
    )


def chat_transcript(repo: Repo, chat_id: str) -> tuple[ChatMeta | None, list[dict[str, Any]]]:
    metas = {meta.chat_id: meta for meta in list_chats(repo.root)}
    entries = read_transcript(repo.root, chat_id)
    for entry in entries:
        # Assistant messages render as Markdown (code blocks, bold, lists); user input stays
        # plain text, like ChatGPT. The template falls back to escaped `text` when
        # `body_html` is unset.
        if entry.get("kind") == "assistant":
            text = entry.get("text")
            if isinstance(text, str):
                entry["body_html"] = render_markdown(text)
    return metas.get(chat_id), entries


# ---- spend -------------------------------------------------------------------------------


def spend_by_day(
    runs: list[RunRecord], days: int = 30, today: date | None = None
) -> list[tuple[date, float]]:
    """Spend of runs created on each of the last `days` days (UTC), oldest first."""
    end = today or datetime.now(UTC).date()
    totals: dict[date, float] = {end - timedelta(days=i): 0.0 for i in range(days)}
    for run in runs:
        day = run.created_at.astimezone(UTC).date()
        if day in totals:
            totals[day] += run.spend_usd
    return sorted(totals.items())


@dataclass
class SpendView:
    runs: list[RunRecord]
    total: float
    by_run: BarChart
    by_day: BarChart
    days: int


def spend_view(repo: Repo, days: int = 30, top_runs: int = 25) -> SpendView:
    runs = repo.store.list_runs()
    costly = sorted(runs, key=lambda r: r.spend_usd, reverse=True)[:top_runs]
    # Long labels are cut with an ellipsis by bar_chart; the full one is the row's tooltip.
    by_run = bar_chart(
        [(f"{r.id} {r.idea.text}", r.spend_usd, f"/runs/{r.id}") for r in costly],
        label_width=330,
    )
    by_day = bar_chart(
        [(day.isoformat(), usd, None) for day, usd in spend_by_day(runs, days)],
        label_width=100,
        row=18,
    )
    return SpendView(runs, sum(r.spend_usd for r in runs), by_run, by_day, days)


# ---- KPIs --------------------------------------------------------------------------------


@dataclass
class KpiView:
    rows: list[PrKpis]  # newest first, for the table
    summary: Summary
    trends: list[Trend]
    last: int
    ship_enabled: bool

    @property
    def regressions(self) -> list[Trend]:
        return [t for t in self.trends if t.regressed]


def kpi_view(repo: Repo, last: int = DEFAULT_LAST) -> KpiView:
    """`jev kpi --last N`: the shipped PRs with their KPIs, medians and trends."""
    rows = load_pr_kpis(repo.store, last)
    return KpiView(
        rows=list(reversed(rows)),
        summary=summarize(rows),
        trends=trends(rows, last),
        last=last,
        ship_enabled=repo.config.ship.enabled,
    )


# ---- settings ----------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvVar:
    name: str
    used_for: str
    is_set: bool  # never the value


def env_vars(config: JevConfig) -> list[EnvVar]:
    """Environment variables the config names, and whether each is set. Never values."""
    wanted: list[tuple[str, str]] = [
        (config.dispatcher.jev.api_key_env, "[dispatcher.jev] api_key_env"),
        (config.workers.api.api_key_env, "[workers.api] api_key_env"),
    ]
    for name, server in config.tools.mcp.items():
        wanted += [(var, f"[tools.mcp.{name}] env_from") for var in server.env_from]
        if server.bearer_token_env:
            wanted.append((server.bearer_token_env, f"[tools.mcp.{name}] bearer_token_env"))
        wanted += [
            (var, f"[tools.mcp.{name}] headers_from_env")
            for var in server.headers_from_env.values()
        ]
    wanted.append((FAKE_WORKERS_ENV, "test hook: fake workers"))
    seen: dict[str, list[str]] = {}
    for name, used in wanted:
        seen.setdefault(name, []).append(used)
    return [
        EnvVar(name, ", ".join(uses), bool(os.environ.get(name, "").strip()))
        for name, uses in seen.items()
    ]


def config_summary(repo: Repo) -> list[tuple[str, str]]:
    """The effective settings that matter most, as (key, value) rows. No secrets: the config
    cannot hold any (keys named like secrets are rejected), and env vars are listed by name."""
    c = repo.config
    status = learning_status(repo.store, c)
    rows = [
        ("dispatcher.primary", c.dispatcher.primary),
        ("dispatcher.jev.model", c.dispatcher.jev.model),
        ("dispatcher.jev.base_url", c.dispatcher.jev.base_url),
        ("budget.per_run_usd", f"{c.budget.per_run_usd:g}"),
        ("budget.per_task_usd", f"{c.budget.per_task_usd:g}"),
        ("budget.max_task_attempts", str(c.budget.max_task_attempts)),
        ("approval.require_plan_approval", str(c.plan_approval_required).lower()),
        ("ship.after_approval", after_approval(c, None) or "off ([ship] enabled = false)"),
        ("integration.test_command", c.integration.test_command or "-"),
        ("review.enabled", str(c.review_enabled).lower()),
        ("quality.level", c.quality.level),
        ("learning", f"{'on' if status.enabled else 'off'} ({status.enabled_source})"),
        ("learning.profile", f"{status.profile} ({status.profile_source})"),
        ("models.prefer_free", str(c.models.prefer_free).lower()),
    ]
    for name in c.workers.names():
        worker = getattr(c.workers, name)
        detail = "enabled" if worker.enabled else "disabled"
        command = getattr(worker, "command", None) or getattr(worker, "base_url", "")
        rows.append((f"workers.{name}", f"{detail}; {command}"))
    for role, settings in c.roles.items():
        models = ", ".join(settings.models) or "default"
        rows.append((f"roles.{role}", f"workers {', '.join(settings.workers)}; models {models}"))
    if os.environ.get(FAKE_WORKERS_ENV) == "1":
        rows.append(("workers (test hook)", f"{FAKE_WORKERS_ENV}=1: every worker is a fake"))
    return rows


def doctor_checks(root: Path) -> list[Check]:
    """`jev doctor` without `--connect`: local checks only."""
    return run_checks(root)


# ---- fleet ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class FleetAgent:
    """One agent currently working: an attempt with no result on a run that has not stopped."""

    run_id: str
    run_idea: str
    run_state: RunState
    task_id: str | None
    task_title: str | None
    task_state: TaskState | None
    role: str  # the agent kind (planner, coder, ...); never a credential
    worker: str
    model: str | None
    started_at: datetime
    stale: bool  # older than its worker's timeout plus RESUME_GRACE_S: likely a dead process
    # (server name, access tier) this role is granted in the run; never hosts or key values.
    grants: list[tuple[str, str]]


def fleet(repo: Repo, now: datetime | None = None) -> list[FleetAgent]:
    """Every active agent across all runs and servers, newest first.

    An attempt with no result means its worker is still running it — unless the run has
    stopped (then the attempt is a leftover of a dead pipeline and is skipped), or the
    attempt outlived its worker's timeout plus `RESUME_GRACE_S` (the harness kills the
    worker at the timeout and then records the result, so it is flagged `stale` instead of
    hidden — the same rule `resume_risk` uses).

    The attempt records no server; the "server" column is the role's server grants in the
    run (`servers/access.py::server_grants`): an agent can only act on the servers its role
    was granted, each at its tier. When the run's workspace profile is unreadable, the
    role's configured servers are used (ProfileError falls back rather than failing the
    whole page). Nothing secret is exposed: server names and tier names only.
    """
    now = now or datetime.now(UTC)
    agents: list[FleetAgent] = []
    for run in repo.store.list_runs():
        if run.state in STOPPED:
            continue
        tasks = {t.id: t for t in repo.store.list_tasks(run.id)}
        try:
            profile = read_run_profile(repo.state_dir, run.id)
        except ProfileError:
            profile = None
        for attempt in repo.store.list_attempts(run.id):
            if attempt.result is not None:
                continue
            task = tasks.get(attempt.task_id) if attempt.task_id else None
            settings = getattr(repo.config.workers, attempt.worker, None)
            timeout = getattr(settings, "timeout_s", 0) or DEFAULT_TIMEOUT_S
            age = (now - attempt.started_at).total_seconds()
            agents.append(
                FleetAgent(
                    run_id=run.id,
                    run_idea=run.idea.text,
                    run_state=run.state,
                    task_id=attempt.task_id,
                    task_title=task.card.title if task is not None else None,
                    task_state=task.state if task is not None else None,
                    role=attempt.role,
                    worker=attempt.worker,
                    model=attempt.model,
                    started_at=attempt.started_at,
                    stale=age > timeout + RESUME_GRACE_S,
                    grants=sorted(server_grants(repo.config, attempt.role, profile).items()),
                )
            )
    return sorted(agents, key=lambda a: a.started_at, reverse=True)
