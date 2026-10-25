"""Event-sourced execution state (docs/events.md).

Every run mutation is dual-written as an append-only event in `run_events`
(migration 17). The snapshot tables stay the source of truth for the pipeline,
the dashboard and the scheduler; events are a projection of the log, used by
`jev events`, `jev replay` (verify mode) and `jev bisect`.

Single-writer invariant: the run lease guarantees at most one driver per run,
and `Store._emit` runs inside the same `_tx()` block as the state write, with
`run_seq = MAX(run_seq)+1` computed in that transaction. No new locking.

Payloads carry what replay needs and nothing more: the run's own idea text and
human notes, and the full plan/design JSON (small, and central to replay). They
never carry worker prompts or transcripts (those stay in `.jev/runs/<id>/` log
files, already redacted by `runlog/redact.py`), and never carry secrets —
`test_event_payloads_carry_no_secrets` asserts this.
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
    state: str | None = None  # the task's new state (set_task_assignment sets it too)
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


def summarize(event: Event) -> str:
    """One human-readable line for an event (payload fields only, never secrets).

    Shared by the `jev events` CLI and the dashboard's run-page timeline, so the
    two never drift apart. (`jev_agent.core.bisect._summary` is a shorter variant
    for the standalone `--export` HTML; it stays separate on purpose.)
    """
    p = event.payload
    match event.type:
        case EventType.RUN_CREATED:
            idea = str(p.get("idea_text", ""))[:70]
            return f"run created: {idea}"
        case EventType.RUN_STATE_CHANGED:
            line = f"{p.get('from_state')} → {p.get('to_state')}"
            if p.get("reason"):
                line += f" ({p['reason']})"
            return line
        case EventType.RUN_RESUMED:
            return f"resumed from {p.get('from_state')} via {p.get('via')}"
        case EventType.PLAN_SET:
            tasks = p.get("plan", {}).get("tasks", [])
            return f"plan set: round {p.get('round')}, {len(tasks)} tasks"
        case EventType.DESIGN_SET:
            return "design doc set"
        case EventType.VALIDATION_RECORDED:
            issues = p.get("issues", [])
            return f"validation: {p.get('verdict')} ({len(issues)} issues)"
        case EventType.TASKS_UPSERTED:
            return f"tasks upserted: {', '.join(p.get('task_ids', []))}"
        case EventType.TASK_STATE_CHANGED:
            line = f"{p.get('task_id')}: {p.get('from_state')} → {p.get('to_state')}"
            if p.get("note"):
                line += f" — {str(p['note'])[:80]}"
            return line
        case EventType.TASK_ASSIGNED:
            return (
                f"{p.get('task_id')} assigned to {p.get('worker')}"
                f" ({p.get('model') or 'default model'}, {p.get('effort') or 'default effort'})"
            )
        case EventType.ATTEMPT_STARTED:
            return (
                f"attempt {p.get('attempt_no')} started: {p.get('role')}"
                f" on {p.get('worker') or '?'}"
            )
        case EventType.ATTEMPT_TESTED:
            return f"attempt {p.get('attempt_id')} tests: {p.get('tests')}"
        case EventType.ATTEMPT_FINISHED:
            line = f"attempt {p.get('attempt_id')} finished: {p.get('status')}"
            if p.get("cost_usd") is not None:
                line += f" (${p['cost_usd']:.2f})"
            summary = str(p.get("summary", ""))[:80]
            if summary:
                line += f" — {summary}"
            return line
        case EventType.DECISION_REQUESTED:
            questions = p.get("questions", {})
            asked = ", ".join(sorted(questions))
            return f"{p.get('point')} requested: {asked}"
        case EventType.DECISION_RECORDED:
            answers = p.get("answers", {})
            shown = ", ".join(f"{k}={v}" for k, v in answers.items())
            return f"{p.get('point')} → {shown} ({p.get('dispatcher')})"
        case EventType.GATE_CHECKED:
            verdict = "BLOCKED" if p.get("blocked") else "passed"
            scope = p.get("scope", "")
            task = f" {p['task_id']}" if p.get("task_id") else ""
            return f"gate {scope}{task}: {verdict}"
        case EventType.SHIP_RECORDED:
            return f"PR #{p.get('number')}: {p.get('state')} (ci: {p.get('ci_status') or '?'})"
        case EventType.SPEND_ADDED:
            task = f" on {p['task_id']}" if p.get("task_id") else ""
            return f"spend +${p.get('usd', 0):.2f}{task} (total ${p.get('running_total', 0):.2f})"
        case EventType.LEASE_CLAIMED:
            return f"lease claimed by {p.get('host')}:{p.get('pid')} ({p.get('reason')})"
        case EventType.LEASE_RELEASED:
            return "lease released"
        case EventType.LEASE_TAKEN_OVER:
            return f"lease taken over from {p.get('old_host')}:{p.get('old_pid')}"
        case EventType.HUMAN_APPROVED:
            return f"human approved {p.get('gate')}"
        case EventType.HUMAN_NOTE:
            return f"human note: {str(p.get('notes', ''))[:80]}"
    return event.type.value  # pragma: no cover - every type is matched above
