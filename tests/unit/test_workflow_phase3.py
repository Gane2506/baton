"""Workflow profiles, phase 3 (issue #368): the full validator catalogue,
the store-wide re-check, the snapshot-aware pipeline (step lookup, gate
policies, every section 8.1 move-site check, snapshot-only settings), the
`default`-preset parameter set, and the `[workflows]` flags.

Phase 3's conformance comparison (legacy path vs. `default` preset path)
lives in tests/conformance/test_conformance_comparison.py; this file covers
the units that comparison does not isolate.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jev_agent.app.context import pipeline_settings
from jev_agent.app.errors import JevError
from jev_agent.app.runs import create_run
from jev_agent.config import JevConfig
from jev_agent.core.assign import Assignment
from jev_agent.core.commands import FakeCommandRunner
from jev_agent.core.models import (
    AutonomyMode,
    Decision,
    DecisionRequest,
    Idea,
    Plan,
    RunState,
    TaskCard,
    ValidationResult,
)
from jev_agent.core.pipeline import (
    Pipeline,
    PipelineError,
    build_pipeline,
    cancel_run,
    execution_gate_target,
    step_gate_target,
)
from jev_agent.core.store import Store
from jev_agent.core.workflow_profiles import (
    ProfileStore,
    build_default_snapshot,
    default_via_profile_enabled,
    validate_profile,
    workflows_enabled,
)
from jev_agent.core.workflow_profiles.validator import ValidationContext
from jev_agent.dispatch.rules import RulesDispatcher
from jev_agent.roles.schemas import QualityReview
from jev_agent.workers.base import WorkerJob
from jev_agent.workers.fake import FakeStep, FakeWorker
from jev_agent.workers.registry import WorkerRegistry

FULL_STAGES = ["INTAKE", "PLANNING", "VALIDATING", "DESIGNING", "EXECUTING", "INTEGRATING"]
FULL_GATES = {
    "PLANNING": "inherit",
    "VALIDATING": "inherit",
    "DESIGNING": "inherit",
    "EXECUTING": "inherit",
    "INTEGRATING": "inherit",
}


def base_profile(**overrides: Any) -> dict[str, Any]:
    profile = {
        "id": "repo:fast",
        "display_name": "Fast",
        "scope": "repo",
        "stage_list": list(FULL_STAGES),
        "gate_policies": dict(FULL_GATES),
        "designing": "when_needed",
        "ship_enabled": False,
    }
    profile.update(overrides)
    return profile


CTX = ValidationContext(
    models_by_role={"planner": ["model-a", "model-b"], "coder": ["model-c"]},
    learning_profiles={"default", "other"},
    ceilings={"max_plan_rounds": 3, "max_fix_rounds": 2, "score_max_retries": 2},
)


# ---- validator: the full-catalogue entries phase 2 did not carry -------------


def test_drawn_cycle_refused_with_catalogue_message() -> None:
    """Section 7.5's drawn-cycle entry: in the stage-list format a cycle can
    only appear as a revisited stage, so it carries the cycle message (as
    well as the duplicate message)."""
    errors = validate_profile(base_profile(stage_list=[*FULL_STAGES, "PLANNING", "VALIDATING"]))
    assert any(
        "Flows do not loop by drawing. Set the round limit on the step instead." in e
        for e in errors
    )


def test_unknown_learning_profile_lists_allowed_values() -> None:
    """Section 7.5: an unknown name is refused with the allowed values listed
    and the fix stated — the phase-2 message named no values."""
    errors = validate_profile(base_profile(learning_profile="ghost"), context=CTX)
    assert any(
        "Unknown learning profile 'ghost'" in e and "default" in e and "other" in e for e in errors
    )


def test_unknown_ship_setting_refused_with_allowed_values() -> None:
    errors = validate_profile(base_profile(after_approval="whenever"))
    assert any(
        "Unknown after_approval 'whenever'" in e and "merge" in e and "ask" in e for e in errors
    )


def test_reordered_pair_other_than_execution_validation_named() -> None:
    errors = validate_profile(
        base_profile(
            stage_list=["INTAKE", "VALIDATING", "PLANNING", "DESIGNING", "EXECUTING", "INTEGRATING"]
        )
    )
    assert any("validation" in e and "planning" in e for e in errors)


# ---- store-wide re-check -------------------------------------------------------


def test_recheck_all_reports_stored_and_archived_failures(tmp_path: Path) -> None:
    """Phase 3 re-checks every artefact stored under phase 2 (stored profiles
    and archived versions) against the full catalogue, naming the setting.
    Files written directly to disk — the shape an on-disk edit between save
    and creation takes — are reported, not silently skipped."""
    store = ProfileStore(tmp_path, tmp_path / "user")
    good = store.save(base_profile())
    assert store.recheck_all() == []
    # A hand-edited version that the full catalogue refuses (reordered stages).
    bad_dir = tmp_path / ".jev" / "workflow-profiles" / "fast"
    (bad_dir / "v2.json").write_text(
        __import__("json").dumps(
            base_profile(
                stage_list=[
                    "INTAKE",
                    "PLANNING",
                    "EXECUTING",
                    "VALIDATING",
                    "DESIGNING",
                    "INTEGRATING",
                ]
            )
        )
    )
    (bad_dir / "current.json").write_text('{"version": 2}\n')
    # An archived profile with a failing version is re-checked too.
    archived = store.save(base_profile(id="repo:old", display_name="Old"))
    store.archive("repo:old")
    old_dir = tmp_path / ".jev" / "workflow-profiles" / "old"
    (old_dir / "v2.json").write_text(
        __import__("json").dumps(base_profile(id="repo:old", stage_list=[]))
    )
    failures = store.recheck_all()
    by_id = {(f.profile_id, f.version): f for f in failures}
    assert ("repo:fast", 2) in by_id
    assert any(
        "Execution cannot run before validation" in m for m in by_id[("repo:fast", 2)].messages
    )
    assert ("repo:old", 2) in by_id and by_id[("repo:old", 2)].archived
    assert good["version"] == 1 and archived["version"] == 1


# ---- default preset parameter set ----------------------------------------------


def test_default_snapshot_reproduces_todays_behaviour() -> None:
    """Section 8.2/9.1: the default parameter set is the full canonical list
    with `when_needed` designing and `inherit` gates; every other setting
    resolves from the repo configuration (or a tier-1 flag), never invented."""
    config = JevConfig.model_validate(
        {
            "quality": {"level": "medium", "score_threshold": 90},
            "budget": {"max_plan_rounds": 3},
            "ship": {"enabled": False},
        }
    )
    resolved = build_default_snapshot(
        config=config, flags={}, autonomy_mode="standard", plan_approval_required=True
    )
    snapshot = resolved.snapshot
    assert resolved.profile_id is None and resolved.version is None
    assert snapshot["stage_list"] == [
        "INTAKE",
        "PLANNING",
        "VALIDATING",
        "DESIGNING",
        "EXECUTING",
        "INTEGRATING",
        "SHIPPING",
    ]
    assert snapshot["designing"] == "when_needed"
    assert snapshot["gate_policies"] == {
        "PLANNING": "inherit",
        "VALIDATING": "inherit",
        "DESIGNING": "inherit",
        "EXECUTING": "inherit",
        "INTEGRATING": "inherit",
        "SHIPPING": "inherit",
    }
    assert snapshot["quality_level"] == "medium"
    assert snapshot["score_threshold"] == 90
    assert snapshot["ship_enabled"] is False
    assert snapshot["resolved_from"]["stage_list"] == "default"
    assert snapshot["resolved_from"]["quality_level"] == "repo"
    assert resolved.assurance == "full"


def test_workflows_flags_defaults_and_overrides() -> None:
    assert workflows_enabled(JevConfig()) is True
    assert default_via_profile_enabled(JevConfig()) is False
    config = JevConfig.model_validate(
        {"workflows": {"enabled": False, "default_via_profile": True}}
    )
    assert workflows_enabled(config) is False
    assert default_via_profile_enabled(config) is True


# ---- gate helpers with a snapshot ----------------------------------------------


SNAPSHOT = {
    "stage_list": list(FULL_STAGES),
    "gate_policies": {
        "PLANNING": "require",
        "VALIDATING": "skip",
        "DESIGNING": "inherit",
        "EXECUTING": "require",
        "INTEGRATING": "inherit",
    },
    "designing": "when_needed",
}


def test_step_gate_target_snapshot_policies() -> None:
    assert step_gate_target(AutonomyMode.STANDARD, RunState.PLANNING, SNAPSHOT) is (
        RunState.AWAITING_PLANNING
    )
    # `skip` on its own runs the step without parking...
    assert step_gate_target(AutonomyMode.STANDARD, RunState.VALIDATING, SNAPSHOT) is (
        RunState.VALIDATING
    )
    # ...but an explicit ASK_EVERY_STEP gate stands under `skip`.
    assert step_gate_target(AutonomyMode.ASK_EVERY_STEP, RunState.VALIDATING, SNAPSHOT) is (
        RunState.AWAITING_VALIDATING
    )
    # `inherit` keeps the legacy behaviour.
    assert step_gate_target(AutonomyMode.STANDARD, RunState.INTEGRATING, SNAPSHOT) is (
        RunState.INTEGRATING
    )
    assert step_gate_target(AutonomyMode.ASK_EVERY_STEP, RunState.INTEGRATING, SNAPSHOT) is (
        RunState.AWAITING_INTEGRATING
    )
    # No snapshot: legacy behaviour, unchanged.
    assert step_gate_target(AutonomyMode.STANDARD, RunState.PLANNING) is RunState.PLANNING


def test_execution_gate_target_snapshot_policies() -> None:
    assert execution_gate_target(AutonomyMode.STANDARD, False, SNAPSHOT) is RunState.EXECUTING
    # `skip` never skips an explicit gate (per-run flag / require_plan_approval).
    assert (
        execution_gate_target(AutonomyMode.STANDARD, True, SNAPSHOT) is RunState.AWAITING_APPROVAL
    )
    require_snapshot = {
        "gate_policies": {"VALIDATING": "require"},
        "stage_list": list(FULL_STAGES),
    }
    assert execution_gate_target(AutonomyMode.STANDARD, False, require_snapshot) is (
        RunState.AWAITING_APPROVAL
    )
    assert execution_gate_target(AutonomyMode.AUTONOMOUS, True) is RunState.EXECUTING


# ---- pipeline: snapshot-aware execution ----------------------------------------


def _card(tid: str = "T1", test: str | None = None) -> TaskCard:
    return TaskCard(
        id=tid,
        title=f"task {tid}",
        description=f"do {tid}",
        files_hint=[f"src/{tid.lower()}.py"],
        depends_on=[],
        acceptance_criteria=["a"],
        test_command=test,
    )


ONE_TASK = Plan(summary="one task", tasks=[_card()])
DESIGN_TASK = Plan(summary="ui task", tasks=[_card()], needs_design=True)


def _config(**overrides: Any) -> JevConfig:
    data: dict[str, Any] = {
        "approval": {"require_plan_approval": True},
        "review": {"enabled": False},
        "quality": {"level": "medium"},
        "roles": {
            "coder": {"workers": ["claude_code", "codex", "api"], "models": ["coder-model"]},
            "planner": {
                "workers": ["claude_code", "codex", "api"],
                "models": ["config-model", "override-model"],
            },
        },
    }
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return JevConfig.model_validate(data)


class _ReviseOnce(RulesDispatcher):
    """D3 revises once, then approves — the plan-revise loop, scripted."""

    name = "revise-once"

    def __init__(self) -> None:
        self.d3_calls = 0

    async def decide(self, req: DecisionRequest) -> Decision:
        decision = await super().decide(req)
        if req.point == "D3":
            self.d3_calls += 1
            if self.d3_calls == 1:
                decision.answers["verdict"] = "revise"
        return decision


class _AlwaysRevise(RulesDispatcher):
    """D3 always revises: the plan-round cap is what stops the run."""

    name = "always-revise"

    async def decide(self, req: DecisionRequest) -> Decision:
        decision = await super().decide(req)
        if req.point == "D3":
            decision.answers["verdict"] = "revise"
        return decision


def _clean_review() -> FakeStep:
    return FakeStep(
        parsed=QualityReview(summary="review", findings=[]),
    )


@pytest.fixture
def make_env(fixture_repo: Path) -> Iterator[Any]:
    stores: list[Store] = []

    def make(
        script: dict[str, Any],
        config: JevConfig | None = None,
        dispatcher: Any = None,
        exit_codes: dict[str, list[int]] | None = None,
    ) -> Any:
        store = Store.open(
            fixture_repo / ".jev" / "state.db", clock=lambda: "2026-10-30T12:00:00+00:00"
        )
        stores.append(store)
        worker = FakeWorker("fake", script)
        registry = WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")})
        pipeline = build_pipeline(
            fixture_repo,
            config or _config(),
            store,
            registry,
            dispatcher=dispatcher,
            commands=FakeCommandRunner(exit_codes),
            settings=pipeline_settings(config or _config()),
        )
        return SimpleNamespace_env(fixture_repo, store, pipeline, worker)

    yield make
    for store in stores:
        store.close()


class SimpleNamespace_env:
    def __init__(self, repo: Path, store: Store, pipeline: Pipeline, worker: FakeWorker) -> None:
        self.repo, self.store, self.pipeline, self.worker = repo, store, pipeline, worker

    def jobs(self, role: str) -> list[WorkerJob]:
        return [j for j in self.worker.jobs if j.role == role]


def _idea(repo: Path) -> Idea:
    return Idea(text="add a module", repo_path=repo)


async def _drive_to_end(pipeline: Pipeline, run: Any) -> Any:
    """Approve every gate the run parks in until it stops or finishes
    (the plan gate, ASK_EVERY_STEP gates, and the ship gate)."""
    from jev_agent.core.pipeline import APPROVAL_GATES

    gates = set(APPROVAL_GATES) | {RunState.AWAITING_SHIP}
    while run.state in gates:
        run = await pipeline.approve(run.id)
    return run


def _save_profile(repo: Path, profile: dict[str, Any]) -> str:
    store = ProfileStore(repo, repo / "user-home")
    stored = store.save(profile)
    return f"{stored['id']}@v{stored['version']}"


async def test_snapshot_max_plan_rounds_beats_config(make_env: Any) -> None:
    """Section 8.1: round caps read from the snapshot only — a profile cap
    of 1 stops the run after one round where the config cap is 3."""
    env = make_env(
        {
            "planner": FakeStep(parsed=ONE_TASK),
            "validator": FakeStep(parsed=ValidationResult(verdict="approve", issues=[])),
            "code_reviewer": _clean_review(),
        },
        dispatcher=_AlwaysRevise(),
    )
    ref = _save_profile(
        env.repo,
        base_profile(
            max_plan_rounds=1,
            stage_list=[*FULL_STAGES, "SHIPPING"],
            gate_policies={**FULL_GATES, "SHIPPING": "inherit"},
        ),
    )
    run = await env.pipeline.start(_idea(env.repo), workflow_profile=ref)
    assert run.state is RunState.NEEDS_HUMAN
    assert run.reason is not None and "plan not approved after 1 rounds" in run.reason


async def test_snapshot_designing_omission_halts_with_named_error(make_env: Any) -> None:
    """Section 8.1, D3 move site: a plan that needs design under a profile
    that omits DESIGNING halts with the named error, never stalls and never
    silently skips."""
    env = make_env(
        {
            "planner": FakeStep(parsed=DESIGN_TASK),
            "validator": FakeStep(parsed=ValidationResult(verdict="approve", issues=[])),
        },
    )
    ref = _save_profile(
        env.repo,
        base_profile(
            stage_list=["INTAKE", "PLANNING", "VALIDATING", "EXECUTING", "INTEGRATING"],
            gate_policies={k: v for k, v in FULL_GATES.items() if k != "DESIGNING"},
            designing="never",
        ),
    )
    run = await env.pipeline.start(_idea(env.repo), workflow_profile=ref)
    assert run.state is RunState.FAILED
    assert run.reason == "Design was requested but the profile omits design."


async def test_snapshot_score_threshold_and_retries_govern_ship_gate(make_env: Any) -> None:
    """Section 8.1: score settings from the snapshot only. A clean gate
    scores below 100 (the practical max is 96), so a snapshot threshold of
    100 with one retry reworks once and then stops for a human — where the
    config threshold of 85 would have shipped."""
    env = make_env(
        {
            "planner": FakeStep(parsed=ONE_TASK),
            "validator": FakeStep(parsed=ValidationResult(verdict="approve", issues=[])),
            "code_reviewer": _clean_review(),
        },
    )
    ref = _save_profile(
        env.repo,
        base_profile(
            score_threshold=100,
            score_max_retries=1,
            stage_list=[*FULL_STAGES, "SHIPPING"],
            gate_policies={**FULL_GATES, "SHIPPING": "inherit"},
        ),
    )
    run = await _drive_to_end(
        env.pipeline, await env.pipeline.start(_idea(env.repo), workflow_profile=ref)
    )
    assert run.state is RunState.NEEDS_HUMAN, run.reason
    assert run.reason is not None and "ship score gate" in run.reason
    score_tasks = [
        t for t in env.store.list_tasks(run.id) if t.card.title.startswith("Fix score gate")
    ]
    assert len(score_tasks) == 1


async def test_snapshot_ship_off_beats_config_ship_on(make_env: Any) -> None:
    """Section 8.1: ship settings from the snapshot only — a profile with
    shipping off ends DONE unshipped even though the repo config has
    shipping on."""
    config = _config(ship={"enabled": True, "after_approval": "merge"})
    env = make_env(
        {
            "planner": FakeStep(parsed=ONE_TASK),
            "validator": FakeStep(parsed=ValidationResult(verdict="approve", issues=[])),
            "code_reviewer": _clean_review(),
        },
        config=config,
    )
    ref = _save_profile(env.repo, base_profile(ship_enabled=False))
    run = await _drive_to_end(
        env.pipeline, await env.pipeline.start(_idea(env.repo), workflow_profile=ref)
    )
    assert run.state is RunState.DONE, run.reason
    stages = [row.stage for row in env.store.list_stage_rows(run.id)]
    assert "SHIPPING" not in stages


async def test_snapshot_role_model_override_reaches_worker(make_env: Any) -> None:
    """Section 8.1: role model overrides read from the snapshot only — the
    planner job runs the profile's model, not the config's first model."""
    env = make_env(
        {
            "planner": FakeStep(parsed=ONE_TASK),
            "validator": FakeStep(parsed=ValidationResult(verdict="approve", issues=[])),
            "code_reviewer": _clean_review(),
        },
    )
    ref = _save_profile(env.repo, base_profile(role_models={"planner": "override-model"}))
    run = await _drive_to_end(
        env.pipeline, await env.pipeline.start(_idea(env.repo), workflow_profile=ref)
    )
    assert run.state is RunState.DONE, run.reason
    assert env.jobs("planner")[0].model == "override-model"


async def test_approve_gate_of_omitted_step_refused(make_env: Any) -> None:
    """Section 8.1, `Pipeline.approve`: approving the gate of an omitted
    step refuses with a named error instead of resuming into that stage."""
    env = make_env({"planner": FakeStep(parsed=ONE_TASK)})
    run = env.store.create_run(
        _idea(env.repo),
        None,
        "medium",
        flow_snapshot={
            "stage_list": ["INTAKE", "PLANNING", "VALIDATING", "EXECUTING", "INTEGRATING"],
            "gate_policies": {},
            "designing": "never",
        },
    )
    env.store.set_run_state(run.id, RunState.AWAITING_DESIGNING)
    with pytest.raises(PipelineError, match="omits the DESIGNING stage"):
        await env.pipeline.approve(run.id)


async def test_advance_breakout_on_missing_stage_halts_without_learning(make_env: Any) -> None:
    """Section 8.1, `_advance` loop breakout: a profiled run landing in a
    work state with no snapshot entry halts with a named error instead of
    calling `_finish` — so no learning step runs for it."""
    env = make_env({"planner": FakeStep(parsed=ONE_TASK)})
    run = env.store.create_run(
        _idea(env.repo),
        None,
        "medium",
        flow_snapshot={
            "stage_list": ["INTAKE", "PLANNING", "VALIDATING", "EXECUTING", "INTEGRATING"],
            "gate_policies": {},
            "designing": "never",
        },
    )
    env.store.set_run_state(run.id, RunState.DESIGNING)
    done = await env.pipeline.advance(run.id)
    assert done.state is RunState.FAILED
    assert done.reason is not None and "omits the DESIGNING stage" in done.reason
    assert done.learning_outcome is None


async def test_after_ship_vote_snapshot_branches(make_env: Any) -> None:
    """Section 8.1, `_after_ship_vote`: SHIPPING only when listed,
    AWAITING_SHIP exactly when `after_approval` is `ask`, DONE when
    shipping is off."""
    env = make_env({"planner": FakeStep(parsed=ONE_TASK)})
    base = {
        "stage_list": [*FULL_STAGES, "SHIPPING"],
        "gate_policies": {**FULL_GATES, "SHIPPING": "inherit"},
        "designing": "when_needed",
    }

    run = env.store.create_run(
        _idea(env.repo),
        None,
        "medium",
        flow_snapshot={**base, "ship_enabled": True, "after_approval": "ask"},
    )
    await env.pipeline._after_ship_vote(run, "jev/x/result")
    assert env.store.get_run(run.id).state is RunState.AWAITING_SHIP

    run = env.store.create_run(
        _idea(env.repo),
        None,
        "medium",
        flow_snapshot={**base, "ship_enabled": False, "after_approval": "ask"},
    )
    await env.pipeline._after_ship_vote(run, "jev/x/result")
    assert env.store.get_run(run.id).state is RunState.DONE

    run = env.store.create_run(
        _idea(env.repo),
        None,
        "medium",
        flow_snapshot={
            **base,
            "stage_list": FULL_STAGES,
            "ship_enabled": True,
            "after_approval": "merge",
        },
    )
    with pytest.raises(Exception, match="omits shipping"):
        await env.pipeline._after_ship_vote(run, "jev/x/result")


async def test_role_cache_exact_off_stops_serves(make_env: Any) -> None:
    """Section 8.1, cache mode: a snapshot with `exact_off` stops exact
    serves — the same planner prompt runs the worker again instead of
    serving the entry the legacy run stored."""
    config = _config(cache={"enabled": True, "cache_roles": ["planner"]})
    env = make_env(
        {"planner": FakeStep(parsed=ONE_TASK)},
        config=config,
    )
    harness = env.pipeline.h
    assignment = Assignment(worker="api", model="config-model", effort=None)
    legacy = env.store.create_run(_idea(env.repo), None, "medium")
    snap_run = env.store.create_run(
        _idea(env.repo),
        None,
        "medium",
        flow_snapshot={
            "stage_list": list(FULL_STAGES),
            "gate_policies": dict(FULL_GATES),
            "cache_mode": "exact_off",
        },
    )
    await harness.run_worker(
        run_id=legacy.id,
        task_id=None,
        attempt_no=1,
        role="planner",
        assignment=assignment,
        prompt="same prompt",
        workdir=env.repo,
        log_name="a",
    )
    assert len(env.jobs("planner")) == 1
    await harness.run_worker(
        run_id=legacy.id,
        task_id=None,
        attempt_no=1,
        role="planner",
        assignment=assignment,
        prompt="same prompt",
        workdir=env.repo,
        log_name="b",
    )
    assert len(env.jobs("planner")) == 1  # served from cache
    await harness.run_worker(
        run_id=snap_run.id,
        task_id=None,
        attempt_no=1,
        role="planner",
        assignment=assignment,
        prompt="same prompt",
        workdir=env.repo,
        log_name="c",
    )
    assert len(env.jobs("planner")) == 2  # exact_off: no serve


# ---- app/runs: [workflows] flags ------------------------------------------------


def _repo_context(repo: Path, config: JevConfig) -> Any:
    from jev_agent.app.context import RepoContext
    from jev_agent.core.learning import Learning

    store = Store.open(repo / ".jev" / "state.db")
    return RepoContext(repo, config, store, Learning.for_store(store))


async def test_create_run_default_via_profile_stores_snapshot_without_profile_id(
    fixture_repo: Path,
) -> None:
    """G1: with `[workflows] default_via_profile` true, a no-profile run
    executes via the profile path — a snapshot is stored — while picker
    `default` still means no profile (the id columns stay null)."""
    config = JevConfig.model_validate(
        {
            "approval": {"require_plan_approval": True},
            "quality": {"level": "medium"},
            "workflows": {"default_via_profile": True},
        }
    )
    ctx = _repo_context(fixture_repo, config)
    try:
        run = create_run(ctx, "add a module")
    finally:
        ctx.learning.close()
        ctx.store.close()
    assert run.flow_snapshot is not None
    assert run.flow_snapshot["stage_list"] == [
        "INTAKE",
        "PLANNING",
        "VALIDATING",
        "DESIGNING",
        "EXECUTING",
        "INTEGRATING",
        "SHIPPING",
    ]
    assert run.workflow_profile_id is None
    assert run.workflow_profile_version is None


def test_create_run_default_via_profile_off_stays_legacy(fixture_repo: Path) -> None:
    """Default false: a no-profile run stores no snapshot (legacy path)."""
    config = JevConfig.model_validate(
        {"approval": {"require_plan_approval": True}, "quality": {"level": "medium"}}
    )
    ctx = _repo_context(fixture_repo, config)
    try:
        run = create_run(ctx, "add a module")
    finally:
        ctx.learning.close()
        ctx.store.close()
    assert run.flow_snapshot is None


def test_create_run_enabled_false_blocks_profile_selection(fixture_repo: Path) -> None:
    """Rollback switch, distinct from `default_via_profile`: with
    `[workflows] enabled = false`, picking a profile refuses at creation
    instead of silently falling back to the default."""
    store = ProfileStore(fixture_repo, fixture_repo / "user-home")
    store.save(base_profile())
    config = JevConfig.model_validate(
        {
            "approval": {"require_plan_approval": True},
            "quality": {"level": "medium"},
            "workflows": {"enabled": False},
        }
    )
    ctx = _repo_context(fixture_repo, config)
    try:
        with pytest.raises(JevError, match="workflow profiles are disabled"):
            create_run(ctx, "add a module", workflow_profile="repo:fast")
        # ...and disabled wins over default_via_profile: no snapshot either.
        config2 = JevConfig.model_validate(
            {
                "approval": {"require_plan_approval": True},
                "quality": {"level": "medium"},
                "workflows": {"enabled": False, "default_via_profile": True},
            }
        )
        ctx2 = _repo_context(fixture_repo, config2)
        try:
            run = create_run(ctx2, "add a module")
        finally:
            ctx2.learning.close()
            ctx2.store.close()
        assert run.flow_snapshot is None
    finally:
        ctx.learning.close()
        ctx.store.close()


def test_cancel_run_helper_still_available() -> None:
    """Sanity: the cancel helper imported by the dashboard still resolves
    (a refactor guard for the pipeline module's public surface)."""
    assert callable(cancel_run)
