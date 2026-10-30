"""Workflow profiles, phase 4 (issue #368): presets as data, plus the
run-creation picker.

Fixture (stated, so "validates against live configuration" has a fixture):
a repo configuration with quality level ``medium``, plan approval on,
shipping off, cache enabled, and this named set of configured models —
planner ``planner-a``/``planner-b``, validator ``validator-a``, coder
``coder-a``/``coder-b``, reviewer ``reviewer-a``, code_reviewer
``code-reviewer-a``, learner ``learner-a``, designer ``designer-a`` —
under the default round-cap ceilings (plan 3, fix 2, score retries 2).
Under that fixture the section 9.1 table's default-column values resolve
to: score threshold 85 (the repo default) and cache mode ``exact``.

Efficiency evidence, stated honestly (design section 15, phase 4): the
stub/replay harness scripts stage sequences and its workers report no
cost, so no stage is measured-``full`` and no summed-``cost_usd``
comparison between Efficiency and the default is possible there — the
``null``-cost stages are excluded by rule, leaving an empty comparison.
The phase-4 criterion therefore falls back to the static check the design
names: the preset's resolved snapshot matches its section 9.1 table
(fewer round caps, quality/planner/cache unchanged, no cheaper-model
claim, because no per-model price source exists). The end-to-end Mac
dashboard runs are the orchestrator's post-merge step, not this suite's.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jev_agent.app.context import pipeline_settings
from jev_agent.config.schema import JevConfig
from jev_agent.core.commands import FakeCommandRunner
from jev_agent.core.models import Idea, Plan, RunState, TaskCard, ValidationResult
from jev_agent.core.pipeline import build_pipeline
from jev_agent.core.store import Store
from jev_agent.core.workflow_profiles import (
    ProfileStore,
    ProfileValidationError,
    build_default_snapshot,
    load_preset,
    resolve_workflow_profile,
    validate_presets,
    validate_profile,
)
from jev_agent.core.workflow_profiles.presets import (
    PRESET_NAMES,
    preset_picker_entries,
)
from jev_agent.core.workflow_profiles.resolver import (
    build_validation_context,
    pin_workflow_profile,
)
from jev_agent.core.workflow_profiles.validator import ValidationContext
from jev_agent.roles.schemas import QualityReview
from jev_agent.workers.fake import FakeStep, FakeWorker
from jev_agent.workers.registry import WorkerRegistry

CANONICAL = [
    "INTAKE",
    "PLANNING",
    "VALIDATING",
    "DESIGNING",
    "EXECUTING",
    "INTEGRATING",
    "SHIPPING",
]
INHERIT_GATES = {
    "PLANNING": "inherit",
    "VALIDATING": "inherit",
    "DESIGNING": "inherit",
    "EXECUTING": "inherit",
    "INTEGRATING": "inherit",
    "SHIPPING": "inherit",
}

# The fixture's named set of configured models (see the module docstring).
FIXTURE_MODELS: dict[str, list[str]] = {
    "planner": ["planner-a", "planner-b"],
    "validator": ["validator-a"],
    "coder": ["coder-a", "coder-b"],
    "reviewer": ["reviewer-a"],
    "code_reviewer": ["code-reviewer-a"],
    "learner": ["learner-a"],
    "designer": ["designer-a"],
}


def fixture_config(**overrides: Any) -> JevConfig:
    """The fixture repo configuration: quality ``medium``, plan approval on,
    shipping off, cache enabled, the fixture model set, default ceilings."""
    config = JevConfig.model_validate(
        {
            "approval": {"require_plan_approval": True},
            "review": {"enabled": False},
            "quality": {"level": "medium"},
            "ship": {"enabled": False},
        }
    )
    for role, models in FIXTURE_MODELS.items():
        getattr(config.roles, role).models = list(models)
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def fixture_context(**overrides: Any) -> ValidationContext:
    """The fixture as a validator context (the same models and ceilings the
    resolver builds from the fixture configuration)."""
    context = ValidationContext(
        models_by_role=dict(FIXTURE_MODELS),
        learning_profiles={"default"},
        ceilings={"max_plan_rounds": 3, "max_fix_rounds": 2, "score_max_retries": 2},
    )
    for key, value in overrides.items():
        setattr(context, key, value)
    return context


def _resolve(ref: str, config: JevConfig, tmp_path: Path, **kwargs: Any) -> Any:
    return resolve_workflow_profile(
        ref=ref,
        config=config,
        repo_root=tmp_path,
        user_root=tmp_path / "user",
        plan_approval_required=kwargs.pop("plan_approval_required", True),
        cda_available=kwargs.pop("cda_available", True),
        **kwargs,
    )


# ---- preset files: format, identity, shared validation ------------------------


def test_each_preset_validates_against_the_fixture() -> None:
    """Section 15, phase 4: each preset validates under the shared validator
    against the fixture's live configuration."""
    assert validate_presets(fixture_context()) == {
        "default": [],
        "efficiency": [],
        "accuracy": [],
    }


