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
from collections.abc import Collection, Iterator
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
from jev_agent.app.models import ModelListing
from jev_agent.app.quality import CheckRow, check_rows, review_text
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
from jev_agent.core.learning import (
    EdgeView,
    GraphStats,
    Learning,
    LearningError,
    LearningStatus,
    Lesson,
    Profile,
    learning_status,
    normalize_module_ref,
    split_module_ref,
)
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
from jev_agent.core.tuning import MIN_OUTCOMES, Suggestion, collect_samples, suggest
from jev_agent.core.workspace import result_branch
from jev_agent.dashboard.render import render_markdown
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
    cached: bool = False  # served from the decision cache (issue #10)


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
            cached=bool(d.decision.details.get("cache_hit")),
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


@dataclass
class LayaView:
    """`jev laya tune` for one profile: the suggestion and what it is based on."""

    profiles: list[str]  # the profile selector's options
    profile: str | None  # the profile the suggestion is for (None: no profiles exist)
    source: str  # where the current weights come from: jev.toml, defaults, profile NAME
    configured: bool  # jev.toml pins the weights: applying is disabled
    tuned: bool  # the profile has tuned weights: resetting is offered
    passed: int
    failed: int
    suggestion: Suggestion | None  # None when there is nothing to suggest
    reason: str  # why there is no suggestion


def laya_view(repo: Repo, name: str | None) -> LayaView:
    """`jev laya tune` without `--apply`: the weight suggestion for `name` (default: the
    active profile), with per-criterion evidence for the review table."""
    profiles = [p.name for p in repo.learning.profiles()]
    status = learning_status(repo.store, repo.config)
    target = name if name in profiles else (status.profile if status.profile in profiles else None)
    if target is None:
        return LayaView(profiles, None, "defaults", False, False, 0, 0, None, "no profiles yet")
    configured = "weights" in repo.config.dispatcher.laya.model_fields_set
    tuned_weights = None if configured else repo.learning.laya_weights(target)
    current = repo.config.dispatcher.laya.weights if tuned_weights is None else tuned_weights
    source = "jev.toml" if configured else ("defaults" if tuned_weights is None else target)
    result = suggest(collect_samples(repo.store), current, MIN_OUTCOMES)
    return LayaView(
        profiles=profiles,
        profile=target,
        source=source,
        configured=configured,
        tuned=tuned_weights is not None,
        passed=result.passed,
        failed=result.failed,
        suggestion=result if result.weights is not None else None,
        reason=result.reason or "",
    )


def apply_laya_selection(
    current: dict[str, float], suggested: dict[str, float], accepted: Collection[str]
) -> dict[str, float]:
    """Merge the accepted suggested weights into the current ones, renormalized to sum
    to 1 (the same rounding as `core.tuning.suggest`)."""
    raw = {
        criterion: (suggested[criterion] if criterion in accepted else weight)
        for criterion, weight in current.items()
    }
    total = sum(raw.values())
    weights = {criterion: round(weight / total, 3) for criterion, weight in raw.items()}
    largest = max(weights, key=lambda criterion: weights[criterion])
    weights[largest] = round(weights[largest] + 1 - sum(weights.values()), 3)
    return weights


# ---- the lesson graph (the Graph tab of the learning page) ------------------------------


@dataclass(frozen=True)
class GraphLessonRow:
    """One lesson row in the Graph tab: the lesson, its relation counts, and its modules."""

    lesson: Lesson
    relations: dict[str, int]  # "contradicts", "supersedes", "superseded-by", "applies-to"
    modules: list[str]


@dataclass(frozen=True)
class GraphNode:
    kind: str  # "focus" | "lesson" | "run" | "module"
    label: str
    sub: str  # edge label from the focus node
    x: float
    y: float


@dataclass(frozen=True)
class GraphLink:
    x1: float
    y1: float
    x2: float
    y2: float
    color: str
    label: str


@dataclass(frozen=True)
class LearningGraph:
    """The Graph tab: faceted browser rows, module/relation facets, stats, and the focused
    lesson's radial neighborhood (pre-positioned SVG nodes, no JS graph library)."""

    profile: str
    stats: GraphStats
    q: str
    module: str
    relation: str
    modules: list[tuple[str, int]]  # facet chips: (path, applies-to count)
    rows: list[GraphLessonRow]
    focus: Lesson | None
    focus_edges: list[EdgeView]
    focus_co_occurs: list[tuple[str, int]]
    nodes: list[GraphNode]
    links: list[GraphLink]


