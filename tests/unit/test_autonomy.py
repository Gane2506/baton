"""Autonomy modes (Phase 2, workstream C1): the mode enum, run config validation, and the
per-step gate mapping the pipeline dispatches on. No network; the store tests use a real
SQLite database in the fixture repo."""

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from jev_agent.config import JevConfig
from jev_agent.core.models import AutonomyConfig, AutonomyMode, Idea, RunState
from jev_agent.core.pipeline import (
    APPROVAL_GATES,
    STEP_GATES,
    build_pipeline,
    execution_gate_target,
    run_autonomy_mode,
    step_gate_target,
)
from jev_agent.core.store import Store
from jev_agent.workers.registry import WorkerRegistry

# `Pipeline.start` with a non-default autonomy mode records it through new
# `Store.create_run` arguments, which land with migration 14 (see the C1 report).
_STORE_HAS_AUTONOMY = "autonomy_mode" in inspect.signature(Store.create_run).parameters
_NEEDS_MIGRATION_14 = pytest.mark.skipif(
    not _STORE_HAS_AUTONOMY, reason="needs migration 14 (autonomy columns)"
)


def test_autonomy_mode_values() -> None:
    assert [m.value for m in AutonomyMode] == [
        "standard",
        "autonomous",
        "ask_every_step",
        "manual",
        "medium",
    ]
    assert AutonomyMode("autonomous") is AutonomyMode.AUTONOMOUS


def test_standard_is_the_default_mode() -> None:
    config = AutonomyConfig()
    assert config.mode is AutonomyMode.STANDARD
    assert config.is_default()


def test_autonomous_requires_explicit_opt_in() -> None:
    with pytest.raises(ValidationError, match="explicit opt-in"):
        AutonomyConfig(mode=AutonomyMode.AUTONOMOUS)
    opted = AutonomyConfig(mode=AutonomyMode.AUTONOMOUS, autonomous_opt_in=True)
    assert opted.mode is AutonomyMode.AUTONOMOUS
    assert not opted.is_default()


def test_medium_requires_a_folder() -> None:
    with pytest.raises(ValidationError, match="scoped folder"):
        AutonomyConfig(mode=AutonomyMode.MEDIUM)
    with pytest.raises(ValidationError, match="scoped folder"):
        AutonomyConfig(mode=AutonomyMode.MEDIUM, medium_folder="   ")
    scoped = AutonomyConfig(mode=AutonomyMode.MEDIUM, medium_folder="src/jev_agent")
    assert scoped.medium_folder == "src/jev_agent"
    assert not scoped.is_default()


def test_step_gates_are_explicit_states() -> None:
    assert STEP_GATES == {
        RunState.PLANNING: RunState.AWAITING_PLANNING,
        RunState.VALIDATING: RunState.AWAITING_VALIDATING,
        RunState.DESIGNING: RunState.AWAITING_DESIGNING,
        RunState.EXECUTING: RunState.AWAITING_APPROVAL,
        RunState.INTEGRATING: RunState.AWAITING_INTEGRATING,
        RunState.SHIPPING: RunState.AWAITING_SHIPPING,
    }
    assert frozenset(STEP_GATES.values()) == APPROVAL_GATES
    # The richer ship gate (approve/modify/stop) stays separate from the per-step gates.
    assert RunState.AWAITING_SHIP not in APPROVAL_GATES


def test_step_gate_target_gates_every_step_in_ask_every_step() -> None:
    for step, gate in STEP_GATES.items():
        assert step_gate_target(AutonomyMode.ASK_EVERY_STEP, step) is gate


def test_step_gate_target_leaves_other_modes_untouched() -> None:
    for mode in (
        AutonomyMode.STANDARD,
        AutonomyMode.AUTONOMOUS,
        AutonomyMode.MANUAL,
        AutonomyMode.MEDIUM,
    ):
        for step in STEP_GATES:
            assert step_gate_target(mode, step) is step


def test_execution_gate_target() -> None:
    # AUTONOMOUS skips the plan-approval gate, even when it would otherwise apply.
    assert execution_gate_target(AutonomyMode.AUTONOMOUS, True) is RunState.EXECUTING
    assert execution_gate_target(AutonomyMode.AUTONOMOUS, False) is RunState.EXECUTING
    # ASK_EVERY_STEP always puts the plan behind the gate.
    assert execution_gate_target(AutonomyMode.ASK_EVERY_STEP, False) is RunState.AWAITING_APPROVAL
    assert execution_gate_target(AutonomyMode.ASK_EVERY_STEP, True) is RunState.AWAITING_APPROVAL
    # Otherwise the historical rule: plan_only or [approval] require_plan_approval.
    assert execution_gate_target(AutonomyMode.STANDARD, True) is RunState.AWAITING_APPROVAL
    assert execution_gate_target(AutonomyMode.STANDARD, False) is RunState.EXECUTING
    assert execution_gate_target(AutonomyMode.MEDIUM, True) is RunState.AWAITING_APPROVAL
    assert execution_gate_target(AutonomyMode.MANUAL, False) is RunState.EXECUTING


def _record_like(**attrs: Any) -> Any:
    """A stand-in for a run record, for the pre-migration-14 read path."""
    return SimpleNamespace(**attrs)


def test_run_autonomy_mode_defaults_to_standard(fixture_repo: Path) -> None:
    # A run written before migration 14 has no autonomy_mode attribute.
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        run = store.create_run(Idea(text="add a module", repo_path=fixture_repo), None, "none")
        assert run_autonomy_mode(run) is AutonomyMode.STANDARD


def test_run_autonomy_mode_reads_the_recorded_mode() -> None:
    assert run_autonomy_mode(_record_like(autonomy_mode=AutonomyMode.MANUAL)) is AutonomyMode.MANUAL
    assert (
        run_autonomy_mode(_record_like(autonomy_mode="ask_every_step"))
        is AutonomyMode.ASK_EVERY_STEP
    )


@_NEEDS_MIGRATION_14
async def test_manual_start_creates_but_does_not_advance(fixture_repo: Path) -> None:
    """MANUAL is chat-driven: `start` records the run and waits in INTAKE; an explicit
    instruction (`advance`/`resume`, or the chat driver) moves it."""
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        pipeline = build_pipeline(fixture_repo, JevConfig(), store, WorkerRegistry({}))
        run = await pipeline.start(
            Idea(text="add a module", repo_path=fixture_repo),
            autonomy_mode=AutonomyMode.MANUAL,
        )
        assert run.state is RunState.INTAKE
        assert store.get_run(run.id).state is RunState.INTAKE


async def test_approve_passes_each_per_step_gate(fixture_repo: Path) -> None:
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        pipeline = build_pipeline(fixture_repo, JevConfig(), store, WorkerRegistry({}))
        run = store.create_run(Idea(text="add a module", repo_path=fixture_repo), None, "none")
        for step, gate in STEP_GATES.items():
            assert store.set_run_state(run.id, gate)
            record = await pipeline.approve(run.id, execute=False)
            assert record.state is step, gate