def test_each_preset_validates_via_the_resolver_context(tmp_path: Path) -> None:
    """The same validation through the context the resolver actually builds
    from a JevConfig (role models from config, ceilings from config)."""
    config = fixture_config()
    context = build_validation_context(config, learning_profiles={"default"})
    assert validate_presets(context) == {"default": [], "efficiency": [], "accuracy": []}


@pytest.mark.parametrize("name", PRESET_NAMES)
def test_preset_file_shape(name: str) -> None:
    """Section 6.6: presets carry a release instead of a version, the full
    canonical stage list, and `inherit` gate policies for every gated step;
    all three ship under one release."""
    preset = load_preset(name)
    assert preset["id"] == f"preset:{name}"
    assert preset["scope"] == "preset"
    assert preset["release"] == "0.5.0"
    assert preset["stage_list"] == CANONICAL
    assert preset["gate_policies"] == INHERIT_GATES
    assert preset["designing"] == "when_needed"
    assert "version" not in preset
    assert validate_profile(preset, context=fixture_context()) == []


def test_unknown_preset_name_refused() -> None:
    with pytest.raises(ValueError, match="unknown workflow preset 'speedy'"):
        load_preset("speedy")


def test_preset_scope_cannot_be_stored(tmp_path: Path) -> None:
    """Presets are package data, never stored profiles: the store refuses a
    preset-scope file instead of saving it under repo/user scope."""
    store = ProfileStore(tmp_path, tmp_path / "user")
    with pytest.raises(ValueError, match="Unknown profile id 'preset:efficiency'"):
        store.save(load_preset("efficiency"))


def test_preset_without_release_refused() -> None:
    preset = load_preset("efficiency")
    del preset["release"]
    assert any("needs a release" in e for e in validate_profile(preset))


def test_preset_with_version_refused() -> None:
    preset = dict(load_preset("efficiency"), version=1)
    assert any("carries a release, not a version" in e for e in validate_profile(preset))


# ---- stale presets refuse at startup, not at run time --------------------------


def test_preset_referencing_a_removed_model_fails_validation() -> None:
    """Section 15, phase 4: a preset referencing a removed model fails
    validation — the startup check runs the same validator, so the shipped
    files fail the same way the moment one of their references goes stale
    (here a tampered copy names the removed model; the shipped files carry
    no role models, so their staleness surface is the ceiling check below)."""
    tampered = dict(load_preset("efficiency"), role_models={"coder": "removed-model"})
    errors = validate_profile(tampered, context=fixture_context())
    assert any("Unknown model 'removed-model' for role coder" in e for e in errors)


def test_stale_preset_fails_startup_validation_when_ceilings_drop() -> None:
    """A repo whose ceilings drop below a preset's caps makes that preset
    stale: startup validation (validate_presets against the live config)
    fails naming the cap, before any run is created."""
    low_ceiling = fixture_context(
        ceilings={"max_plan_rounds": 1, "max_fix_rounds": 2, "score_max_retries": 2}
    )
    errors = validate_presets(low_ceiling)
    assert errors["default"] == []
    assert any(
        "max_plan_rounds 2 is above this repo's ceiling 1" in e for e in errors["efficiency"]
    )
    assert any("max_plan_rounds 3 is above this repo's ceiling 1" in e for e in errors["accuracy"])


# ---- section 9.1 tables, as resolved snapshots ---------------------------------


