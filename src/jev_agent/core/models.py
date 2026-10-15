"""Core data models shared by the harness, dispatchers, workers and store (PLAN.md section 4)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DecisionPoint = Literal["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"]
# `learner` distills lessons at the end of a run (docs/learning.md); `code_reviewer` reviews
# diffs for the quality gate (docs/quality.md); `designer` designs the UI before it is approved
# (core/design_system.py); the others build the code.
Role = Literal["planner", "validator", "coder", "reviewer", "code_reviewer", "learner", "designer"]
# The roles a learned lesson can apply to (every role except the learner itself).
LESSON_ROLES: tuple[Role, ...] = ("planner", "validator", "coder", "reviewer")
WorkerStatus = Literal["success", "failure", "error", "quota_exhausted", "timeout"]


class RunState(StrEnum):
    """States of a run (one idea). See PLAN.md section 3."""

    INTAKE = "INTAKE"
    PLANNING = "PLANNING"
    VALIDATING = "VALIDATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    # ASK_EVERY_STEP autonomy mode: one explicit gate per pipeline step, approved with
    # `jev approve` like AWAITING_APPROVAL (AWAITING_SHIP stays the ship gate).
    AWAITING_PLANNING = "AWAITING_PLANNING"  # INTAKE done; approve before PLANNING
    AWAITING_VALIDATING = "AWAITING_VALIDATING"  # plan drafted; approve before VALIDATING
    DESIGNING = "DESIGNING"  # producing the run's UI design
    # ASK_EVERY_STEP autonomy mode: the design gate, approved with `jev approve` like the
    # other step gates (AWAITING_SHIP stays the ship gate).
    AWAITING_DESIGNING = "AWAITING_DESIGNING"  # UI work approved; approve before DESIGNING
    AWAITING_INTEGRATING = "AWAITING_INTEGRATING"  # tasks executed; approve before INTEGRATING
    AWAITING_SHIPPING = "AWAITING_SHIPPING"  # D6 voted ship; approve before SHIPPING
    EXECUTING = "EXECUTING"
    INTEGRATING = "INTEGRATING"
    # D6 voted to ship; a person chooses approve, modify or stop (`[ship] after_approval`).
    AWAITING_SHIP = "AWAITING_SHIP"
    # Pushing the result, opening the PR, waiting for its checks and merging (docs/ship.md).
    SHIPPING = "SHIPPING"
    DONE = "DONE"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FAILED = "FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    CANCELLED = "CANCELLED"


class TaskState(StrEnum):
    """States of a task (one unit of work from the plan). See PLAN.md section 3."""

    PENDING = "PENDING"
    READY = "READY"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    RETRY = "RETRY"
    ESCALATE = "ESCALATE"
    REASSIGN = "REASSIGN"
    DONE = "DONE"
    FAILED = "FAILED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class AutonomyMode(StrEnum):
    """How much of the pipeline runs without a person (Phase 2, workstream C1).

    Recorded on the run; `core/pipeline.py` dispatches on it. STANDARD is the
    historical approve-once flow, so runs written before the mode existed keep it.
    """

    # The historical default: one human gate on the plan (AWAITING_APPROVAL), plus the
    # ship gate (AWAITING_SHIP) when `[ship] after_approval` asks for it.
    STANDARD = "standard"
    # Explicit opt-in only (`AutonomyConfig.autonomous_opt_in`): no plan-approval gate;
    # the Jev+Laya >= 85 decision gate stays the quality backstop.
    AUTONOMOUS = "autonomous"
    # A person approves each pipeline step: the run waits in an explicit AWAITING_* gate
    # state before PLANNING, VALIDATING, EXECUTING, INTEGRATING and SHIPPING.
    ASK_EVERY_STEP = "ask_every_step"
    # Chat-driven: `Pipeline.start` only creates the run; it waits in INTAKE for an
    # explicit instruction (`advance`/`resume`, or the chat driver).
    MANUAL = "manual"
    # Folder-scoped standard flow; enforcing the folder boundary is workstream C2's job.
    MEDIUM = "medium"


class AutonomyConfig(BaseModel):
    """A run's autonomy configuration (Phase 2, workstream C1).

    Validated at the boundary (`Pipeline.start`): AUTONOMOUS needs an explicit opt-in
    (never the default), MEDIUM needs the scoped folder.
    """

    mode: AutonomyMode = AutonomyMode.STANDARD
    autonomous_opt_in: bool = False
    medium_folder: str | None = None

    @model_validator(mode="after")
    def _check_opt_ins(self) -> AutonomyConfig:
        if self.mode is AutonomyMode.AUTONOMOUS and not self.autonomous_opt_in:
            raise ValueError("AUTONOMOUS mode needs an explicit opt-in (autonomous_opt_in=True)")
        if self.mode is AutonomyMode.MEDIUM and not (self.medium_folder or "").strip():
            raise ValueError("MEDIUM mode needs the scoped folder (medium_folder)")
        return self

    def is_default(self) -> bool:
        """The historical configuration: the store's column defaults already record it,
        so `create_run` needs no new arguments."""
        return (
            self.mode is AutonomyMode.STANDARD
            and not self.autonomous_opt_in
            and self.medium_folder is None
        )


class Idea(BaseModel):
    text: str = Field(min_length=1)
    repo_path: Path
    context_files: list[Path] = Field(default_factory=list)


class TaskCard(BaseModel):
    id: str = Field(pattern=r"^T\d+$")  # "T1", "T2", ...
    title: str
    description: str
    kind: Literal["plan", "fix"] | None = Field(
        default=None,
        description="Task kind: 'plan' for planner tasks, 'fix' for integration fix tasks. "
        "None for cards written before kinds existed; those fall back to the legacy title "
        "check (core.integration.is_fix_task).",
    )
    files_hint: list[str] = Field(default_factory=list)  # files/dirs likely touched
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    test_command: str | None = None  # e.g. "pytest tests/test_search.py"


class Plan(BaseModel):
    summary: str
    tasks: list[TaskCard] = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)
    # True when the work involves UI or frontend: the planner flags it so the pipeline can
    # design the UI (the `designer` role, core/design_system.py) before the design is approved.
    needs_design: bool = False

    @model_validator(mode="after")
    def _check_task_graph(self) -> Plan:
        ids = [t.id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        known = set(ids)
        for task in self.tasks:
            missing = [d for d in task.depends_on if d not in known]
            if missing:
                raise ValueError(f"task {task.id} depends on unknown task(s): {missing}")
            if task.id in task.depends_on:
                raise ValueError(f"task {task.id} depends on itself")
        return self


class ScreenSpec(BaseModel):
    """One screen of a UI design: what it shows and which components it is built from."""

    name: str
    description: str = ""
    components: list[str] = Field(default_factory=list)


class DesignDoc(BaseModel):
    """The designer's output: the run's UI design, grounded in the repo's design system.

    `tokens_ref` is the design-system version the design was built against (None when the
    repo has no system yet); `notes` may propose updates to the system itself.
    """

    title: str
    summary: str = ""
    screens: list[ScreenSpec] = Field(default_factory=list)
    tokens_ref: str | None = None
    notes: list[str] = Field(default_factory=list)
    version: int = 1


class ChoiceChallenge(BaseModel):
    """The validator's "why X, not Y" on one choice the planner made."""

    choice: str  # what the plan does
    alternative: str = ""  # the option the plan did not take
    reason: str = ""  # the planner's reason, or "(not stated)"
    holds: bool  # whether the choice is justified over the alternative
    note: str = ""  # the validator's assessment; for a failed challenge, what is wrong

    def as_issue(self) -> str:
        """The issue a failed challenge sends back to the planner."""
        versus = f" instead of {self.alternative}" if self.alternative else ""
        why = self.note or "the reason does not hold"
        return f"Choice: {self.choice}{versus}. {why} (planner's reason: {self.reason or 'none'})"


