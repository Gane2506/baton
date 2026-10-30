"""Per-stage measurement rows (issue #368, design section 11).

One row per `_advance` handler call. The pipeline wrapper is the only writer
of stage rows, so a stage cannot run unrecorded by taking a different path;
decision and attempt writes carry the stage active at the write, and the
wrapper aggregates them into the row when it closes. Token and cost fields
stay NULL when unknown — NULL means unknown, never zero — and a row is
"partial" when any attempt in it reported no tokens.
"""

from __future__ import annotations

import json
from datetime import datetime

from .base import StoreBase
from .errors import NotFoundError
from .records import StageRecord
from .rows import _attempt_record, _aware_time, _decision_record, _stage_record, _task_record


def _tokens_of(result: object) -> tuple[int | None, int | None]:
    """(tokens_in, tokens_out) from a worker result's tokens dict, or Nones."""
    tokens = getattr(result, "tokens", None)
    if not isinstance(tokens, dict):
        return None, None
    prompt = tokens.get("prompt")
    completion = tokens.get("completion")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None, None
    return prompt, completion


class StagesMixin(StoreBase):
    """Stage records, pending gate waits, and the run's workflow snapshot."""

    def now_iso(self) -> str:
        """The store clock's current timestamp (pipeline gate math uses it so a
        controllable clock in tests measures gate waits deterministically)."""
        return self._clock()

    # ---- run snapshot columns ------------------------------------------------

    def set_run_workflow(
        self,
        run_id: str,
        *,
        workflow_profile_id: str | None,
        workflow_profile_version: int | None,
        workflow_profile_release: str | None,
        goal: str | None,
        preset_goal: str | None,
        assurance: str | None,
        assurance_absent: list[str],
        flow_snapshot: dict[str, object] | None,
    ) -> None:
        """Store the resolved snapshot on a run (design section 6.4). Called
        once, at creation, by the profile resolver's funnel."""
        with self._tx() as db:
            cur = db.execute(
                "UPDATE runs SET workflow_profile_id = ?, workflow_profile_version = ?,"
                " workflow_profile_release = ?, goal = ?, preset_goal = ?, assurance = ?,"
                " assurance_absent = ?, flow_snapshot = ?, updated_at = ? WHERE id = ?",
                (
                    workflow_profile_id,
                    workflow_profile_version,
                    workflow_profile_release,
                    goal,
                    preset_goal,
                    assurance,
                    json.dumps(assurance_absent),
                    None if flow_snapshot is None else json.dumps(flow_snapshot),
                    self._clock(),
                    run_id,
                ),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"run {run_id!r} not found")

    # ---- stage rows ------------------------------------------------------------

    def list_stage_rows(self, run_id: str) -> list[StageRecord]:
        """A run's stage rows, oldest first (attempt order within a stage)."""
        rows = self._conn.execute(
            "SELECT * FROM stage_records WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [_stage_record(row) for row in rows]

    def open_stage_row(self, run_id: str) -> StageRecord | None:
        """The run's still-open stage row, if any (a row kept open across the
        plan gate, whose gate wait has not ended yet)."""
        row = self._conn.execute(
            "SELECT * FROM stage_records WHERE run_id = ? AND ended_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return None if row is None else _stage_record(row)

    def start_stage_row(
        self,
        run_id: str,
        stage: str,
        *,
        workflow_profile_id: str | None = None,
        workflow_profile_version: int | None = None,
        workflow_profile_release: str | None = None,
        goal: str | None = None,
        preset_goal: str | None = None,
        gate_wait_seconds: float = 0.0,
    ) -> int:
        """Open a row for one handler call. Attempt numbers continue across
        Halt/resume: the next attempt is one past the rows already written for
        this stage of this run, never restarted at 1."""
        self._require_run(run_id)
        with self._tx() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM stage_records WHERE run_id = ? AND stage = ?",
                (run_id, stage),
            ).fetchone()
            cur = db.execute(
                "INSERT INTO stage_records (run_id, stage, attempt, workflow_profile_id,"
                " workflow_profile_version, workflow_profile_release, goal, preset_goal,"
                " started_at, gate_wait_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    stage,
                    int(row[0]) + 1,
                    workflow_profile_id,
                    workflow_profile_version,
                    workflow_profile_release,
                    goal,
                    preset_goal,
                    self._clock(),
                    gate_wait_seconds,
                ),
            )
            if cur.lastrowid is None:
                from .errors import StoreError

                raise StoreError("stage row insert did not return an id")
            return cur.lastrowid

    def write_skipped_stage_row(
        self,
        run_id: str,
        stage: str,
        *,
        workflow_profile_id: str | None = None,
        workflow_profile_version: int | None = None,
        workflow_profile_release: str | None = None,
        goal: str | None = None,
        preset_goal: str | None = None,
    ) -> int:
        """A row for a conditional stage that did not run (DESIGNING under
        `when_needed` for a non-UI run): zero work, no tokens, outcome skipped.
        Tokens stay NULL — a skipped stage is never displayed as zero."""
        self._require_run(run_id)
        now = self._clock()
        with self._tx() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM stage_records WHERE run_id = ? AND stage = ?",
                (run_id, stage),
            ).fetchone()
            cur = db.execute(
                "INSERT INTO stage_records (run_id, stage, attempt, workflow_profile_id,"
                " workflow_profile_version, workflow_profile_release, goal, preset_goal,"
                " started_at, ended_at, work_seconds, outcome, completeness)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'skipped', 'full')",
                (
                    run_id,
                    stage,
                    int(row[0]) + 1,
                    workflow_profile_id,
                    workflow_profile_version,
                    workflow_profile_release,
                    goal,
                    preset_goal,
                    now,
                    now,
                ),
            )
            if cur.lastrowid is None:
                from .errors import StoreError

                raise StoreError("stage row insert did not return an id")
            return cur.lastrowid

    def add_gate_wait_to_row(self, row_id: int, seconds: float) -> None:
        """Add parked gate seconds to an open row (the plan gate's wait belongs
        to the VALIDATING row, design section 11.2)."""
        with self._tx() as db:
            cur = db.execute(
                "UPDATE stage_records SET gate_wait_seconds = gate_wait_seconds + ?"
                " WHERE id = ? AND ended_at IS NULL",
                (max(seconds, 0.0), row_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"open stage row {row_id} not found")

    def finish_stage_row(
        self, row_id: int, *, outcome: str, halt_reason: str | None = None
    ) -> StageRecord:
        """Close a row, aggregating its tagged attempts, decisions, and shadow
        observations into the section 11.1 fields. Closing an already-closed
        row is a no-op returning the stored row."""
        with self._tx() as db:
            row = db.execute("SELECT * FROM stage_records WHERE id = ?", (row_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"stage row {row_id} not found")
            if row["ended_at"] is not None:
                return _stage_record(row)
            record = _stage_record(row)
            # Self-contained reads: a store mixin may only call StoreBase
            # methods, so stage aggregation queries its tables directly
            # instead of calling the runs/attempts/decisions/tasks mixins.
            run_row = db.execute(
                "SELECT plan_round, quality_level FROM runs WHERE id = ?",
                (record.run_id,),
            ).fetchone()
            if run_row is None:
                raise NotFoundError(f"run {record.run_id} not found")
            plan_round = int(run_row[0])
            quality_level = str(run_row[1])
            now = self._clock()
            started = record.started_at
            attempts = [
                a
                for a in (
                    _attempt_record(row)
                    for row in db.execute(
                        "SELECT * FROM attempts WHERE run_id = ? AND stage = ? ORDER BY id",
                        (record.run_id, record.stage),
                    )
                )
                if a.started_at >= started
            ]
            # Sum of reported tokens only; None when nothing reported at all
            # (a partial row's sum is a lower bound — "at least" — never zero
            # dressed as exact, design section 11.3).
            tokens_in: int | None = None
            tokens_out: int | None = None
            partial = False
            cost: float | None = 0.0
            cost_reason: str | None = None
            finished = [a for a in attempts if a.result is not None]
            if any(a.result is None for a in attempts):
                partial = True
            for attempt in finished:
                result = attempt.result
                if result is None:  # unreachable: finished holds results only
                    continue
                tin, tout = _tokens_of(result)
                if tin is None or tout is None:
                    partial = True
                else:
                    tokens_in = (tokens_in or 0) + tin
                    tokens_out = (tokens_out or 0) + tout
                if result.cost_usd is None:
                    cost = None
                    cost_reason = "unreported-cost"
                elif cost is not None:
                    cost += result.cost_usd
            if not finished:
                cost = None
                cost_reason = "unfinished-attempts" if attempts else "no-attempts"
            decisions = [
                d
                for d in (
                    _decision_record(row)
                    for row in db.execute(
                        "SELECT * FROM decisions WHERE run_id = ? AND stage = ? ORDER BY id",
                        (record.run_id, record.stage),
                    )
                )
                if d.created_at >= started
            ]
            cache_hits = sum(1 for d in decisions if d.decision.details.get("cache_hit"))
            cache_served = cache_hits > 0
            shadow = self._conn.execute(
                "SELECT COUNT(*) FROM cache_shadow WHERE run_id = ? AND stage = ?"
                " AND created_at >= ?",
                (record.run_id, record.stage, _iso_started(started)),
            ).fetchone()[0]
            if record.stage == "VALIDATING":
                defects = max(0, plan_round - 1)
            elif record.stage == "INTEGRATING":
                tasks = [
                    _task_record(row)
                    for row in db.execute(
                        "SELECT * FROM tasks WHERE run_id = ? ORDER BY position",
                        (record.run_id,),
                    )
                ]
                defects = len([t for t in tasks if t.card.kind == "fix"])
            else:
                defects = 0
            ended = _aware_time(now)
            work = max(0.0, (ended - started).total_seconds() - record.gate_wait_seconds)
            effective_model = finished[-1].model if finished else None
            db.execute(
                "UPDATE stage_records SET ended_at = ?, work_seconds = ?, tokens_in = ?,"
                " tokens_out = ?, cost_usd = ?, cost_reason = ?, cache_hits = ?,"
                " cache_served = ?, semantic_shadow_hits = ?, outcome = ?, halt_reason = ?,"
                " defects_caught = ?, effective_model = ?, effective_level = ?,"
                " completeness = ? WHERE id = ?",
                (
                    now,
                    work,
                    tokens_in,
                    tokens_out,
                    cost,
                    cost_reason,
                    cache_hits,
                    int(cache_served),
                    int(shadow),
                    outcome,
                    halt_reason,
                    defects,
                    effective_model,
                    quality_level,
                    "partial" if partial else "full",
                    row_id,
                ),
            )
        row = self._conn.execute("SELECT * FROM stage_records WHERE id = ?", (row_id,)).fetchone()
        return _stage_record(row)

    def close_open_stage_rows(self, run_id: str, *, reason: str) -> int:
        """Close every open row of a run as halted with the reason (cancel:
        design section 11.2 — outcome `halted`, reason `cancelled`)."""
        count = 0
        for row in self.list_stage_rows(run_id):
            if row.ended_at is None:
                self.finish_stage_row(row.id, outcome="halted", halt_reason=reason)
                count += 1
        return count

    # ---- pending gate waits ------------------------------------------------------

    def add_pending_gate_wait(self, run_id: str, stage: str, seconds: float) -> None:
        """Bank a gate wait for a step whose row does not exist yet (the gate
        parks before the step runs); the wrapper consumes it at row open."""
        with self._tx() as db:
            db.execute(
                "INSERT INTO pending_gate_waits (run_id, stage, seconds) VALUES (?, ?, ?)"
                " ON CONFLICT (run_id, stage) DO UPDATE SET seconds = seconds + excluded.seconds",
                (run_id, stage, max(seconds, 0.0)),
            )

    def take_pending_gate_wait(self, run_id: str, stage: str) -> float:
        """Consume and return the banked gate wait for a step (0 when none)."""
        with self._tx() as db:
            row = db.execute(
                "SELECT seconds FROM pending_gate_waits WHERE run_id = ? AND stage = ?",
                (run_id, stage),
            ).fetchone()
            if row is None:
                return 0.0
            db.execute(
                "DELETE FROM pending_gate_waits WHERE run_id = ? AND stage = ?", (run_id, stage)
            )
            return float(row["seconds"])

    def clear_pending_gate_wait(self, run_id: str, stage: str) -> None:
        """Drop a banked wait that will never be consumed (a ship decision
        abandoned by modify/stop instead of approval)."""
        with self._tx() as db:
            db.execute(
                "DELETE FROM pending_gate_waits WHERE run_id = ? AND stage = ?", (run_id, stage)
            )


def _iso_started(started: datetime) -> str:
    """A stage row's start as a stored-timestamp string for comparisons."""
    from .rows import _iso

    return _iso(started) or ""