def test_efficiency_snapshot_matches_section_9_1(tmp_path: Path) -> None:
    """Efficiency: fewer round caps, and quality / planner policy / cache /
    score threshold / role models all unchanged from the repo configuration
    (never lowered, never narrowed, never a cheaper-model claim)."""
    config = fixture_config()
    resolved = _resolve("preset:efficiency", config, tmp_path)
    assert resolved is not None
    snapshot = resolved.snapshot
    assert snapshot["max_plan_rounds"] == 2
    assert snapshot["max_fix_rounds"] == 1
    assert snapshot["score_max_retries"] == 1
    assert snapshot["quality_level"] == "medium"
    assert snapshot["planner_policy"] is None  # the repo's (unset) policy
    assert snapshot["cache_mode"] == "exact"
    assert snapshot["score_threshold"] == config.quality.score_threshold == 85
    assert snapshot["cda_grading"] == "off"
    assert snapshot["role_models"] == {}
    assert snapshot["stage_list"] == CANONICAL
    assert snapshot["designing"] == "when_needed"
    assert resolved.profile_id == "preset:efficiency"
    assert resolved.version is None
    assert resolved.release == "0.5.0"
    assert resolved.goal is None  # no stated goal
    assert resolved.preset_goal == "efficiency"  # display only (section 5)
    assert resolved.assurance == "full" and resolved.assurance_absent == []


def test_efficiency_never_lowers_a_lower_repo_quality(tmp_path: Path) -> None:
    """Section 9.1 as resolved (the §10 resolution): Efficiency quality is
    repo configuration, never lowered — a `basic` repo stays `basic`."""
    config = fixture_config()
    config.quality.level = "basic"
    resolved = _resolve("preset:efficiency", config, tmp_path)
    assert resolved is not None and resolved.snapshot["quality_level"] == "basic"


def test_efficiency_caps_are_strictly_below_the_default_caps(tmp_path: Path) -> None:
    """The static fallback for the Efficiency savings claim (see the module
    docstring): every Efficiency cap is strictly below the default's cap on
    the same fixture, so the preset measurably allows fewer rounds."""
    config = fixture_config()
    default = build_default_snapshot(config=config, plan_approval_required=True, cda_available=True)
    efficiency = _resolve("preset:efficiency", config, tmp_path)
    assert efficiency is not None
    for key in ("max_plan_rounds", "max_fix_rounds", "score_max_retries"):
        assert efficiency.snapshot[key] < default.snapshot[key], key


def test_accuracy_snapshot_matches_section_9_1(tmp_path: Path) -> None:
    """Accuracy differs from the default in exactly three observable ways
    (section 9.1): CDA grading on, a production quality floor, and the
    `accuracy` goal label — plus the H3 priority row, display only."""
    config = fixture_config()
    resolved = _resolve("preset:accuracy", config, tmp_path)
    assert resolved is not None
    snapshot = resolved.snapshot
    assert snapshot["stage_list"] == CANONICAL
    assert snapshot["cda_grading"] == "on"
    assert snapshot["quality_level"] == "production"
    assert snapshot["max_plan_rounds"] == 3
    assert snapshot["max_fix_rounds"] == 2
    assert snapshot["score_max_retries"] == 2
    assert snapshot["score_threshold"] == 85
    assert snapshot["cache_mode"] == "exact"
    assert snapshot["planner_policy"] is None  # never narrowed
    assert snapshot["role_models"] == {}  # never narrowed
    assert snapshot["priority"] == "quality first, speed second"
    assert resolved.preset_goal == "accuracy"
    assert resolved.goal is None
    # Section 10.1 inputs present: a real quality level (not `none`), the
    # score gate kept, the plan gate present, the full stage list.
    assert snapshot["quality_level"] != "none"
    assert not snapshot["score_skip_human"]
    assert resolved.assurance == "full" and resolved.assurance_absent == []


def test_accuracy_quality_floor_keeps_a_stricter_repo_level(tmp_path: Path) -> None:
    """Section 9.1: the repo's level is kept when stricter — a `scalable`
    repo stays `scalable` under Accuracy, and so does a workspace value
    passed as the repo tier."""
    config = fixture_config()
    config.quality.level = "scalable"
    resolved = _resolve("preset:accuracy", config, tmp_path)
    assert resolved is not None and resolved.snapshot["quality_level"] == "scalable"
    workspace = _resolve(
        "preset:accuracy",
        fixture_config(),
        tmp_path,
        flags={"quality_repo_value": "scalable", "quality_source": "workspace"},
    )
    assert workspace is not None and workspace.snapshot["quality_level"] == "scalable"


