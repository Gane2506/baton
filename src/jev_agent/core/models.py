"""Core data models shared by the harness, dispatchers, workers and store (PLAN.md section 4)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DecisionPoint = Literal["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"]
# `learner` distills lessons at the end of a run (docs/learning.md); `code_reviewer` reviews
# diffs for the quality gate (docs/quality.md); the others build the code.
Role = Literal["planner", "validator", "coder", "reviewer", "code_reviewer", "learner"]
# The roles a learned lesson can apply to (every role except the learner itself).
LESSON_ROLES: tuple[Role, ...] = ("planner", "validator", "coder", "reviewer")
WorkerStatus = Literal["success", "failure", "error", "quota_exhausted", "timeout"]


class RunState(StrEnum):
    """States of a run (one idea). See PLAN.md section 3."""

    INTAKE = "INTAKE"
    PLANNING = "PLANNING"
    VALIDATING = "VALIDATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    INTEGRATING = "INTEGRATING"
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


class Idea(BaseModel):
    text: str = Field(min_length=1)
    repo_path: Path
    context_files: list[Path] = Field(default_factory=list)


class TaskCard(BaseModel):
    id: str = Field(pattern=r"^T\d+$")  # "T1", "T2", ...
    title: str
    description: str
    files_hint: list[str] = Field(default_factory=list)  # files/dirs likely touched
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    test_command: str | None = None  # e.g. "pytest tests/test_search.py"


class Plan(BaseModel):
    summary: str
    tasks: list[TaskCard] = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)

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


class ValidationResult(BaseModel):
    verdict: Literal["approve", "revise"]
    issues: list[str] = Field(default_factory=list)


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
