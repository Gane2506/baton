"""Event-sourced execution state (docs/events.md).

Every run mutation is dual-written as an append-only event in `run_events`
(migration 17). The snapshot tables stay the source of truth for the pipeline,
the dashboard and the scheduler; events are a projection of the log, used by
`jev events`, `jev replay` (verify mode) and `jev bisect`.

Single-writer invariant: the run lease guarantees at most one driver per run,
and `Store._emit` runs inside the same `_tx()` block as the state write, with
`run_seq = MAX(run_seq)+1` computed in that transaction. No new locking.

Payloads reference, never duplicate, the snapshot tables — and never carry
free-text prompts or transcripts (those stay in `.jev/runs/<id>/` log files,
already redacted by `runlog/redact.py`). `test_event_payloads_carry_no_secrets`
asserts this.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

EVENT_SCHEMA_V = 1

# Actors recorded on events. "pipeline" drives the run under the lease; "human"
# acts through `jev approve` / `jev cancel` / the dashboard; "scheduler" through
# `jev scheduler`; "worker:<role>" is reserved for worker-side emissions.
ACTOR_PIPELINE = "pipeline"
ACTOR_HUMAN = "human"
ACTOR_SCHEDULER = "scheduler"


class EventType(StrEnum):
    """Every kind of event in `run_events`. Stable strings: persisted in the DB."""

    RUN_CREATED = "run.created"
    RUN_STATE_CHANGED = "run.state_changed"
    RUN_RESUMED = "run.resumed"
    PLAN_SET = "plan.set"
    DESIGN_SET = "design.set"
    VALIDATION_RECORDED = "validation.recorded"
    TASKS_UPSERTED = "tasks.upserted"
    TASK_STATE_CHANGED = "task.state_changed"
    TASK_ASSIGNED = "task.assigned"
    ATTEMPT_STARTED = "attempt.started"
    ATTEMPT_TESTED = "attempt.tested"
    ATTEMPT_FINISHED = "attempt.finished"
    DECISION_REQUESTED = "decision.requested"
    DECISION_RECORDED = "decision.recorded"
    GATE_CHECKED = "gate.checked"
    SHIP_RECORDED = "ship.recorded"
    SPEND_ADDED = "spend.added"
    LEASE_CLAIMED = "lease.claimed"
    LEASE_RELEASED = "lease.released"
    LEASE_TAKEN_OVER = "lease.taken_over"
    HUMAN_APPROVED = "human.approved"
    HUMAN_NOTE = "human.note"


class RunCreatedPayload(BaseModel):
    idea_text: str
    quality_level: str
    plan_only: bool = False
    ship_mode: str | None = None
    autonomy_mode: str = "standard"


class RunStateChangedPayload(BaseModel):
    from_state: str
    to_state: str
    reason: str | None = None
    resume_state: str | None = None


class RunResumedPayload(BaseModel):
    from_state: str
    via: str  # "resume command" | "scheduler recovery"


class PlanSetPayload(BaseModel):
    round: int
    plan: dict[str, Any]  # the full plan JSON: plans are small and central to replay


class DesignSetPayload(BaseModel):
    design: dict[str, Any]  # the full design doc JSON (the DESIGNING stage)


class ValidationRecordedPayload(BaseModel):
    verdict: str
    issues: list[str] = Field(default_factory=list)
    validation: dict[str, Any] | None = None  # full JSON when a validation ran


class TasksUpsertedPayload(BaseModel):
    task_ids: list[str]
    from_plan_round: int


class TaskStateChangedPayload(BaseModel):
    task_id: str
    from_state: str
    to_state: str
    note: str | None = None


class TaskAssignedPayload(BaseModel):
    task_id: str
    worker: str
    model: str | None = None
    effort: str | None = None
    decision_id: int | None = None  # the D4/D5 decision that picked this assignment


class AttemptStartedPayload(BaseModel):
    attempt_id: int
    task_id: str | None = None
    attempt_no: int
    role: str
    worker: str
    model: str | None = None
    lesson_ids: list[str] = Field(default_factory=list)
    log_path: str | None = None  # full transcript stays in the log file, not here


class AttemptTestedPayload(BaseModel):
    attempt_id: int
    task_id: str | None = None
    tests: str  # "passed" | "failed" | "skipped"


class AttemptFinishedPayload(BaseModel):
    attempt_id: int
    task_id: str | None = None
    status: str
    summary: str
    cost_usd: float | None = None
    duration_s: float | None = None
    # The full result stays in `attempts.result_json`; this is the story without joins.


class DecisionRequestedPayload(BaseModel):
    point: str  # D1..D8
    task_id: str | None = None
    questions: dict[str, list[str]]  # question name -> allowed answers (the ask)


class DecisionRecordedPayload(BaseModel):
    decision_id: int
    point: str
    task_id: str | None = None
    answers: dict[str, str]
    dispatcher: str
    model: str | None = None
    fallback_reason: str | None = None


class GateCheckedPayload(BaseModel):
    report_id: int
    task_id: str | None = None
    scope: str
    level: str
    blocked: bool
    top_findings: list[str] = Field(default_factory=list)


class ShipRecordedPayload(BaseModel):
    pr_id: int
    number: int | None = None
    state: str
    ci_status: str | None = None


class SpendAddedPayload(BaseModel):
    usd: float
    task_id: str | None = None
    running_total: float


class LeaseClaimedPayload(BaseModel):
    pid: int
    host: str
    reason: str  # "start" | "approve" | "resume" | "recovery" | "drive"


class LeaseReleasedPayload(BaseModel):
    held_s: float | None = None


class LeaseTakenOverPayload(BaseModel):
    old_pid: int | None = None
    old_host: str | None = None
    new_pid: int
    new_host: str


class HumanApprovedPayload(BaseModel):
    gate: str  # which AWAITING_* state was passed
    execute: bool = True


class HumanNotePayload(BaseModel):
    notes: str  # the person's own words (`jev ship modify`)
    task_id: str | None = None


#: Payload model per event type; `Event.payload` is validated against it.
EVENT_PAYLOADS: dict[EventType, type[BaseModel]] = {
    EventType.RUN_CREATED: RunCreatedPayload,
    EventType.RUN_STATE_CHANGED: RunStateChangedPayload,
    EventType.RUN_RESUMED: RunResumedPayload,
    EventType.PLAN_SET: PlanSetPayload,
    EventType.DESIGN_SET: DesignSetPayload,
    EventType.VALIDATION_RECORDED: ValidationRecordedPayload,
    EventType.TASKS_UPSERTED: TasksUpsertedPayload,
    EventType.TASK_STATE_CHANGED: TaskStateChangedPayload,
    EventType.TASK_ASSIGNED: TaskAssignedPayload,
    EventType.ATTEMPT_STARTED: AttemptStartedPayload,
    EventType.ATTEMPT_TESTED: AttemptTestedPayload,
    EventType.ATTEMPT_FINISHED: AttemptFinishedPayload,
    EventType.DECISION_REQUESTED: DecisionRequestedPayload,
    EventType.DECISION_RECORDED: DecisionRecordedPayload,
    EventType.GATE_CHECKED: GateCheckedPayload,
    EventType.SHIP_RECORDED: ShipRecordedPayload,
    EventType.SPEND_ADDED: SpendAddedPayload,
    EventType.LEASE_CLAIMED: LeaseClaimedPayload,
    EventType.LEASE_RELEASED: LeaseReleasedPayload,
    EventType.LEASE_TAKEN_OVER: LeaseTakenOverPayload,
    EventType.HUMAN_APPROVED: HumanApprovedPayload,
    EventType.HUMAN_NOTE: HumanNotePayload,
}


class Event(BaseModel):
    """One row of `run_events`, with its payload validated per type."""

    schema_v: int = EVENT_SCHEMA_V
    run_id: str
    run_seq: int
    type: EventType
    ts: datetime
    actor: str = ACTOR_PIPELINE
    parent_run_seq: int | None = None
    synthetic: bool = False
    payload: dict[str, Any]

    def typed_payload(self) -> BaseModel:
        """The payload validated against this event type's model."""
        model = EVENT_PAYLOADS[self.type]
        return model.model_validate(self.payload)


def event_from_row(row: Any) -> Event:
    """Build an `Event` from a `run_events` row (payload re-validated per type)."""
    payload = json.loads(row["payload_json"])
    model = EVENT_PAYLOADS[EventType(row["type"])]
    model.model_validate(payload)  # fail loud on a corrupt or drifted payload
    return Event(
        schema_v=row["schema_v"],
        run_id=row["run_id"],
        run_seq=row["run_seq"],
        type=EventType(row["type"]),
        ts=datetime.fromisoformat(row["ts"]),
        actor=row["actor"],
        parent_run_seq=row["parent_run_seq"],
        synthetic=bool(row["synthetic"]),
        payload=payload,
    )
