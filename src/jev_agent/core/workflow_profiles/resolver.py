"""The one resolver function (issue #368, design sections 6.4, 9.2, 10).

Called from both run-creation entry points (`app/runs.py create_run` and
`Pipeline.start`), so every creation path applies the same precedence once,
stores the same snapshot, and computes the same assurance. A path that
cannot resolve a profile refuses with the setting named — never silently
falls back to the default.

Precedence (F1, explicit order table): per-run flags, then the profile
version, then repo configuration and the workspace profile, then Baton
built-in defaults. The snapshot records the winning source per setting in
`resolved_from`. A tier-3 win records `repo` or `workspace` per whichever
source the existing intra-tier resolution actually resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .assurance import compute_assurance
from .schema import parse_profile_ref
from .store import ProfileStore, ProfileStoreError
from .validator import (
    ProfileValidationError,
    ValidationContext,
    normalize_profile,
    validate_profile,
)

DEFAULT_REF = "default"


@dataclass(frozen=True)
class ResolvedProfile:
    """A run's resolved workflow profile: the snapshot to store plus the
    queryable columns. `snapshot` is the complete resolved parameter set —
    the executor reads only the snapshot, never the live profile."""

    profile_id: str
    version: int
    release: str | None
    snapshot: dict[str, Any]
    assurance: str
    assurance_absent: list[str]
    goal: str | None
    preset_goal: str | None


def build_validation_context(
    config: Any, *, learning_profiles: set[str] | None
) -> ValidationContext:
    """Live configuration for the creation-time re-check (section 6.5):
    role model allowlists (an empty list means the role has no configured
    restriction, so it constrains nothing), learning profile names, and
    round-cap ceilings from the target repo's configuration."""
    models_by_role: dict[str, list[str]] = {}
    for name, role_config in config.roles.items():
        models_by_role[name] = list(getattr(role_config, "models", []) or [])
    return ValidationContext(
        models_by_role=models_by_role,
        learning_profiles=learning_profiles,
        ceilings={
            "max_plan_rounds": config.budget.max_plan_rounds,
            "max_fix_rounds": config.integration.max_fix_rounds,
            "score_max_retries": config.quality.score_max_retries,
        },
    )


def _cda_available() -> bool:
    """Whether the Coding Decision Agent backend loads now (grading `on`
    with no backend refuses at creation; it does not run ungraded). The
    loader only checks presence — it never downloads weights — and the
    import is lazy, so most runs never touch the orchestrator package."""
    try:
        from jev_agent.code_orchestrator.backend import (
            CodeOrchestratorUnavailable,
            load_backend,
        )
    except Exception:
        return False
    try:
        load_backend()
    except CodeOrchestratorUnavailable:
        return False
    except Exception:
        return False
    return True


