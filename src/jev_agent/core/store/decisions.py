"""Decision persistence: dispatcher calls and their answers."""

from __future__ import annotations

from jev_agent.core.events import DecisionRecordedPayload, EventType
from jev_agent.core.models import Decision, DecisionRequest

from .base import StoreBase
from .errors import StoreError
from .records import DecisionRecord
from .rows import _decision_record


class DecisionsMixin(StoreBase):
    """Recorded dispatcher decisions."""

    def record_decision(
        self,
        run_id: str,
        task_id: str | None,
        request: DecisionRequest,
        decision: Decision,
        fallback_reason: str | None = None,
        *,
        parent_run_seq: int | None = None,
        stage: str | None = None,
    ) -> int:
        """Store one dispatcher call; returns the decision id.

        `parent_run_seq` links the causal parent — normally the `decision.requested`
        event for this same decision, passed by the caller (`harness.decide`).
        `stage` is the pipeline stage active at the decision write, if any.
        """
        with self._tx() as db:
            self._require_run(run_id)
            if task_id is not None:
                self._require_task(run_id, task_id)
            cur = db.execute(
                "INSERT INTO decisions (run_id, task_id, point, request_json, decision_json,"
                " dispatcher, model, cost_usd, latency_ms, fallback_reason, created_at, stage)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    task_id,
                    request.point,
                    request.model_dump_json(),
                    decision.model_dump_json(),
                    decision.dispatcher,
                    decision.model,
                    decision.cost_usd,
                    decision.latency_ms,
                    fallback_reason,
                    self._clock(),
                    stage,
                ),
            )
            if cur.lastrowid is None:
                raise StoreError("decision insert did not return an id")
            self._emit(
                db,
                run_id,
                EventType.DECISION_RECORDED,
                DecisionRecordedPayload(
                    decision_id=cur.lastrowid,
                    point=str(request.point),
                    task_id=task_id,
                    answers=dict(decision.answers),
                    dispatcher=decision.dispatcher,
                    model=decision.model,
                    fallback_reason=fallback_reason,
                ),
                parent_run_seq=parent_run_seq,
            )
            return cur.lastrowid

    def list_decisions(self, run_id: str) -> list[DecisionRecord]:
        """All decisions of a run, oldest first."""
        self._require_run(run_id)
        rows = self._conn.execute("SELECT * FROM decisions WHERE run_id = ? ORDER BY id", (run_id,))
        return [_decision_record(row) for row in rows]