RELATION_FACETS = (
    ("contradictions", "has contradictions"),
    ("supersedes", "supersedes another"),
    ("superseded", "superseded by another"),
    ("failed-runs", "from failed runs"),
)

_EDGE_COLORS = {
    "supersedes": "#2f9e44",
    "contradicts": "#d64545",
    "applies-to": "#3b82f6",
    "derived-from": "#8a8f98",
    "co-occurs-with": "#8b5cf6",
}


def _failed_runs(store: Store) -> set[str]:
    return {
        run.id
        for run in store.list_runs()
        if run.state is RunState.FAILED and run.learning_profile is not None
    }


def learning_graph_view(
    repo: Repo,
    profile: str,
    *,
    q: str = "",
    module: str = "",
    relation: str = "",
    focus_id: str = "",
) -> LearningGraph | None:
    """The Graph tab's data. None when the profile is unknown or its database has no graph
    tables yet (repo databases before their graph migration)."""
    try:
        db = repo.learning.db_for(profile)
    except LearningError:
        return None
    if not db.graph_ready():
        return None
    stats = db.graph_stats(profile)
    lessons = db.list_lessons(profile)
    if q.strip():
        try:
            wanted = {lesson.id for lesson in db.graph_query(profile, q.strip(), limit=500)}
            lessons = [lesson for lesson in lessons if lesson.id in wanted]
        except LearningError:
            pass  # not a searchable query: fall back to the unfiltered list
    neighborhoods = {
        lesson.number: db.edges_for_lesson(profile, lesson.number) for lesson in lessons
    }
    if module:
        lessons = [
            lesson
            for lesson in lessons
            if any(
                v.edge == "applies-to" and _module_matches(v.other, module)
                for v in neighborhoods[lesson.number]
            )
        ]
    failed = _failed_runs(repo.store) if relation == "failed-runs" else set()
    if relation == "failed-runs":
        lessons = [lesson for lesson in lessons if lesson.source_run in failed]
    elif relation in ("contradictions", "supersedes", "superseded"):
        want = {
            "contradictions": ("contradicts", None),
            "supersedes": ("supersedes", "out"),
            "superseded": ("supersedes", "in"),
        }[relation]
        lessons = [
            lesson
            for lesson in lessons
            if any(
                v.edge == want[0] and (want[1] is None or v.direction == want[1])
                for v in neighborhoods[lesson.number]
            )
        ]
    rows = []
    for lesson in lessons:
        counts = {"contradicts": 0, "supersedes": 0, "superseded-by": 0, "applies-to": 0}
        modules: list[str] = []
        for view in neighborhoods[lesson.number]:
            if view.edge == "contradicts":
                counts["contradicts"] += 1
            elif view.edge == "supersedes":
                counts["supersedes" if view.direction == "out" else "superseded-by"] += 1
            elif view.edge == "applies-to":
                counts["applies-to"] += 1
                modules.append(view.other)
        rows.append(GraphLessonRow(lesson, counts, sorted(set(modules))))
    focus: Lesson | None = None
    focus_edges: list[EdgeView] = []
    focus_co: list[tuple[str, int]] = []
    nodes: list[GraphNode] = []
    links: list[GraphLink] = []
    if focus_id:
        try:
            _, focus = repo.learning.find_lesson(focus_id)
        except LearningError:
            focus = None
        if focus is not None and focus.profile == profile:
            focus_edges = db.edges_for_lesson(profile, focus.number)
            focus_co = db.co_occurs(repo.store.connection, profile, focus.number)
            nodes, links = _neighborhood_svg(focus, focus_edges, focus_co)
        else:
            focus = None
    return LearningGraph(
        profile=profile,
        stats=stats,
        q=q,
        module=module,
        relation=relation,
        modules=stats.top_modules,
        rows=rows,
        focus=focus,
        focus_edges=focus_edges,
        focus_co_occurs=focus_co,
        nodes=nodes,
        links=links,
    )


def _module_matches(stored: str, query: str) -> bool:
    """Directory-prefix matching on module refs, qualified or not (`src/auth` matches
    `baton:src/auth/login.py`)."""
    _, tail = split_module_ref(query.strip())
    want = normalize_module_ref(tail)
    if not want:
        return False
    paths = [stored] if ":" not in stored else [stored, stored.split(":", 1)[1]]
    return any(p == want or p.startswith(want + "/") for p in paths)