def test_accuracy_refuses_without_a_cda_backend(tmp_path: Path) -> None:
    """Section 6.5: grading `on` with no backend refuses at creation — it
    does not silently run ungraded."""
    with pytest.raises(ProfileValidationError, match="cda_grading"):
        _resolve("preset:accuracy", fixture_config(), tmp_path, cda_available=False)


def test_default_preset_matches_the_phase_3_parameter_set(tmp_path: Path) -> None:
    """The `preset:default` file is the data form of phase 3's
    `build_default_snapshot` parameter set: every setting resolves the same
    way; only the identity differs (a preset id + release, no version)."""
    config = fixture_config()
    legacy = build_default_snapshot(config=config, plan_approval_required=True, cda_available=True)
    preset = _resolve("preset:default", config, tmp_path)
    assert preset is not None
    for key in (
        "stage_list",
        "gate_policies",
        "designing",
        "quality_level",
        "max_plan_rounds",
        "max_fix_rounds",
        "score_max_retries",
        "score_threshold",
        "cache_mode",
        "cda_grading",
        "role_models",
    ):
        assert preset.snapshot[key] == legacy.snapshot[key], key
    assert preset.profile_id == "preset:default"
    assert preset.release == "0.5.0"


def test_picker_default_still_means_no_profile(tmp_path: Path) -> None:
    """G1: the resolver's `default` still means no profile, not the preset
    file — the picker value and the preset reference are different things."""
    assert (
        resolve_workflow_profile(
            ref="default",
            config=fixture_config(),
            repo_root=tmp_path,
            user_root=tmp_path / "user",
            flags={},
            cda_available=True,
        )
        is None
    )


def test_unknown_preset_ref_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown workflow preset 'preset:speedy'"):
        _resolve("preset:speedy", fixture_config(), tmp_path)


# ---- pinning: presets pass through, stored profiles pin ------------------------


def test_preset_ref_passes_through_pinning_unchanged(tmp_path: Path) -> None:
    """Presets are pinned by release, not version: queue-time pinning leaves
    a preset reference exactly as typed (the scheduler pins it at enqueue
    by passing it through, not by versioning it)."""
    assert (
        pin_workflow_profile(
            ref="preset:efficiency", repo_root=tmp_path, user_root=tmp_path / "user"
        )
        == "preset:efficiency"
    )
    with pytest.raises(ValueError, match="unknown workflow preset 'preset:speedy'"):
        pin_workflow_profile(ref="preset:speedy", repo_root=tmp_path, user_root=tmp_path / "user")


def test_stored_profile_still_pins_at_queue_time(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path, tmp_path / "user")
    store.save(
        {
            "id": "repo:fast",
            "display_name": "Fast",
            "scope": "repo",
            "stage_list": CANONICAL,
            "gate_policies": dict(INHERIT_GATES),
            "designing": "when_needed",
            "ship_enabled": False,
        }
    )
    assert (
        pin_workflow_profile(ref="repo:fast", repo_root=tmp_path, user_root=tmp_path / "user")
        == "repo:fast@v1"
    )


# ---- picker entries: behaviour before selection --------------------------------


def test_picker_entries_show_behaviour_assurance_and_priority() -> None:
    """Section 13.1: the picker entries carry their documented behaviour
    before selection, the assurance preview from the repo's current
    settings, the Accuracy priority row, and the unpriced-role notice on
    Efficiency (and nowhere else)."""
    entries = preset_picker_entries(fixture_config(), plan_approval_required=True)
    by_ref = {entry["ref"]: entry for entry in entries}
    assert [entry["ref"] for entry in entries] == [
        "default",
        "preset:efficiency",
        "preset:accuracy",
    ]
    assert by_ref["default"]["assurance_label"] == "Full checks"
    efficiency = by_ref["preset:efficiency"]
    assert "plan at most 2" in efficiency["description"]
    assert "never lowered" in efficiency["description"]
    assert efficiency["unpriced_notice"] is not None
    assert "No role has a configured model price" in efficiency["unpriced_notice"]
    assert efficiency["assurance_label"] == "Full checks"
    accuracy = by_ref["preset:accuracy"]
    assert "CDA grading on" in accuracy["description"]
    assert accuracy["priority"] == "quality first, speed second"
    assert accuracy["unpriced_notice"] is None
    assert accuracy["assurance_label"] == "Full checks"


