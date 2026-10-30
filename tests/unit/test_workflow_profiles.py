"""Workflow profiles, phases 0-2 (issue #368): validator catalogue, store
atomicity/versioning/archive/delete, resolver precedence/refusals/assurance,
snapshot survival, and export/import round-trips.

Each catalogue entry is a separate case (design section 17: unknown model,
quality level, cache mode, and learning profile are four cases, not one).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jev_agent.config.schema import JevConfig
from jev_agent.core.workflow_profiles import (
    ProfileRefusal,
    ProfileStore,
    ProfileValidationError,
    resolve_workflow_profile,
    validate_profile,
)
from jev_agent.core.workflow_profiles.resolver import pin_workflow_profile
from jev_agent.core.workflow_profiles.validator import ValidationContext

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


def ship_profile(**overrides: Any) -> dict[str, Any]:
    profile = base_profile(
        stage_list=[*FULL_STAGES, "SHIPPING"],
        gate_policies={**FULL_GATES, "SHIPPING": "inherit"},
        ship_enabled=True,
    )
    profile.update(overrides)
    return profile


CTX = ValidationContext(
    models_by_role={"planner": ["model-a", "model-b"], "coder": ["model-c"]},
    learning_profiles={"default"},
    ceilings={"max_plan_rounds": 3, "max_fix_rounds": 2, "score_max_retries": 2},
)


# ---- validator: catalogue entries, each a separate case ------------------------


def test_valid_profile_passes() -> None:
    assert validate_profile(base_profile(), context=CTX) == []


def test_unknown_stage_refused() -> None:
    errors = validate_profile(base_profile(stage_list=[*FULL_STAGES, "DANCING"]))
    assert any("Unknown stage 'DANCING'" in e for e in errors)


def test_reordered_stages_refused() -> None:
    errors = validate_profile(
        base_profile(
            stage_list=["INTAKE", "PLANNING", "EXECUTING", "VALIDATING", "DESIGNING", "INTEGRATING"]
        )
    )
    assert any("Execution cannot run before validation" in e for e in errors)


@pytest.mark.parametrize(
    "omitted",
    ["INTAKE", "PLANNING", "VALIDATING", "EXECUTING", "INTEGRATING"],
)
def test_omitted_required_stage_refused(omitted: str) -> None:
    stages = [s for s in FULL_STAGES if s != omitted]
    gates = {k: v for k, v in FULL_GATES.items() if k != omitted}
    designing = "never" if omitted == "DESIGNING" else "when_needed"
    errors = validate_profile(
        base_profile(stage_list=stages, gate_policies=gates, designing=designing)
    )
    assert any("cannot be removed" in e for e in errors), errors


def test_duplicate_stage_refused() -> None:
    errors = validate_profile(base_profile(stage_list=[*FULL_STAGES, "PLANNING"]))
    assert any("Duplicate stage" in e for e in errors)


def test_empty_stage_list_refused() -> None:
    errors = validate_profile(base_profile(stage_list=[], gate_policies={}))
    assert any("needs at least intake" in e for e in errors)


def test_missing_fields_refused() -> None:
    errors = validate_profile({"id": "repo:x"})
    assert sum("Missing field" in e for e in errors) >= 4


def test_unknown_field_refused() -> None:
    errors = validate_profile(base_profile(test_command="pytest"))
    assert any("Unknown field 'test_command'" in e for e in errors)


def test_gate_without_stage_refused() -> None:
    errors = validate_profile(
        base_profile(
            stage_list=[s for s in FULL_STAGES if s != "DESIGNING"],
            gate_policies=FULL_GATES,
            designing="never",
        )
    )
    assert any("has no stage to attach to" in e for e in errors)


def test_intake_gate_policy_refused() -> None:
    errors = validate_profile(base_profile(gate_policies={**FULL_GATES, "INTAKE": "require"}))
    assert any("Intake has no waiting state" in e for e in errors)


def test_shipping_incoherence_refused() -> None:
    errors = validate_profile(base_profile(ship_enabled=True))
    assert any("Shipping is omitted but shipping is on" in e for e in errors)


def test_unknown_model_refused() -> None:
    errors = validate_profile(base_profile(role_models={"planner": "no-such-model"}), context=CTX)
    assert any("Unknown model 'no-such-model' for role planner" in e for e in errors)


def test_unknown_quality_level_refused() -> None:
    errors = validate_profile(base_profile(quality_level="ultra"))
    assert any("Unknown quality level 'ultra'" in e for e in errors)


def test_unknown_cache_mode_refused() -> None:
    errors = validate_profile(base_profile(cache_mode="semantic"))
    assert any("Unknown cache mode 'semantic'" in e for e in errors)


def test_unknown_learning_profile_refused() -> None:
    errors = validate_profile(base_profile(learning_profile="ghost"), context=CTX)
    assert any("Unknown learning profile 'ghost'" in e for e in errors)


def test_unknown_planner_policy_refused() -> None:
    errors = validate_profile(base_profile(planner_policy="cheapest"))
    assert any("Unknown planner policy 'cheapest'" in e for e in errors)


def test_cap_above_ceiling_refused_not_clamped() -> None:
    errors = validate_profile(base_profile(max_plan_rounds=9), context=CTX)
    assert any("above this repo's ceiling 3" in e for e in errors)


def test_cap_below_one_refused() -> None:
    errors = validate_profile(base_profile(max_fix_rounds=0))
    assert any("max_fix_rounds must be an integer of at least 1" in e for e in errors)


def test_quality_none_with_shipping_refused() -> None:
    errors = validate_profile(ship_profile(quality_level="none"))
    assert any(e.startswith("Refused:") and "none" in e for e in errors)


def test_score_skip_human_with_shipping_refused() -> None:
    errors = validate_profile(ship_profile(score_skip_human=True))
    assert any(e.startswith("Refused:") and "score_skip_human" in e for e in errors)


def test_save_failure_lists_every_error() -> None:
    errors = validate_profile(
        base_profile(stage_list=[], gate_policies={}, quality_level="ultra", cache_mode="nope")
    )
    assert len(errors) >= 3


# ---- store: atomicity, versioning, archive, delete ------------------------------


def test_save_versions_and_current_pointer(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path, tmp_path / "user")
    first = store.save(base_profile())
    second = store.save(base_profile(quality_level="medium"))
    assert (first["version"], second["version"]) == (1, 2)
    assert store.get("repo:fast")["quality_level"] == "medium"
    assert store.versions("repo:fast") == [1, 2]


def test_current_pointer_crash_semantics(tmp_path: Path) -> None:
    """A crash between writing v<n>.json and current.json leaves current
    pointing at the previous version: the new version simply is not current."""
    store = ProfileStore(tmp_path, tmp_path / "user")
    store.save(base_profile())
    directory = tmp_path / ".jev" / "workflow-profiles" / "fast"
    (directory / "v2.json").write_text(
        json.dumps(base_profile(quality_level="production", version=2)), encoding="utf-8"
    )
    assert store.get("repo:fast")["version"] == 1


def test_collision_retries_as_next_version(tmp_path: Path) -> None:
    """Two editors racing for the same version: the second retries as n+1,
    never overwriting."""
    store = ProfileStore(tmp_path, tmp_path / "user")
    store.save(base_profile())
    directory = tmp_path / ".jev" / "workflow-profiles" / "fast"
    (directory / "v2.json").write_text(json.dumps(base_profile(version=2)), encoding="utf-8")
    stored = store.save(base_profile(quality_level="basic"))
    assert stored["version"] == 3
    assert store.get("repo:fast")["version"] == 3


def test_archive_hides_from_lists_but_snapshots_keep_running(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path, tmp_path / "user")
    store.save(base_profile())
    store.archive("repo:fast")
    assert store.list_profiles() == []
    assert store.get("repo:fast")["version"] == 1
    assert store.list_profiles(include_archived=True) != []


def test_permanent_delete_refused_while_referenced(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path, tmp_path / "user")
    store.save(base_profile())
    with pytest.raises(ProfileRefusal, match="2 run\\(s\\) reference"):
        store.delete_permanent("repo:fast", referencing_runs=2)


def test_permanent_delete_removes_files_when_unreferenced(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path, tmp_path / "user")
    store.save(base_profile())
    store.delete_permanent("repo:fast", referencing_runs=0)
    assert not store.exists("repo:fast")


def test_corrupt_file_reported_never_partially_applied(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path, tmp_path / "user")
    store.save(base_profile())
    directory = tmp_path / ".jev" / "workflow-profiles" / "fast"
    (directory / "v1.json").write_text('{"id": "repo:fast", "stage_list": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be read"):
        store.get("repo:fast")


# ---- resolver: precedence, refusals, assurance, snapshots ------------------------


def _resolver_store(tmp_path: Path) -> ProfileStore:
    store = ProfileStore(tmp_path, tmp_path / "user")
    store.save(base_profile())
    return store


def test_resolver_flag_beats_profile_beats_repo(tmp_path: Path) -> None:
    _resolver_store(tmp_path)
    config = JevConfig()
    resolved = resolve_workflow_profile(
        ref="repo:fast",
        config=config,
        repo_root=tmp_path,
        user_root=tmp_path / "user",
        flags={"quality_level": "production"},
        cda_available=False,
    )
    assert resolved is not None
    assert resolved.snapshot["quality_level"] == "production"
    assert resolved.snapshot["resolved_from"]["quality_level"] == "flag"


def test_resolver_profile_beats_repo_and_records_source(tmp_path: Path) -> None:
    store = _resolver_store(tmp_path)
    store.save(base_profile(quality_level="medium"))
    config = JevConfig()
    resolved = resolve_workflow_profile(
        ref="repo:fast",
        config=config,
        repo_root=tmp_path,
        user_root=tmp_path / "user",
        flags={},
        cda_available=False,
    )
    assert resolved is not None
    assert resolved.snapshot["quality_level"] == "medium"
    assert resolved.snapshot["resolved_from"]["quality_level"] == "profile"


def test_resolver_default_means_no_profile(tmp_path: Path) -> None:
    assert (
        resolve_workflow_profile(
            ref="default",
            config=JevConfig(),
            repo_root=tmp_path,
            user_root=tmp_path / "user",
            flags={},
            cda_available=False,
        )
        is None
    )
    assert (
        resolve_workflow_profile(
            ref=None,
            config=JevConfig(),
            repo_root=tmp_path,
            user_root=tmp_path / "user",
            flags={},
            cda_available=False,
        )
        is None
    )


def test_resolver_unresolvable_id_names_id_and_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"repo:gone.*version 4.*pick a current profile"):
        resolve_workflow_profile(
            ref="repo:gone@v4",
            config=JevConfig(),
            repo_root=tmp_path,
            user_root=tmp_path / "user",
            flags={},
            cda_available=False,
        )


def test_resolver_refuses_require_gate_on_autonomous(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path, tmp_path / "user")
    store.save(base_profile(gate_policies={**FULL_GATES, "VALIDATING": "require"}))
    with pytest.raises(ProfileValidationError, match="Autonomous runs do not park"):
        resolve_workflow_profile(
            ref="repo:fast",
            config=JevConfig(),
            repo_root=tmp_path,
            user_root=tmp_path / "user",
            flags={},
            autonomy_mode="autonomous",
            cda_available=False,
        )


def test_resolver_refuses_grading_on_without_backend(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path, tmp_path / "user")
    store.save(base_profile(cda_grading="on"))
    with pytest.raises(ProfileValidationError, match="cda_grading"):
        resolve_workflow_profile(
            ref="repo:fast",
            config=JevConfig(),
            repo_root=tmp_path,
            user_root=tmp_path / "user",
            flags={},
            cda_available=False,
        )


def test_resolver_assurance_full_and_reduced(tmp_path: Path) -> None:
    _resolver_store(tmp_path)
    config = JevConfig()
    full = resolve_workflow_profile(
        ref="repo:fast",
        config=config,
        repo_root=tmp_path,
        user_root=tmp_path / "user",
        flags={"quality_level": "medium"},
        plan_approval_required=True,
        cda_available=False,
    )
    assert full is not None and full.assurance == "full" and full.assurance_absent == []
    reduced = resolve_workflow_profile(
        ref="repo:fast",
        config=config,
        repo_root=tmp_path,
        user_root=tmp_path / "user",
        flags={"quality_level": "none"},
        plan_approval_required=False,
        cda_available=False,
    )
    # quality none with shipping off is label-only (section 10.2): the run
    # is reduced, not refused.
    assert reduced is not None and reduced.assurance == "reduced"
    assert "quality gate off" in reduced.assurance_absent
    assert "plan approval gate absent" in reduced.assurance_absent


def test_snapshot_survives_edits_and_archives(tmp_path: Path) -> None:
    store = _resolver_store(tmp_path)
    config = JevConfig()
    first = resolve_workflow_profile(
        ref="repo:fast",
        config=config,
        repo_root=tmp_path,
        user_root=tmp_path / "user",
        flags={},
        cda_available=False,
    )
    assert first is not None
    store.save(base_profile(quality_level="production"))
    store.archive("repo:fast")
    # The pinned snapshot still resolves to version 1's values.
    pinned = resolve_workflow_profile(
        ref="repo:fast@v1",
        config=config,
        repo_root=tmp_path,
        user_root=tmp_path / "user",
        flags={},
        cda_available=False,
    )
    assert pinned is not None and pinned.version == 1
    assert pinned.snapshot["quality_level"] != "production"
    assert first.snapshot["quality_level"] != "production"


def test_queue_time_pinning(tmp_path: Path) -> None:
    store = _resolver_store(tmp_path)
    pinned = pin_workflow_profile(ref="repo:fast", repo_root=tmp_path, user_root=tmp_path / "user")
    assert pinned == "repo:fast@v1"
    store.save(base_profile(quality_level="production"))
    # Pinning again picks up the version current then.
    assert (
        pin_workflow_profile(ref="repo:fast", repo_root=tmp_path, user_root=tmp_path / "user")
        == "repo:fast@v2"
    )
    # An already-pinned reference is left alone.
    assert (
        pin_workflow_profile(ref="repo:fast@v1", repo_root=tmp_path, user_root=tmp_path / "user")
        == "repo:fast@v1"
    )


def test_pinned_recurring_ref_floor_refusal(tmp_path: Path) -> None:
    # Partial coverage (issue #368 phases 0-2): the safety floor is enforced
    # at run creation by the single resolver, so a scheduler-enqueued pinned
    # reference refuses instead of running un-gated — never silently
    # applied. The profile itself saves clean (quality none, ship_enabled
    # absent so the repo value applies); the refusal triggers when the
    # run's repo has shipping on, which resolves ship_enabled True.
    store = ProfileStore(tmp_path, tmp_path / "user")
    profile = base_profile(quality_level="none")
    del profile["ship_enabled"]
    store.save(profile)
    pinned = pin_workflow_profile(ref="repo:fast", repo_root=tmp_path, user_root=tmp_path / "user")
    assert pinned == "repo:fast@v1"
    config = JevConfig()
    config.ship.enabled = True
    with pytest.raises(ProfileValidationError, match="Refused:"):
        resolve_workflow_profile(
            ref=pinned,
            config=config,
            repo_root=tmp_path,
            user_root=tmp_path / "user",
            flags={},
        )


def test_export_import_round_trip(tmp_path: Path) -> None:
    """Exported JSON imports cleanly into another store; invalid JSON is
    refused, never partially applied."""
    source = ProfileStore(tmp_path / "a", tmp_path / "user")
    stored = source.save(base_profile(quality_level="medium", priority="quality first"))
    exported = json.dumps(source.get("repo:fast"))
    target = ProfileStore(tmp_path / "b", tmp_path / "user")
    imported = target.save(json.loads(exported))
    assert imported["quality_level"] == stored["quality_level"]
    assert imported["priority"] == "quality first"
    assert imported["stage_list"] == stored["stage_list"]
    with pytest.raises(ProfileValidationError):
        target.save(json.loads('{"id": "repo:bad", "stage_list": []}'))
    assert not target.exists("repo:bad")
