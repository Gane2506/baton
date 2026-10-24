"""Quality gate report persistence (`docs/quality.md`)."""

from __future__ import annotations

from pathlib import Path

from jev_agent.core.events import EventType, GateCheckedPayload
from jev_agent.quality.models import GateResult

from .base import StoreBase
from .errors import StoreError
from .records import QualityReportRecord
from .rows import _quality_record


class QualityMixin(StoreBase):
    """Quality gate reports."""

    def add_quality_report(
        self, run_id: str, result: GateResult, report_path: Path | None = None
    ) -> int:
        """Store one gate's result; returns its id."""
        with self._tx() as db:
            self._require_run(run_id)
            cur = db.execute(
                "INSERT INTO quality_reports (run_id, task_id, scope, number, level, blocked,"
                " result_json, report_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    result.task_id,
                    result.scope,
                    result.number,
                    result.level,
                    int(result.blocked),
                    result.model_dump_json(),
                    None if report_path is None else str(report_path),
                    self._clock(),
                ),
            )
            if cur.lastrowid is None:
                raise StoreError("quality report insert did not return an id")
            self._emit(
                db,
                run_id,
                EventType.GATE_CHECKED,
                GateCheckedPayload(
                    report_id=cur.lastrowid,
                    task_id=result.task_id,
                    scope=str(result.scope),
                    level=str(result.level),
                    blocked=result.blocked,
                    top_findings=result.top_findings(),
                ),
                parent_run_seq=self._latest_event_seq_locked(
                    db, run_id, EventType.ATTEMPT_FINISHED, task_id=result.task_id
                ),
            )
            return cur.lastrowid

    def list_quality_reports(self, run_id: str) -> list[QualityReportRecord]:
        """The run's gates, oldest first."""
        self._require_run(run_id)
        rows = self._conn.execute(
            "SELECT * FROM quality_reports WHERE run_id = ? ORDER BY id", (run_id,)
        )
        return [_quality_record(row) for row in rows]