def resolve_workflow_profile(
    *,
    ref: str | None,
    config: Any,
    repo_root: Path | None,
    user_root: Path | None,
    flags: dict[str, Any] | None = None,
    autonomy_mode: str = "standard",
    plan_approval_required: bool = False,
    learning_profiles: set[str] | None = None,
    cda_available: bool | None = None,
) -> ResolvedProfile | None:
    """Resolve a creation field value to a snapshot, or None for the legacy
    path. `ref` None or `"default"` means no profile (design section 13.1:
    picker `default` always means no profile, not a preset that runs).
    `flags` carries tier-1 per-run values (`quality_level`, `ship_mode`,
    `learning_profile`) plus tier-3 source markers (`quality_source`,
    `learning_source`: "workspace" when the value came from the workspace
    profile, else "repo"). Raises ProfileStoreError / ProfileValidationError
    with the setting named on any failure."""
    if ref is None or ref == DEFAULT_REF:
        return None
    parsed = parse_profile_ref(ref)
    if parsed is None:
        raise ProfileStoreError(
            f"workflow profile {ref!r} no longer resolves: pick a current profile"
        )
    scope, slug, version = parsed
    profile_id = f"{scope}:{slug}"
    store = ProfileStore(repo_root, user_root)
    try:
        profile = store.get(profile_id, version)
    except ProfileStoreError as exc:
        # Name the id and version, and tell the user to pick a current profile.
        if version is not None:
            raise ProfileStoreError(
                f"workflow profile {profile_id!r} version {version} no longer resolves: "
                "pick a current profile"
            ) from exc
        raise
    context = build_validation_context(config, learning_profiles=learning_profiles)
    errors = validate_profile(profile, context=context)
    if errors:
        raise ProfileValidationError(errors)
    profile = normalize_profile(profile)
    flags = flags or {}

    resolved: dict[str, Any] = {}
    sources: dict[str, str] = {}

    def pick(
        key: str, flag: Any, profile_value: Any, repo_value: Any, repo_source: str = "repo"
    ) -> None:
        if flag is not None:
            resolved[key] = flag
            sources[key] = "flag"
        elif profile_value is not None:
            resolved[key] = profile_value
            sources[key] = "profile"
        else:
            resolved[key] = repo_value
            sources[key] = repo_source

    pick(
        "quality_level",
        flags.get("quality_level"),
        profile.get("quality_level"),
        flags.get("quality_repo_value", config.quality.level),
        flags.get("quality_source", "repo"),
    )
    pick("score_threshold", None, profile.get("score_threshold"), config.quality.score_threshold)
    pick(
        "score_max_retries",
        None,
        profile.get("score_max_retries"),
        config.quality.score_max_retries,
    )
    pick(
        "score_skip_human",
        None,
        profile.get("score_skip_human"),
        config.quality.score_skip_human,
    )
    pick("max_plan_rounds", None, profile.get("max_plan_rounds"), config.budget.max_plan_rounds)
    pick("max_fix_rounds", None, profile.get("max_fix_rounds"), config.integration.max_fix_rounds)
    pick(
        "max_task_attempts",
        None,
        profile.get("max_task_attempts"),
        config.budget.max_task_attempts,
    )
    pick(
        "planner_policy",
        None,
        profile.get("planner_policy"),
        getattr(config.models, "planner_policy", None),
    )
    pick(
        "cache_mode",
        None,
        profile.get("cache_mode"),
        "exact" if config.cache.enabled else "exact_off",
    )
    pick("cda_grading", None, profile.get("cda_grading"), "off")
    pick("ship_enabled", None, profile.get("ship_enabled"), config.ship.enabled)
    pick(
        "after_approval",
        flags.get("ship_mode"),
        profile.get("after_approval"),
        config.ship.after_approval or "ask",
    )
    # Learning is special: the caller already resolved flag/workspace/config
    # into one effective value, but the profile (tier 2) must still beat the
    # workspace/config value (tier 3) without beating an explicit flag (tier
    # 1). The flag is therefore the effective value only when the caller
    # says it came from a flag; otherwise the effective value is tier 3.
    learning_flag = flags.get("learning_profile") if flags.get("learning_profile_is_flag") else None
    pick(
        "learning_profile",
        learning_flag,
        profile.get("learning_profile"),
        flags.get("learning_profile"),
        flags.get("learning_source", "repo"),
    )
    role_models: dict[str, str] = {}
    role_sources: dict[str, str] = {}
    profile_roles = profile.get("role_models") or {}
    for name, _role_config in config.roles.items():
        if name in profile_roles:
            role_models[name] = profile_roles[name]
            role_sources[name] = "profile"
    resolved["role_models"] = role_models
    sources["role_models"] = role_sources  # type: ignore[assignment]

    resolved["stage_list"] = profile["stage_list"]
    resolved["gate_policies"] = profile["gate_policies"]
    resolved["designing"] = profile.get("designing", "when_needed")
    resolved["goal"] = profile.get("goal")
    resolved["priority"] = profile.get("priority")
    sources["stage_list"] = "profile"
    sources["gate_policies"] = "profile"
    sources["designing"] = "profile"

    # Safety floor, creation-time (B1 + section 7.2): refusals name the setting.
    ship_on = resolved["ship_enabled"] is True
    if profile.get("quality_level") == "none" and ship_on:
        raise ProfileValidationError(
            [
                'Refused: quality level "none" while shipping is enabled would ship '
                "un-gated code. Turn shipping off, or raise the quality level."
            ]
        )
    if profile.get("score_skip_human") is True and ship_on:
        raise ProfileValidationError(
            [
                "Refused: score_skip_human while shipping is enabled would skip the "
                "ship score gate. Turn shipping off, or keep the score gate."
            ]
        )
    if autonomy_mode == "autonomous" and any(
        policy == "require" for policy in resolved["gate_policies"].values()
    ):
        raise ProfileValidationError(
            [
                "Autonomous runs do not park for approval. Pick a gated mode or drop "
                "the required gate."
            ]
        )
    if resolved["cda_grading"] == "on":
        available = _cda_available() if cda_available is None else cda_available
        if not available:
            raise ProfileValidationError(
                [
                    "Refused: cda_grading is on but no Coding Decision Agent backend is "
                    "available. Install the backend, or set cda_grading to off."
                ]
            )

    if autonomy_mode == "autonomous":
        plan_gate_present = False
    elif autonomy_mode == "ask_every_step":
        plan_gate_present = True
    else:
        plan_gate_present = bool(
            plan_approval_required or resolved["gate_policies"].get("VALIDATING") == "require"
        )
    assurance, absent = compute_assurance(
        quality_level=resolved["quality_level"],
        score_skip_human=bool(resolved["score_skip_human"]),
        autonomy_mode=autonomy_mode,
        plan_gate_present=plan_gate_present,
    )

    version_resolved = int(profile["version"]) if profile.get("version") is not None else 1
    snapshot: dict[str, Any] = {
        **resolved,
        "resolved_from": sources,
        "workflow_profile_id": profile_id,
        "workflow_profile_version": version_resolved,
        "workflow_profile_release": profile.get("release"),
        "assurance_absent": absent,
    }
    return ResolvedProfile(
        profile_id=profile_id,
        version=version_resolved,
        release=profile.get("release"),
        snapshot=snapshot,
        assurance=assurance,
        assurance_absent=absent,
        goal=None,
        preset_goal=None,
    )


def pin_workflow_profile(*, ref: str, repo_root: Path | None, user_root: Path | None) -> str:
    """Resolve a creation field value to its pinned form
    `<scope>:<slug>@v<n>` at queue time: the queued run executes the version
    it was queued with, even if the profile is edited before execution."""
    parsed = parse_profile_ref(ref)
    if parsed is None:
        raise ProfileStoreError(
            f"workflow profile {ref!r} no longer resolves: pick a current profile"
        )
    scope, slug, version = parsed
    profile_id = f"{scope}:{slug}"
    if version is not None:
        return ref
    store = ProfileStore(repo_root, user_root)
    current = store.get(profile_id)
    return f"{profile_id}@v{current['version']}"