class ValidationResult(BaseModel):
    verdict: Literal["approve", "revise"]
    issues: list[str] = Field(default_factory=list)
    challenges: list[ChoiceChallenge] = Field(default_factory=list)  # empty in pre-#67 runs

    @model_validator(mode="after")
    def _failed_challenges_are_issues(self) -> ValidationResult:
        """A choice without a sound reason is an issue and the plan goes back for revision.

        Enforced here so a reply that flags a choice but still approves is consistent, and the
        planner gets the mistake through `issues`. Idempotent: a stored result reloads as is.
        """
        for challenge in self.challenges:
            if challenge.holds:
                continue
            self.verdict = "revise"
            issue = challenge.as_issue()
            if issue not in self.issues:
                self.issues.append(issue)
        return self


class WorkerResult(BaseModel):
    status: WorkerStatus
    summary: str  # short, written by the agent per role contract
    files_changed: list[str] = Field(default_factory=list)
    cost_usd: float | None = None
    tokens: dict[str, int] | None = None
    raw_log_path: Path


class DecisionRequest(BaseModel):
    point: DecisionPoint
    state: dict[str, Any]  # compact cards only, never full transcripts
    questions: dict[str, list[str]]  # question name -> allowed answers

    @model_validator(mode="after")
    def _check_questions(self) -> DecisionRequest:
        if not self.questions:
            raise ValueError("a decision request needs at least one question")
        for name, allowed in self.questions.items():
            if not allowed:
                raise ValueError(f"question {name!r} has no allowed answers")
        return self


class Decision(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    point: DecisionPoint
    answers: dict[str, str]
    dispatcher: str  # "jev" | "rules"
    model: str | None = None  # model that made the decision
    cost_usd: float | None = None
    latency_ms: int = 0
    # Dispatcher-specific extras, stored with the decision. Jev puts `direct` (questions with a
    # single allowed answer), `answers` ({question: {choice, confidence, probabilities}}) and
    # `usage` here; the rules dispatcher leaves it empty.
    details: dict[str, Any] = Field(default_factory=dict)

    def invalid_answers(self, req: DecisionRequest) -> list[str]:
        """Return a description of every answer that is missing or not allowed by `req`."""
        problems: list[str] = []
        for name, allowed in req.questions.items():
            if name not in self.answers:
                problems.append(f"{name}: missing")
            elif self.answers[name] not in allowed:
                problems.append(f"{name}: {self.answers[name]!r} not in {allowed}")
        return problems
