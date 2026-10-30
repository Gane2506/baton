"""Store record types: the frozen dataclasses a `Store` reads and writes.

These are pure data (plus the `LearningCounts` addition); they know nothing
about SQLite. Row -> record conversion lives in `rows.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, get_args

from jev_agent.config.schema import ShipMode
from jev_agent.core.models import (
    Decision,
    DecisionRequest,
    DesignDoc,
    Idea,
    Plan,
    Role,
    RunState,
    TaskCard,
    TaskState,
    ValidationResult,
    WorkerResult,
)
from jev_agent.quality.levels import QualityLevel
from jev_agent.quality.models import GateResult


@dataclass(frozen=True)
class LearningCounts:
    """Lesson changes made by a run's learning steps (`core.learner`), kept as data."""

    added: int
    updated: int
    retired: int

    def __add__(self, other: LearningCounts) -> LearningCounts:
        return LearningCounts(
            self.added + other.added, self.updated + other.updated, self.retired + other.retired
        )


@dataclass(frozen=True)
class RunRecord:
    id: str
    idea: Idea
    state: RunState
    plan: Plan | None
    plan_round: int
    spend_usd: float
    created_at: datetime
    updated_at: datetime
    reason: str | None = None  # why the run stopped (NEEDS_HUMAN, FAILED, ...), if it did
    validation: ValidationResult | None = None  # the latest validator verdict
    # For a NEEDS_HUMAN run that `resume` may continue: the state to continue from.
    resume_state: RunState | None = None
    # The learning profile this run reads and writes lessons with; None: learning is off.
    learning_profile: str | None = None
    # What the end-of-run learning step did ("learned: ...", "skipped: ..."); None: not yet.
    learning_outcome: str | None = None
    # Lessons the run's learning steps added, updated and retired; None: none applied.
    learning_counts: LearningCounts | None = None
    # The quality gate level this run uses (docs/quality.md); "none" for older runs.
    quality_level: QualityLevel = "none"
    # `jev run --plan-only` (or a plan-only queue item): stop at the approval gate even when
    # `[approval] require_plan_approval` is off, also after `jev resume`.
    plan_only: bool = False
    # `jev run --auto-ship` ("merge") or `--ask-before-ship` ("ask"); None: the config decides.
    ship_mode: ShipMode | None = None
    # Phase 2 workstream C: the run's autonomy mode (see core/models.py::AutonomyMode);
    # "standard" is the historical approve-once flow.
    autonomy_mode: str = "standard"
    # Explicit opt-in for the autonomous mode; never default-on.
    autonomous_opt_in: bool = False
    # Folder the medium mode is scoped to; None: no folder scoping.
    medium_folder: str | None = None
    # The designer's design doc (migration 16, the DESIGNING pipeline stage); None: the
    # run never needed a design (its plan had `needs_design` false).
    design: DesignDoc | None = None
    # The chat's design-system choice, for runs launched from a chat (migration 19):
    # "library:<name>", "repo", or None (the repo default, as before).
    design_system: str | None = None
    # Workflow profile snapshot columns (issue #368, migration 22; design
    # section 6.4). None for legacy runs: no profile was picked, no snapshot
    # stored, and execution behaviour is unchanged.
    workflow_profile_id: str | None = None
    workflow_profile_version: int | None = None
    workflow_profile_release: str | None = None
    goal: str | None = None  # the user-stated goal; None: none was stated
    preset_goal: str | None = None  # the picked preset's goal label, display only
    assurance: str | None = None  # "full" | "reduced" (None: not computed)
    assurance_absent: tuple[str, ...] = ()  # absent behaviours, in plain words
    flow_snapshot: dict[str, object] | None = None  # resolved parameter set


@dataclass(frozen=True)
class StageRecord:
    """One stage execution of a run (design section 11.1): one row per handler
    call, one row per attempt of a retried stage. Token/cost fields are None
    when unknown — never zero — and `completeness` is "partial" when any
    attempt in the row reported no tokens."""

    id: int
    run_id: str
    stage: str  # a RunState work state, e.g. "PLANNING"
    attempt: int  # 1-based, continuing across Halt/resume, never restarting
    workflow_profile_id: str | None
    workflow_profile_version: int | None
    workflow_profile_release: str | None
    goal: str | None
    preset_goal: str | None
    started_at: datetime
    ended_at: datetime | None
    work_seconds: float | None  # ended minus started minus gate-wait seconds
    gate_wait_seconds: float
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    cost_reason: str | None
    cache_hits: int
    cache_served: bool
    semantic_shadow_hits: int
    outcome: str | None  # "success" | "halted" | "failed" | "skipped"
    halt_reason: str | None
    defects_caught: int
    effective_model: str | None
    effective_level: str | None
    completeness: str | None  # "full" | "partial"


@dataclass(frozen=True)
class RunLease:
    """Who drives a run right now (issue #61, `core/lease.py`)."""

    run_id: str
    token: str  # random per claim: tells two holders with the same pid apart
    pid: int
    host: str
    acquired_at: datetime
    heartbeat_at: datetime