def _neighborhood_svg(
    focus: Lesson, edges: list[EdgeView], co_occurs: list[tuple[str, int]]
) -> tuple[list[GraphNode], list[GraphLink]]:
    """Radial 1-hop neighborhood: the lesson centered, neighbors on a circle, edges labeled
    and color-coded. Deterministic, ≤ 13 nodes, no layout library."""
    import math

    items: list[tuple[str, str, str, str]] = []  # (kind, label, edge, sub)
    for view in edges:
        kind = {"lesson": "lesson", "run": "run", "module": "module"}.get(view.other_type, "lesson")
        label = view.other if len(view.other) <= 26 else "…" + view.other[-25:]
        sub = view.edge if view.direction == "out" else f"{view.edge}-by"
        if view.edge == "contradicts":
            sub = "contradicts"
        items.append((kind, label, view.edge, sub))
    for other, times in co_occurs[:4]:
        items.append(("lesson", other, "co-occurs-with", f"co-occurs x{times}"))
    items = items[:12]
    cx, cy, radius = 160.0, 160.0, 118.0
    nodes = [GraphNode("focus", focus.id, "", cx, cy)]
    links: list[GraphLink] = []
    for i, (kind, label, edge, sub) in enumerate(items):
        angle = 2 * math.pi * i / max(len(items), 1) - math.pi / 2
        x, y = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        nodes.append(GraphNode(kind, label, sub, x, y))
        color = _EDGE_COLORS.get(edge, "#8a8f98")
        links.append(GraphLink(cx, cy, x, y, color, sub))
    return nodes, links


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


# ---- artifacts ---------------------------------------------------------------------------

ARTIFACT_KINDS = ("plan", "diff", "tests", "design")
ARTIFACT_LABELS = {"plan": "Plan", "diff": "Diff", "tests": "Tests & Quality", "design": "Design"}
_DIFF_STATES = (
    RunState.INTEGRATING,
    RunState.SHIPPING,
    RunState.AWAITING_SHIP,
    RunState.DONE,
)


@dataclass(frozen=True)
class Artifact:
    """One work product from a run: a plan, a diff, or a quality report.

    `url` deep-links to the existing viewer (the chat's work tab when the run
    came from a chat, otherwise the run page); this page never duplicates viewers.
    """

    kind: str  # one of ARTIFACT_KINDS
    title: str
    run_id: str
    run_title: str
    chat_id: str | None
    chat_title: str | None
    created_at: datetime
    url: str


def artifacts(
    repo: Repo, kind: str = "", chat_id: str = "", q: str = ""
) -> tuple[list[Artifact], list[tuple[str, str]]]:
    """Every work product across all runs, newest first, plus the chat list for filters.

    Plans come from runs with a validated plan, diffs from runs that reached the
    integration states, test reports from the quality-gate table, and designs from
    runs that went through the DESIGNING stage. Preview and
    terminal streams are live-only (no stored history) and are not aggregated here.
    `kind` filters to one artifact type, `chat_id` to one chat, `q` searches titles,
    run titles and chat titles case-insensitively.
    """
    run_chat: dict[str, tuple[str, str]] = {}
    chat_options: list[tuple[str, str]] = []
    for meta in list_chats(repo.root):
        title = chat_title(meta, repo.root)
        chat_options.append((meta.chat_id, title))
        for entry in meta.runs:
            run_chat.setdefault(entry["run_id"], (meta.chat_id, title))

    out: list[Artifact] = []
    for run in repo.store.list_runs():
        cid, ctitle = run_chat.get(run.id, (None, None))
        run_title = run.idea.text
        if run.plan is not None:
            out.append(
                Artifact(
                    kind="plan",
                    title=f"Plan: {run_title}",
                    run_id=run.id,
                    run_title=run_title,
                    chat_id=cid,
                    chat_title=ctitle,
                    created_at=run.created_at,
                    url=f"/chats/{cid}/work/plan" if cid else f"/runs/{run.id}",
                )
            )
        if run.design is not None:
            out.append(
                Artifact(
                    kind="design",
                    title=f"Design: {run.design.title or run_title}",
                    run_id=run.id,
                    run_title=run_title,
                    chat_id=cid,
                    chat_title=ctitle,
                    created_at=run.updated_at,
                    url=f"/runs/{run.id}#design",
                )
            )
        if run.state in _DIFF_STATES:
            out.append(
                Artifact(
                    kind="diff",
                    title=f"Diff: {run_title}",
                    run_id=run.id,
                    run_title=run_title,
                    chat_id=cid,
                    chat_title=ctitle,
                    created_at=run.updated_at,
                    url=f"/chats/{cid}/work/diff" if cid else f"/runs/{run.id}",
                )
            )
        for report in repo.store.list_quality_reports(run.id):
            out.append(
                Artifact(
                    kind="tests",
                    title=f"Quality report {report.result.label}: {run_title}",
                    run_id=run.id,
                    run_title=run_title,
                    chat_id=cid,
                    chat_title=ctitle,
                    created_at=report.created_at,
                    url=f"/chats/{cid}/work/tests" if cid else f"/runs/{run.id}#quality-review",
                )
            )

    needle = q.strip().lower()
    rows = [
        a
        for a in out
        if (not kind or a.kind == kind)
        and (not chat_id or a.chat_id == chat_id)
        and (
            not needle
            or needle in a.title.lower()
            or needle in a.run_title.lower()
            or needle in (a.chat_title or "").lower()
        )
    ]
    rows.sort(key=lambda a: a.created_at, reverse=True)
    return rows, chat_options


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
    week_usd: float  # trailing 7 days, same buckets as the daily chart
    month_usd: float  # trailing `days` days, same buckets as the daily chart
    avg_usd: float  # total / runs, 0.0 when there are no runs
    costliest: list[RunRecord]  # top runs by spend, for the leaderboard table
    day_totals: list[tuple[date, float]]  # (day, usd) oldest first, one per day
    days: int
    saved_usd: float = 0.0  # dollars the decision/role cache saved (issue #10)
    saved_by_run: dict[str, float] = field(default_factory=dict)

    @property
    def has_spend(self) -> bool:
        return self.total > 0