def test_picker_assurance_preview_follows_repo_settings() -> None:
    """The preview is a pre-computation over the repo's current settings:
    a repo with the quality gate off previews as reduced, not full."""
    config = fixture_config()
    config.quality.level = "none"
    entries = preset_picker_entries(config, plan_approval_required=True)
    assert entries[0]["assurance"] == "reduced"
    assert "quality gate off" in entries[0]["assurance_absent"]
    # Accuracy's production floor restores the quality input, so its preview
    # is full even on that repo (the plan gate is still present).
    assert entries[2]["assurance"] == "full"


# ---- preset runs through the snapshot-aware pipeline ----------------------------


def _card(tid: str = "T1") -> TaskCard:
    return TaskCard(
        id=tid,
        title=f"task {tid}",
        description=f"do {tid}",
        files_hint=[f"src/{tid.lower()}.py"],
        depends_on=[],
        acceptance_criteria=["a"],
    )


ONE_TASK = Plan(summary="one task", tasks=[_card()])


def _clean_review() -> FakeStep:
    return FakeStep(parsed=QualityReview(summary="review", findings=[]))


def _pipeline_env(fixture_repo: Path) -> Any:
    config = JevConfig.model_validate(
        {
            "approval": {"require_plan_approval": True},
            "review": {"enabled": False},
            "quality": {"level": "medium"},
            "roles": {
                "coder": {"workers": ["claude_code", "codex", "api"], "models": ["coder-model"]},
            },
            "ship": {"enabled": False},
        }
    )
    store = Store.open(
        fixture_repo / ".jev" / "state.db", clock=lambda: "2026-10-30T12:00:00+00:00"
    )
    worker = FakeWorker(
        "fake",
        {
            "planner": FakeStep(parsed=ONE_TASK),
            "validator": FakeStep(parsed=ValidationResult(verdict="approve", issues=[])),
            "code_reviewer": _clean_review(),
        },
    )
    registry = WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")})
    pipeline = build_pipeline(
        fixture_repo,
        config,
        store,
        registry,
        commands=FakeCommandRunner(None),
        settings=pipeline_settings(config),
    )
    return SimpleNamespace(repo=fixture_repo, store=store, pipeline=pipeline, worker=worker)


async def test_efficiency_preset_runs_through_the_pipeline(fixture_repo: Path) -> None:
    """Phase 4 exit, replay-harness form: an Efficiency preset run executes
    through the snapshot-aware pipeline to DONE, stores the preset id +
    release + snapshot on the run record, and records the full stage list
    (DESIGNING skipped for a non-UI run, as under `when_needed`)."""
    env = _pipeline_env(fixture_repo)
    try:
        run = await env.pipeline.start(
            Idea(text="add a module", repo_path=env.repo), workflow_profile="preset:efficiency"
        )
        from jev_agent.core.pipeline import APPROVAL_GATES

        gates = set(APPROVAL_GATES) | {RunState.AWAITING_SHIP}
        while run.state in gates:
            run = await env.pipeline.approve(run.id)
        assert run.state is RunState.DONE
        reread = env.store.get_run(run.id)
        assert reread.workflow_profile_id == "preset:efficiency"
        assert reread.workflow_profile_version is None
        assert reread.workflow_profile_release == "0.5.0"
        assert reread.flow_snapshot is not None
        assert reread.flow_snapshot["max_plan_rounds"] == 2
        assert reread.flow_snapshot["max_fix_rounds"] == 1
        assert reread.preset_goal == "efficiency"
        assert reread.goal is None
        rows = env.store.list_stage_rows(run.id)
        stages = [row.stage for row in rows if row.outcome != "skipped"]
        assert stages == ["INTAKE", "PLANNING", "VALIDATING", "EXECUTING", "INTEGRATING"]
    finally:
        env.store.close()
