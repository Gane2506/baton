"""Run persistence: create, read, update state/plan/validation/spend."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection
from dataclasses import asdict

from jev_agent.config.schema import ShipMode
from jev_agent.core.events import (
    ACTOR_PIPELINE,
    DesignSetPayload,
    EventType,
    PlanSetPayload,
    RunCreatedPayload,
    RunStateChangedPayload,
    SpendAddedPayload,
    ValidationRecordedPayload,
)
from jev_agent.core.models import DesignDoc, Idea, Plan, RunState, ValidationResult
from jev_agent.quality.levels import QualityLevel, parse_level

from .base import (
    _RUN_UPDATE_COLUMNS,
    StoreBase,
    _check_update_assignments,
    _is_runs_id_collision,
    new_run_id,
)
from .errors import NotFoundError, StoreError
from .records import LearningCounts, RunRecord
from .rows import _learning_counts, _run_record


class RunsMixin(StoreBase):
    """Runs and their snapshot columns."""

    def create_run(
        self,
        idea: Idea,
        learning_profile: str | None = None,
        quality_level: QualityLevel = "none",
        *,
        plan_only: bool = False,
        ship_mode: ShipMode | None = None,
        autonomy_mode: str = "standard",
        autonomous_opt_in: bool = False,
        medium_folder: str | None = None,
        run_id: str | None = None,
        design_system: str | None = None,
    ) -> RunRecord:
        now = self._clock()
        if run_id is not None:
            # An explicitly chosen id is used as-is: retrying the same id can never
            # succeed, and any other constraint failure is the caller's bug, not a
            # collision. Fail fast with a clear message either way.
            try:
                return self._insert_run(
                    run_id,
                    idea,
                    now,
                    learning_profile,
                    quality_level,
                    plan_only,
                    ship_mode,
                    autonomy_mode,
                    autonomous_opt_in,
                    medium_folder,
                    design_system,
                )
            except sqlite3.IntegrityError as exc:
                if _is_runs_id_collision(exc):
                    raise StoreError(f"run {run_id!r} already exists") from exc
                raise StoreError(f"could not create run: {exc}") from exc
        for _ in range(5):  # retry on the (very unlikely) generated-id collision
            candidate = new_run_id()
            try:
                return self._insert_run(
                    candidate,
                    idea,
                    now,
                    learning_profile,
                    quality_level,
                    plan_only,
                    ship_mode,
                    autonomy_mode,
                    autonomous_opt_in,
                    medium_folder,
                    design_system,
                )
            except sqlite3.IntegrityError as exc:
                # Only a PK/UNIQUE collision on runs.id is worth retrying; anything
                # else (a CHECK violation, a NOT NULL, ...) is a real bug that would
                # fail identically five times.
                if not _is_runs_id_collision(exc):
                    raise StoreError(f"could not create run: {exc}") from exc
        raise StoreError("could not generate a unique run id")

    def _insert_run(
        self,
        run_id: str,
        idea: Idea,
        now: str,
        learning_profile: str | None,
        quality_level: QualityLevel,
        plan_only: bool,
        ship_mode: ShipMode | None,
        autonomy_mode: str,
        autonomous_opt_in: bool,
        medium_folder: str | None,
        design_system: str | None = None,
    ) -> RunRecord:
        """Insert one run row plus its RUN_CREATED event; raise on any constraint."""
        with self._tx() as db:
            db.execute(
                "INSERT INTO runs (id, idea_json, state, created_at, updated_at,"
                " learning_profile, quality_level, plan_only, ship_mode,"
                " autonomy_mode, autonomous_opt_in, medium_folder, design_system)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    idea.model_dump_json(),
                    RunState.INTAKE,
                    now,
                    now,
                    learning_profile,
                    quality_level,
                    int(plan_only),
                    ship_mode,
                    autonomy_mode,
                    int(autonomous_opt_in),
                    medium_folder,
                    design_system,
                ),
            )
            self._emit(
                db,
                run_id,
                EventType.RUN_CREATED,
                RunCreatedPayload(
                    idea_text=idea.text,
                    quality_level=str(quality_level),
                    plan_only=plan_only,
                    ship_mode=None if ship_mode is None else str(ship_mode),
                    autonomy_mode=autonomy_mode,
                ),
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> RunRecord:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"run {run_id!r} not found")
        return _run_record(row)

    def list_runs(self) -> list[RunRecord]:
        """All runs, newest first."""
        rows = self._conn.execute("SELECT * FROM runs ORDER BY created_at DESC, rowid DESC")
        return [_run_record(row) for row in rows]

    def set_run_state(
        self,
        run_id: str,
        state: RunState,
        reason: str | None = None,
        resume_state: RunState | None = None,
        *,
        unless: Collection[RunState] = (),
        actor: str = ACTOR_PIPELINE,
    ) -> bool:
        """Set the run's state. `reason` (why it stopped) replaces any previous reason.

        `resume_state` is where `resume` may continue a stopped run from; every state change
        replaces it, so it is only set while the run stays stopped.

        With `unless`, the change happens only if the run is not in one of those states, in the
        same statement, so a concurrent write (`jev cancel` from another shell, the dashboard)
        cannot be overwritten between a check and the update. Returns whether it changed.

        `actor` records who made the change in the event log (`jev cancel` passes "human").
        """
        guard = ""
        if unless:
            guard = f" AND state NOT IN ({', '.join('?' for _ in unless)})"
        with self._tx() as db:
            row = db.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"run {run_id!r} not found")
            from_state = str(row["state"])
            cur = db.execute(
                "UPDATE runs SET state = ?, reason = ?, resume_state = ?, updated_at = ?"
                f" WHERE id = ?{guard}",
                (state, reason, resume_state, self._clock(), run_id, *unless),
            )
            if cur.rowcount == 0:
                return False
            self._emit(
                db,
                run_id,
                EventType.RUN_STATE_CHANGED,
                RunStateChangedPayload(
                    from_state=from_state,
                    to_state=str(state),
                    reason=reason,
                    resume_state=None if resume_state is None else str(resume_state),
                ),
                actor=actor,
            )
        return True

    def set_run_validation(self, run_id: str, validation: ValidationResult | None) -> None:
        value = None if validation is None else validation.model_dump_json()
        with self._tx() as db:
            cur = db.execute(
                "UPDATE runs SET validation_json = ?, updated_at = ? WHERE id = ?",
                (value, self._clock(), run_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"run {run_id!r} not found")
            if validation is not None:
                self._emit(
                    db,
                    run_id,
                    EventType.VALIDATION_RECORDED,
                    ValidationRecordedPayload(
                        verdict=str(validation.verdict),
                        issues=list(validation.issues),
                        validation=validation.model_dump(mode="json"),
                    ),
                )

    def set_run_plan(self, run_id: str, plan: Plan, plan_round: int) -> None:
        with self._tx() as db:
            cur = db.execute(
                "UPDATE runs SET plan_json = ?, plan_round = ?, updated_at = ? WHERE id = ?",
                (plan.model_dump_json(), plan_round, self._clock(), run_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"run {run_id!r} not found")
            self._emit(
                db,
                run_id,
                EventType.PLAN_SET,
                PlanSetPayload(round=plan_round, plan=plan.model_dump(mode="json")),
            )

    def set_run_design(self, run_id: str, design: DesignDoc) -> None:
        """Persist the designer's design doc for the run (the DESIGNING pipeline stage)."""
        with self._tx() as db:
            cur = db.execute(
                "UPDATE runs SET design_json = ?, updated_at = ? WHERE id = ?",
                (design.model_dump_json(), self._clock(), run_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"run {run_id!r} not found")
            self._emit(
                db,
                run_id,
                EventType.DESIGN_SET,
                DesignSetPayload(design=design.model_dump(mode="json")),
            )

    def set_run_learning(self, run_id: str, profile: str | None) -> None:
        """Change the learning profile the run uses from now on (None: learning off)."""
        self._update_run(run_id, "learning_profile = ?", (profile,))

    def set_run_learning_outcome(self, run_id: str, outcome: str | None) -> None:
        self._update_run(run_id, "learning_outcome = ?", (outcome,))

    def add_run_learning_counts(self, run_id: str, counts: LearningCounts) -> LearningCounts:
        """Add a learning step's lesson changes to the run's totals; returns the new totals."""
        with self._tx() as db:
            row = db.execute(
                "SELECT learning_counts_json FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"run {run_id} not found")
            old = _learning_counts(row["learning_counts_json"])
            total = counts if old is None else old + counts
            db.execute(
                "UPDATE runs SET learning_counts_json = ? WHERE id = ?",
                (json.dumps(asdict(total)), run_id),
            )
        return total

    def set_run_quality(self, run_id: str, level: QualityLevel) -> None:
        """Change the quality level the run uses from now on (`jev resume --quality`)."""
        self._update_run(run_id, "quality_level = ?", (parse_level(level),))

    def add_run_spend(self, run_id: str, usd: float) -> None:
        self._update_run(run_id, "spend_usd = spend_usd + ?", (usd,))

    def add_spend(self, run_id: str, task_id: str | None, usd: float) -> None:
        """Add `usd` to the run total and, if `task_id` is given, to that task, atomically.

        Emits `spend.added` with the running total. Spend is the noisiest event source,
        so only this method emits: per-attempt and per-decision costs, never streaming
        updates (`add_run_spend` stays snapshot-only).
        """
        with self._tx() as db:
            cur = db.execute(
                "UPDATE runs SET spend_usd = spend_usd + ?, updated_at = ? WHERE id = ?",
                (usd, self._clock(), run_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"run {run_id!r} not found")
            if task_id is not None:
                cur = db.execute(
                    "UPDATE tasks SET spend_usd = spend_usd + ? WHERE run_id = ? AND id = ?",
                    (usd, run_id, task_id),
                )
                if cur.rowcount == 0:
                    raise NotFoundError(f"task {task_id!r} not found in run {run_id!r}")
            total = db.execute("SELECT spend_usd FROM runs WHERE id = ?", (run_id,)).fetchone()[
                "spend_usd"
            ]
            self._emit(
                db,
                run_id,
                EventType.SPEND_ADDED,
                SpendAddedPayload(usd=usd, task_id=task_id, running_total=float(total)),
            )

    def _update_run(self, run_id: str, assignments: str, params: tuple[object, ...]) -> None:
        _check_update_assignments(assignments, _RUN_UPDATE_COLUMNS)
        with self._tx() as db:
            cur = db.execute(
                f"UPDATE runs SET {assignments}, updated_at = ? WHERE id = ?",
                (*params, self._clock(), run_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"run {run_id!r} not found")
