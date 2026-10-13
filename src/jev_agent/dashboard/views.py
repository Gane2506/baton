"""What each dashboard page shows, built from the same store and functions as the CLI.

Every function here takes an open `Repo` and returns plain data for a template; nothing
writes. The board uses `jev board`'s `load_board`, the run page `jev status` / `jev log` /
`jev quality show` helpers, the queue page `core.queue`, and so on.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from jev_agent.cli.board_cmds import COLUMN_FOR_STATE, COLUMNS, Card, load_board
from jev_agent.cli.doctor_cmd import Check, run_checks
from jev_agent.cli.quality_cmds import review_text
from jev_agent.cli.queue_cmds import item_flags
from jev_agent.cli.status_cmds import format_answers, learning_line, quality_line
from jev_agent.cli.text import short
from jev_agent.cli.wiring import FAKE_WORKERS_ENV, STATE_DB_FILENAME
from jev_agent.config import STATE_DIRNAME, JevConfig, load_config
from jev_agent.core.benchmarks import DEFAULT_LAST, Summary, Trend, summarize, trends
from jev_agent.core.chat import ChatMeta, list_chats, read_transcript
from jev_agent.core.harness import DEFAULT_TIMEOUT_S
from jev_agent.core.kpi import PrKpis, load_pr_kpis
from jev_agent.core.learning import Learning, LearningStatus, Lesson, Profile, learning_status
from jev_agent.core.models import RunState
from jev_agent.core.pipeline import STOPPED
from jev_agent.core.queue import effective_status, queue_counts, run_states
from jev_agent.core.scheduler import LockInfo, scheduler_lock, spent_today
from jev_agent.core.store import (
    AttemptRecord,
    NotFoundError,
    QualityReportRecord,
    QueueItem,
    QueueStatus,
    RunRecord,
    ShippedPrRecord,
    Store,
    TaskRecord,
)
from jev_agent.core.workspace import result_branch
from jev_agent.dashboard.render import BarChart, bar_chart, render_markdown
from jev_agent.quality.report import latest_per_gate

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
    """Config, store and learning profiles for one request (the same files as the CLI)."""
    config = load_config(root)
    with Store.open(root / STATE_DIRNAME / STATE_DB_FILENAME) as store:
        learning = Learning.for_store(store)
        try:
            yield Repo(root, config, store, learning)
        finally:
            learning.close()


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID.match(run_id))


# ---- board -------------------------------------------------------------------------------


@dataclass(frozen=True)
class BoardCard:
    card: Card
    run_id: str | None  # None for queue items
    quality: str  # short gate status ("basic: passed"), or ""


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
    """`jev board`'s columns, with each run card's quality status."""
    columns = load_board(repo.store, datetime.now(UTC), show_all=show_all, title_width=90)
    runs = {r.id: r for r in repo.store.list_runs()}
    out: list[tuple[str, list[BoardCard]]] = []
    for name in COLUMNS:
        cards: list[BoardCard] = []
        for card in columns[name]:
            run = runs.get(card.ref)
            if run is None:
                cards.append(BoardCard(card, None, ""))
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
    if run.state is RunState.AWAITING_APPROVAL:
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

    Signals (no run lease exists yet): an attempt without a result that started less than its
    worker's timeout plus `RESUME_GRACE_S` ago (an older one is from a process that died), and
    a live scheduler lock whose current queue item is this run's.
    """
    if run.state in STOPPED or run.state is RunState.AWAITING_APPROVAL:
        return ""
    now = now or datetime.now(UTC)
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
    return {
        "shipped": shipped,
        "ship_enabled": settings.enabled,
        "can_ship": settings.enabled and not note,
        "ship_note": note,
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


@dataclass
class QueueView:
    rows: list[QueueRow]
    counts: dict[QueueStatus, int]
    scheduler: LockInfo | None
    spent_today: float
    profiles: list[Profile]
    hidden: int  # finished items not shown without show_all


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


def chats(repo: Repo) -> list[tuple[ChatMeta, str]]:
    """Saved chats, newest first, with a short title from the brief."""
    return [
        (meta, short(meta.brief or "", 70) if meta.brief else "") for meta in list_chats(repo.root)
    ]


def chat_transcript(repo: Repo, chat_id: str) -> tuple[ChatMeta | None, list[dict[str, Any]]]:
    metas = {meta.chat_id: meta for meta in list_chats(repo.root)}
    return metas.get(chat_id), read_transcript(repo.root, chat_id)


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
        ("approval.require_plan_approval", str(c.approval.require_plan_approval).lower()),
        ("integration.test_command", c.integration.test_command or "-"),
        ("review.enabled", str(c.review.enabled).lower()),
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
