"""Measurement, phase 1 (issue #368, section 11): stage rows per handler
call, attempt continuation across halt/resume, gate-wait vs work seconds,
the null-token display rule, full/partial sum rules, and shadow tagging.

Pipeline-driven tests use a fake clock on the store so gate waits are
exact, not wall-clock races.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from jev_agent.config import JevConfig
from jev_agent.core.models import Idea, RunState
from jev_agent.core.pipeline import build_pipeline
from jev_agent.core.store import Store
from jev_agent.dashboard.views import stage_token_summary
from jev_agent.workers.fake import FakeStep, FakeWorker
from jev_agent.workers.registry import WorkerRegistry


class FakeClock:
    """A clock the store reads, so gate waits are deterministic."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> str:
        return self.now.isoformat()

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _fake_pipeline(fixture_repo: Path, store: Store, script=None):
    """Fake workers registered under real names: JevConfig validates role
    workers against the known set, so the fakes take the real workers'
    names and the rules dispatcher picks them."""
    config = JevConfig.model_validate({"dispatcher": {"primary": "rules"}})
    fake = FakeWorker("fake", script or {})
    return build_pipeline(
        fixture_repo, config, store, WorkerRegistry({"claude_code": fake, "api": fake})
    )


async def _parked_run(fixture_repo: Path, store: Store, clock: FakeClock):
    """A run parked at the plan gate: INTAKE, PLANNING, VALIDATING rows
    written, the VALIDATING row still open."""
    pipeline = _fake_pipeline(fixture_repo, store)
    run = await pipeline.start(Idea(text="add a module", repo_path=fixture_repo))
    assert run.state is RunState.AWAITING_APPROVAL
    return pipeline, run


async def test_gate_wait_lives_outside_work_seconds(fixture_repo: Path) -> None:
    clock = FakeClock()
    with Store.open(fixture_repo / ".jev" / "state.db", clock=clock) as store:
        pipeline, run = await _parked_run(fixture_repo, store, clock)
        rows = {row.stage: row for row in store.list_stage_rows(run.id)}
        assert rows["INTAKE"].outcome == "success"
        assert rows["PLANNING"].outcome == "success"
        validating = rows["VALIDATING"]
        assert validating.ended_at is None  # still parked in the gate
        # The fake plan needs no design: a skipped DESIGNING row is written
        # immediately, with zero work and unknown tokens.
        skipped = rows["DESIGNING"]
        assert skipped.outcome == "skipped"
        assert skipped.work_seconds == 0
        assert skipped.tokens_in is None and skipped.tokens_out is None

        clock.advance(30)
        record = await pipeline.approve(run.id)
        assert record.state is RunState.DONE
        rows = {row.stage: row for row in store.list_stage_rows(run.id)}
        validating = rows["VALIDATING"]
        assert validating.outcome == "success"
        assert validating.gate_wait_seconds == 30
        assert validating.work_seconds == 0  # gate wait is never work
        assert rows["EXECUTING"].outcome == "success"
        assert rows["INTEGRATING"].outcome == "success"


async def test_attempt_numbers_continue_across_halt_and_resume(fixture_repo: Path) -> None:
    """A validator that always revises, with two plan rounds allowed: the
    run plans twice, then halts in VALIDATING, and the next PLANNING row
    after the halt is attempt 3 — never restarted at 1."""
    from jev_agent.core.models import ValidationResult

    clock = FakeClock()
    with Store.open(fixture_repo / ".jev" / "state.db", clock=clock) as store:
        config = JevConfig.model_validate(
            {"dispatcher": {"primary": "rules"}, "budget": {"max_plan_rounds": 2}}
        )
        fake = FakeWorker(
            "fake",
            {"validator": FakeStep(parsed=ValidationResult(verdict="revise", issues=["no"]))},
        )
        pipeline = build_pipeline(
            fixture_repo,
            config,
            store,
            WorkerRegistry({"claude_code": fake, "api": fake}),
        )
        run = await pipeline.start(Idea(text="add a module", repo_path=fixture_repo))
        assert run.state is RunState.NEEDS_HUMAN
        attempts = [row.attempt for row in store.list_stage_rows(run.id) if row.stage == "PLANNING"]
        assert attempts == [1, 2]
        halted = [row for row in store.list_stage_rows(run.id) if row.outcome == "halted"]
        assert halted and all(row.halt_reason for row in halted)
        # The next handler call after the halt continues the numbering.
        store.start_stage_row(run.id, "PLANNING")
        attempts = [row.attempt for row in store.list_stage_rows(run.id) if row.stage == "PLANNING"]
        assert attempts == [1, 2, 3]