@dataclass(frozen=True)
class TaskRecord:
    run_id: str
    id: str
    card: TaskCard
    state: TaskState
    attempts: int
    spend_usd: float
    # Current worker assignment (set at D4, changed by retry/escalate/reassign).
    worker: str | None = None
    model: str | None = None
    effort: str | None = None
    note: str | None = None  # feedback for the next attempt, or why the task failed
    lessons_scored: bool = False  # the lessons injected into its attempts were scored


@dataclass(frozen=True)
class AttemptRecord:
    id: int
    run_id: str
    task_id: str | None  # None for run-level work such as planning
    attempt_no: int
    role: Role
    worker: str
    model: str | None
    effort: str | None
    log_path: Path | None
    started_at: datetime
    finished_at: datetime | None
    result: WorkerResult | None  # None until finish_attempt is called
    lesson_ids: tuple[str, ...] = ()  # lessons injected into this attempt's prompt
    stage: str | None = None  # pipeline stage active at the attempt write, if any


@dataclass(frozen=True)
class QualityReportRecord:
    """One quality gate: a task attempt or an integration round (docs/quality.md)."""

    id: int
    run_id: str
    task_id: str | None
    result: GateResult
    report_path: Path | None
    created_at: datetime


@dataclass(frozen=True)
class DecisionRecord:
    id: int
    run_id: str
    task_id: str | None
    request: DecisionRequest
    decision: Decision
    fallback_reason: str | None  # why the rules dispatcher was used instead, if it was
    created_at: datetime
    stage: str | None = None  # pipeline stage active at the decision write, if any


PrState = Literal["open", "closed", "merged"]
PR_STATES: tuple[PrState, ...] = get_args(PrState)


@dataclass(frozen=True)
class ShippedPrRecord:
    """A pull request opened by `jev ship` for a run, and what `jev ship sync` last read from
    GitHub (None: not synced yet, or GitHub did not say)."""

    id: int
    run_id: str
    remote: str
    repo: str  # owner/name
    branch: str  # the pushed head branch
    base: str
    number: int | None
    url: str | None
    title: str
    draft: bool
    head_oid: str | None  # the commit pushed
    pushed_commits: int | None  # commits the pushed branch had over its base
    created_at: datetime
    state: PrState = "open"
    merged_at: datetime | None = None
    closed_at: datetime | None = None
    review_comments: int | None = None
    reviews: int | None = None
    ci_status: str | None = None  # success | failure | pending | none
    commits_after_open: int | None = None
    additions: int | None = None
    deletions: int | None = None
    changed_files: int | None = None
    reverted: bool | None = None
    last_synced_at: datetime | None = None
    raw: dict[str, object] | None = None  # the last `gh pr view --json` answer

    @property
    def time_to_merge_h(self) -> float | None:
        if self.merged_at is None:
            return None
        return max((self.merged_at - self.created_at).total_seconds(), 0.0) / 3600


QueuePriority = Literal["high", "normal", "low"]
QueueStatus = Literal["queued", "running", "waiting", "done", "failed", "cancelled"]
QUEUE_PRIORITIES: tuple[QueuePriority, ...] = get_args(QueuePriority)
QUEUE_STATUSES: tuple[QueueStatus, ...] = get_args(QueueStatus)


@dataclass(frozen=True)
class QueueItem:
    """One idea waiting in (or worked through from) the queue; see `core/queue.py`."""

    id: int
    idea: str
    priority: QueuePriority
    position: int
    status: QueueStatus  # as stored; `core.queue.effective_status` folds in the run's state
    run_id: str | None
    plan_only: bool
    learning_profile: str | None  # `--learning NAME`
    no_learning: bool  # `--no-learning`
    profile: str | None  # workspace profile name, passed through to the run
    note: str | None
    created_at: datetime
    updated_at: datetime
    quality: QualityLevel | None = None  # `--quality LEVEL`; None: the config's level
    ship_mode: ShipMode | None = None  # `--auto-ship` / `--ask-before-ship`; None: the config's
    # Workflow profile pick (issue #368): "repo:slug" (current version) or
    # "repo:slug@v<n>" (pinned version, as queued). Distinct from `profile`
    # (the workspace profile name) above.
    workflow_profile: str | None = None


@dataclass(frozen=True)
class RecurringTask:
    """A task that re-enqueues `brief` on a cron `schedule` (see `core.recurrence`).

    `schedule` is the normalized cron expression, `schedule_source` the original text;
    `next_due_at` is the next fire time; `active_item_id` is the queue item it last
    enqueued, NULL when idle between runs.
    """

    id: int
    brief: str
    schedule: str
    schedule_source: str
    timezone: str
    next_due_at: datetime
    last_run_at: datetime | None
    enabled: bool
    active_item_id: int | None
    priority: QueuePriority
    plan_only: bool
    learning_profile: str | None
    no_learning: bool
    profile: str | None
    quality: str | None
    ship_mode: str | None
    created_at: datetime
    workflow_profile: str | None = None
