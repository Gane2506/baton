"""Backfill synthetic events for runs that predate the event log (docs/events.md).

`backfill_run` synthesizes events from the snapshot tables (`runs`, `tasks`,
`attempts`, `decisions`, `quality_reports`), ordered by their timestamps. Every
synthesized event has `synthetic=true` and no causal links: the snapshots record
*what is*, not *what happened*, so intermediate transitions (a task's
PENDING → RUNNING → DONE path, plan round 1's cards) are unrecoverable and are
not invented. Backfilled runs support timeline viewing and `--mark` bisect;
verify replay treats synthetic logs as approximate (see `core/replay.py`).

Backfill refuses a run that already has events: it is all-or-nothing per run,
so a second run never duplicates.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from jev_agent.core.events import (
    ACTOR_PIPELINE,
    AttemptFinishedPayload,
    AttemptStartedPayload,
    DecisionRecordedPayload,
    DecisionRequestedPayload,
    DesignSetPayload,
    EventType,
    GateCheckedPayload,
    PlanSetPayload,
    RunCreatedPayload,
    RunStateChangedPayload,
    TaskAssignedPayload,
    TaskStateChangedPayload,
    TasksUpsertedPayload,
    ValidationRecordedPayload,
)
from jev_agent.core.models import RunState, TaskState
from jev_agent.core.store import Store

# Tie-break order for events that share a timestamp: the order the pipeline
# would have emitted them in.
_TYPE_ORDER: dict[EventType, int] = {
    EventType.RUN_CREATED: 0,
    EventType.PLAN_SET: 10,
    EventType.DESIGN_SET: 11,
    EventType.VALIDATION_RECORDED: 12,
    EventType.TASKS_UPSERTED: 20,
    EventType.TASK_ASSIGNED: 20,
    EventType.TASK_STATE_CHANGED: 21,
    EventType.ATTEMPT_STARTED: 30,
    EventType.ATTEMPT_FINISHED: 31,
    EventType.DECISION_REQUESTED: 40,
    EventType.DECISION_RECORDED: 41,
    EventType.GATE_CHECKED: 50,
    EventType.RUN_STATE_CHANGED: 90,
}


class BackfillError(RuntimeError):
    """The run cannot be backfilled (it already has events)."""


def _candidates(store: Store, run_id: str) -> list[tuple[datetime, int, EventType, BaseModel]]:
    """(timestamp, order, type, payload) for every synthesizable event of the run."""
    run = store.get_run(run_id)
    created = run.created_at
    out: list[tuple[datetime, int, EventType, BaseModel]] = [
        (
            created,
            _TYPE_ORDER[EventType.RUN_CREATED],
            EventType.RUN_CREATED,
            RunCreatedPayload(
                idea_text=run.idea.text,
                quality_level=str(run.quality_level),
                plan_only=run.plan_only,
                ship_mode=None if run.ship_mode is None else str(run.ship_mode),
                autonomy_mode=run.autonomy_mode,
            ),
        )
    ]
    if run.plan is not None:
        out.append(
            (
                created,
                _TYPE_ORDER[EventType.PLAN_SET],
                EventType.PLAN_SET,
                PlanSetPayload(round=run.plan_round, plan=run.plan.model_dump(mode="json")),
            )
        )
    tasks = store.list_tasks(run_id)
    if tasks:
        out.append(
            (
                created,
                _TYPE_ORDER[EventType.TASKS_UPSERTED],
                EventType.TASKS_UPSERTED,
                TasksUpsertedPayload(
                    task_ids=[t.id for t in tasks], from_plan_round=run.plan_round
                ),
            )
        )
    for task in tasks:
        if task.worker is not None:
            # Approximate: only the latest assignment survives in the snapshot.
            out.append(
                (
                    created,
                    _TYPE_ORDER[EventType.TASK_ASSIGNED],
                    EventType.TASK_ASSIGNED,
                    TaskAssignedPayload(
                        task_id=task.id,
                        state=str(task.state),
                        worker=task.worker,
                        model=task.model,
                        effort=task.effort,
                        decision_id=None,
                    ),
                )
            )
        if task.state != TaskState.PENDING:
            # Approximate: only the current state survives in the snapshot.
            out.append(
                (
                    created,
                    _TYPE_ORDER[EventType.TASK_STATE_CHANGED],
                    EventType.TASK_STATE_CHANGED,
                    TaskStateChangedPayload(
                        task_id=task.id,
                        from_state=str(TaskState.PENDING),
                        to_state=str(task.state),
                        note=task.note,
                    ),
                )
            )
    if run.design is not None:
        out.append(
            (
                created,
                _TYPE_ORDER[EventType.DESIGN_SET],
                EventType.DESIGN_SET,
                DesignSetPayload(design=run.design.model_dump(mode="json")),
            )
        )
    if run.validation is not None:
        out.append(
            (
                created,
                _TYPE_ORDER[EventType.VALIDATION_RECORDED],
                EventType.VALIDATION_RECORDED,
                ValidationRecordedPayload(
                    verdict=str(run.validation.verdict),
                    issues=list(run.validation.issues),
                    validation=run.validation.model_dump(mode="json"),
                ),
            )
        )
    for attempt in store.list_attempts(run_id):
        out.append(
            (
                attempt.started_at,
                _TYPE_ORDER[EventType.ATTEMPT_STARTED],
                EventType.ATTEMPT_STARTED,
                AttemptStartedPayload(
                    attempt_id=attempt.id,
                    task_id=attempt.task_id,
                    attempt_no=attempt.attempt_no,
                    role=str(attempt.role),
                    worker=attempt.worker,
                    model=attempt.model,
                    lesson_ids=list(attempt.lesson_ids),
                    log_path=None if attempt.log_path is None else str(attempt.log_path),
                ),
            )
        )
        if attempt.finished_at is not None and attempt.result is not None:
            duration_s = (attempt.finished_at - attempt.started_at).total_seconds()
            out.append(
                (
                    attempt.finished_at,
                    _TYPE_ORDER[EventType.ATTEMPT_FINISHED],
                    EventType.ATTEMPT_FINISHED,
                    AttemptFinishedPayload(
                        attempt_id=attempt.id,
                        task_id=attempt.task_id,
                        status=str(attempt.result.status),
                        summary=attempt.result.summary,
                        cost_usd=attempt.result.cost_usd,
                        duration_s=duration_s,
                    ),
                )
            )
    for decision in store.list_decisions(run_id):
        out.append(
            (
                decision.created_at,
                _TYPE_ORDER[EventType.DECISION_REQUESTED],
                EventType.DECISION_REQUESTED,
                DecisionRequestedPayload(
                    point=str(decision.request.point),
                    task_id=decision.task_id,
                    questions=decision.request.questions,
                ),
            )
        )
        out.append(
            (
                decision.created_at,
                _TYPE_ORDER[EventType.DECISION_RECORDED],
                EventType.DECISION_RECORDED,
                DecisionRecordedPayload(
                    decision_id=decision.id,
                    point=str(decision.request.point),
                    task_id=decision.task_id,
                    answers=dict(decision.decision.answers),
                    dispatcher=decision.decision.dispatcher,
                    model=decision.decision.model,
                    fallback_reason=decision.fallback_reason,
                ),
            )
        )
    for report in store.list_quality_reports(run_id):
        out.append(
            (
                report.created_at,
                _TYPE_ORDER[EventType.GATE_CHECKED],
                EventType.GATE_CHECKED,
                GateCheckedPayload(
                    report_id=report.id,
                    task_id=report.task_id,
                    scope=str(report.result.scope),
                    level=str(report.result.level),
                    blocked=report.result.blocked,
                    top_findings=report.result.top_findings(),
                ),
            )
        )
    if run.state != RunState.INTAKE:
        # Approximate: only the current state survives in the snapshot.
        out.append(
            (
                run.updated_at,
                _TYPE_ORDER[EventType.RUN_STATE_CHANGED],
                EventType.RUN_STATE_CHANGED,
                RunStateChangedPayload(
                    from_state=str(RunState.INTAKE),
                    to_state=str(run.state),
                    reason=run.reason,
                    resume_state=None if run.resume_state is None else str(run.resume_state),
                ),
            )
        )
    return out


def backfill_run(store: Store, run_id: str) -> int:
    """Synthesize the run's event history from its snapshot tables.

    Returns the number of events created. Raises `NotFoundError` for an unknown
    run and `BackfillError` when the run already has events (backfill is
    all-or-nothing per run: a second run would duplicate).
    """
    store.get_run(run_id)  # raises NotFoundError for an unknown run
    if store.list_events(run_id, limit=1):
        raise BackfillError(
            f"run {run_id} already has events; backfill runs once per run and never"
            " mixes synthetic events into a live log"
        )
    if not store.events_enabled:
        raise BackfillError("event log disabled for this repo ([events] enabled = false)")
    candidates = sorted(_candidates(store, run_id), key=lambda c: (c[0], c[1]))
    count = 0
    for _, _, event_type, payload in candidates:
        store.emit(run_id, event_type, payload, actor=ACTOR_PIPELINE, synthetic=True)
        count += 1
    return count  # at least run.created: a known run always yields events