def spend_view(
    repo: Repo, days: int = 30, top_runs: int = 25, today: date | None = None
) -> SpendView:
    runs = repo.store.list_runs()
    saved_by_run = repo.store.cache_saved_by_run()
    return spend_view_for_runs(
        runs,
        days=days,
        top_runs=top_runs,
        today=today,
        saved_by_run=saved_by_run,
    )


def spend_view_for_runs(
    runs: list[RunRecord],
    *,
    days: int = 30,
    top_runs: int = 25,
    today: date | None = None,
    saved_by_run: dict[str, float] | None = None,
) -> SpendView:
    """The spend page's numbers from a run list (pure; `spend_view` reads the store).

    The week/month windows reuse `spend_by_day`'s buckets, so the summary cards
    can never disagree with the daily chart.
    """
    day_totals = spend_by_day(runs, days, today)
    month_usd = sum(usd for _, usd in day_totals)
    week_usd = sum(usd for _, usd in spend_by_day(runs, 7, today))
    total = sum(r.spend_usd for r in runs)
    costly = sorted(runs, key=lambda r: r.spend_usd, reverse=True)[:top_runs]
    saved = saved_by_run or {}
    return SpendView(
        runs=runs,
        total=total,
        week_usd=week_usd,
        month_usd=month_usd,
        avg_usd=total / len(runs) if runs else 0.0,
        costliest=costly,
        day_totals=day_totals,
        days=days,
        saved_usd=sum(saved.values()),
        saved_by_run=saved,
    )


# ---- KPIs --------------------------------------------------------------------------------


@dataclass
class KpiView:
    rows: list[PrKpis]  # newest first, for the table
    summary: Summary
    trends: list[Trend]
    last: int
    ship_enabled: bool
    last_synced: datetime | None = None  # when `ship sync --all` last ran (any surface)

    @property
    def regressions(self) -> list[Trend]:
        return [t for t in self.trends if t.regressed]


SHIP_SYNC_FILENAME = "ship_sync.json"  # in the state dir: {"synced_at": isoformat}