async def test_cancel_closes_open_rows_as_halted_cancelled(fixture_repo: Path) -> None:
    from jev_agent.core.pipeline import cancel_run

    clock = FakeClock()
    with Store.open(fixture_repo / ".jev" / "state.db", clock=clock) as store:
        _, run = await _parked_run(fixture_repo, store, clock)
        cancel_run(store, run.id)
        rows = store.list_stage_rows(run.id)
        validating = next(row for row in rows if row.stage == "VALIDATING")
        assert validating.outcome == "halted"
        assert validating.halt_reason == "cancelled"


def test_store_stage_row_aggregation_null_and_partial(fixture_repo: Path) -> None:
    """Tokens sum over reported attempts only; an attempt that never
    finishes makes the row partial, and a row with nothing reported has
    NULL tokens — never zero."""
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        run = store.create_run(Idea(text="add a module", repo_path=fixture_repo), None, "none")
        row_id = store.start_stage_row(run.id, "PLANNING")
        store.start_attempt(run.id, None, 1, "coder", "fake", model="m", stage="PLANNING")
        finished = store.finish_stage_row(row_id, outcome="success")
        assert finished.completeness == "partial"
        assert finished.tokens_in is None and finished.tokens_out is None
        assert finished.cost_usd is None and finished.cost_reason == "unfinished-attempts"


def test_store_stage_row_full_sums(fixture_repo: Path) -> None:
    from jev_agent.core.models import WorkerResult

    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        run = store.create_run(Idea(text="add a module", repo_path=fixture_repo), None, "none")
        row_id = store.start_stage_row(run.id, "EXECUTING")
        attempt_id = store.start_attempt(
            run.id, None, 1, "coder", "fake", model="m", stage="EXECUTING"
        )
        store.finish_attempt(
            attempt_id,
            WorkerResult(
                status="success",
                summary="ok",
                tokens={"prompt": 100, "completion": 50},
                cost_usd=0.5,
                raw_log_path=Path("x.log"),
            ),
        )
        finished = store.finish_stage_row(row_id, outcome="success")
        assert finished.completeness == "full"
        assert (finished.tokens_in, finished.tokens_out, finished.cost_usd) == (100, 50, 0.5)


def test_shadow_tagged_by_stage_and_untagged_ignored(fixture_repo: Path) -> None:
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        run = store.create_run(Idea(text="add a module", repo_path=fixture_repo), None, "none")
        row_id = store.start_stage_row(run.id, "PLANNING")
        store.cache_shadow_log("abc", "exact", True, 1.0, stage="PLANNING", run_id=run.id)
        store.cache_shadow_log("def", "exact", True, 1.0)  # no stage: no row
        finished = store.finish_stage_row(row_id, outcome="success")
        assert finished.semantic_shadow_hits == 1


def test_null_tokens_never_displayed_as_zero() -> None:
    """stage_token_summary: (None, None, False) when nothing was
    reported — the templates render 'unknown', never 0."""
    from jev_agent.core.store.records import StageRecord

    def row(**kwargs) -> StageRecord:
        return StageRecord(
            id=1,
            run_id="r",
            stage="PLANNING",
            attempt=1,
            workflow_profile_id=None,
            workflow_profile_version=None,
            workflow_profile_release=None,
            goal=None,
            preset_goal=None,
            started_at=datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC),
            ended_at=datetime(2026, 10, 30, 12, 1, 0, tzinfo=UTC),
            work_seconds=60,
            gate_wait_seconds=0,
            tokens_in=kwargs.get("tokens_in"),
            tokens_out=kwargs.get("tokens_out"),
            cost_usd=None,
            cost_reason=None,
            cache_hits=0,
            cache_served=False,
            semantic_shadow_hits=0,
            outcome="success",
            halt_reason=None,
            defects_caught=0,
            effective_model=None,
            effective_level=None,
            completeness="full" if kwargs.get("tokens_in") is not None else "partial",
        )

    assert stage_token_summary([row()]) == (None, None, False)
    assert stage_token_summary([row(tokens_in=10, tokens_out=5)]) == (10, 5, True)
    # Partial: totals are lower bounds over reported rows only.
    assert stage_token_summary([row(tokens_in=10, tokens_out=5), row()]) == (10, 5, False)


async def test_gate_wait_banked_for_step_gate(fixture_repo: Path) -> None:
    """Approving a step gate with no open row banks the wait for the
    target step's row, instead of losing it."""
    clock = FakeClock()
    with Store.open(fixture_repo / ".jev" / "state.db", clock=clock) as store:
        pipeline = _fake_pipeline(fixture_repo, store)
        run = store.create_run(Idea(text="add a module", repo_path=fixture_repo), None, "none")
        assert store.set_run_state(run.id, RunState.AWAITING_PLANNING)
        clock.advance(5)
        await pipeline.approve(run.id, execute=False)
        assert store.take_pending_gate_wait(run.id, "PLANNING") == 5