def ship_last_synced(repo: Repo) -> datetime | None:
    """When `jev ship sync --all` last refreshed the shipped PRs (any surface), if ever."""
    import json

    try:
        raw = json.loads((repo.state_dir / SHIP_SYNC_FILENAME).read_text())
        return datetime.fromisoformat(raw["synced_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def kpi_view(repo: Repo, last: int = DEFAULT_LAST) -> KpiView:
    """`jev kpi --last N`: the shipped PRs with their KPIs, medians and trends."""
    rows = load_pr_kpis(repo.store, last)
    return KpiView(
        rows=list(reversed(rows)),
        summary=summarize(rows),
        trends=trends(rows, last),
        last=last,
        ship_enabled=repo.config.ship.enabled,
        last_synced=ship_last_synced(repo),
    )


# ---- settings ----------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvVar:
    name: str
    used_for: str
    is_set: bool  # never the value
    from_secrets: bool = False  # this process got it from ~/.jev/secrets.env

    @property
    def status(self) -> str:
        """Display label. Keys from the real environment cannot be removed here."""
        if not self.is_set:
            return "Not set"
        return "Set" if self.from_secrets else "Set (environment)"


def env_var_names(config: JevConfig) -> list[str]:
    """Variable names the config points at — the datalist for the add-key form.
    The test hook is excluded; it is not a real key."""
    names = [config.dispatcher.jev.api_key_env, config.workers.api.api_key_env]
    for _name, server in config.tools.mcp.items():
        names += list(server.env_from)
        if server.bearer_token_env:
            names.append(server.bearer_token_env)
        names += list(server.headers_from_env.values())
    return sorted({n for n in names if n and n != FAKE_WORKERS_ENV})


def env_vars(config: JevConfig) -> list[EnvVar]:
    """Environment variables the config names, and whether each is set. Never values."""
    from jev_agent.core.secrets import loaded_names, read_secrets

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
    # Keys saved through the dashboard show here even when the config no longer
    # (or never did) reference them, so they stay visible and removable.
    for name in read_secrets():
        seen.setdefault(name, []).append("secrets file (~/.jev/secrets.env)")
    from_file = loaded_names()
    return [
        EnvVar(
            name,
            ", ".join(uses),
            bool(os.environ.get(name, "").strip()),
            name in from_file,
        )
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


def browser_check(repo: Repo) -> Check | None:
    """The doctor's browser check, if the config uses the browser at all."""
    for check in doctor_checks(repo.root):
        if check.name == "browser":
            return check
    return None


def model_listing(repo: Repo) -> tuple[ModelListing | None, str | None]:
    """`jev models list` from the on-disk cache only: the dashboard never fetches the
    network for this page (that would hang a render); `jev models list --refresh`
    refreshes the cache."""
    from jev_agent.app.models import list_models
    from jev_agent.workers.openrouter_models import cache_path, load_catalog

    catalog, _ = load_catalog(
        cache_path(repo.root),
        repo.config.workers.api.base_url,
        ttl_s=repo.config.models.cache_ttl_hours * 3600,
        refresh=False,
        allow_fetch=False,
    )
    if catalog is None:
        return None, None
    return list_models(catalog, free=False, supports=[]), None


def quality_checks(repo: Repo) -> list[CheckRow]:
    """`jev quality checks`: every check by level, whether its tool is installed and
    whether it applies to this repo. (The ai-review gate is a static row in the
    template, like the CLI's extra table row.)"""
    return check_rows(repo.root, repo.config)


# ---- fleet ---------------------------------------------------------------------------------

# The six agent roles, in pipeline order; the roster on the Fleet page shows one card each.
ROLE_NAMES: tuple[str, ...] = (
    "planner",
    "validator",
    "coder",
    "reviewer",
    "code_reviewer",
    "learner",
)


@dataclass(frozen=True)
class RoleCard:
    """One row of the Fleet roster: a role, what it does, and whether it is working."""

    role: str
    title: str  # "Code Reviewer"
    blurb: str  # one line, from the config schema's field description
    workers: str  # "claude_code, api"
    addendum: str  # the role's current additional prompt instructions
    working: bool  # an agent with this role is running right now


def role_roster(config: JevConfig, agents: list[FleetAgent]) -> list[RoleCard]:
    """The six role cards for the Fleet page, in pipeline order.

    `working` reuses what the fleet endpoint already computed: a role is working when
    one of the currently-running agents has that role. Blurds come from the schema so
    they cannot drift from the config reference.
    """
    from jev_agent.config.schema import RolesConfig

    active = {agent.role for agent in agents}
    cards: list[RoleCard] = []
    for role in ROLE_NAMES:
        rc = getattr(config.roles, role)
        description = RolesConfig.model_fields[role].description or ""
        cards.append(
            RoleCard(
                role=role,
                title=role.replace("_", " ").title(),
                blurb=description,
                workers=", ".join(rc.workers) or "—",
                addendum=rc.prompt_addendum,
                working=role in active,
            )
        )
    return cards


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
