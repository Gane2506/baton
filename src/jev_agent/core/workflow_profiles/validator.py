"""The workflow profile validator (issue #368, design section 7.5).

One function validates profile-format JSON, shared by the editor (save time)
and the executor (creation-time re-check): a save-time pass and a
creation-time pass use the same rules. Phase 2 carries the schema plus the
live catalogue subset — schema parse, unknown names against current
configuration, duplicate stages, empty list — plus the safety-floor refusals
(section 10.2, B1), which phase 2 owns. The full catalogue lands in phase 3.

Every failure is returned as a plain message naming the setting and stating
the fix; a save that fails lists every error, not just the first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .schema import (
    AFTER_APPROVAL,
    ALL_FIELDS,
    CACHE_MODES,
    CANONICAL_ORDER,
    CDA_GRADING,
    DESIGNING_MODES,
    GATE_POLICIES,
    GOALS,
    ORIGINS,
    PLANNER_POLICIES,
    QUALITY_LEVELS,
    REQUIRED_FIELDS,
    REQUIRED_STAGE_DEPENDENCY,
    REQUIRED_STAGES,
    ROLES,
    SCOPES,
    parse_profile_id,
    pretty_stage,
)

_STAGE_LIST_TEXT = "intake, planning, validation, design, execution, integration, shipping"


@dataclass
class ValidationContext:
    """Live configuration a profile is checked against (section 6.5): role
    model allowlists, learning profile names, and round-cap ceilings. Any
    field left None skips that check — the creation-time re-check always
    passes a full context, so a stale reference refuses there at the latest."""

    models_by_role: dict[str, list[str]] | None = None
    learning_profiles: set[str] | None = None
    ceilings: dict[str, int] | None = None


@dataclass
class ProfileValidationError(ValueError):
    """A profile that failed validation, with every error listed."""

    messages: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return "; ".join(self.messages)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _one_of(value: object, allowed: tuple[str, ...]) -> bool:
    return isinstance(value, str) and value in allowed


def validate_profile(data: object, *, context: ValidationContext | None = None) -> list[str]:
    """Validate profile-format JSON, returning every error message (empty
    when valid). `data` must be a JSON object; anything else is one error."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return [
            "A workflow profile must be a JSON object with an id, a stage list, and gate policies."
        ]
    ctx = context or ValidationContext()

    for key in sorted(data):
        if key not in ALL_FIELDS:
            errors.append(
                f"Unknown field {key!r}: profiles carry only the fields in section 6.6. Remove it."
            )
    for key in REQUIRED_FIELDS:
        if key not in data:
            errors.append(
                f"Missing field {key!r}: a profile needs an id, a display name, a scope, "
                "a stage list, and gate policies."
            )

    profile_id = data.get("id")
    scope = data.get("scope")
    if isinstance(profile_id, str):
        parsed = parse_profile_id(profile_id)
        if parsed is None:
            errors.append(
                f"Unknown profile id {profile_id!r}: ids look like 'repo:<slug>' or "
                "'user:<slug>', with a slug of letters, digits, '-' and '_' starting "
                "with a letter or digit, at most 40 characters."
            )
        elif isinstance(scope, str) and parsed[0] != scope:
            errors.append(
                f"Profile id {profile_id!r} does not match its scope {scope!r}: "
                "use the id for the scope the profile is saved under."
            )
    elif "id" in data:
        errors.append("Profile id must be a string like 'repo:<slug>' or 'user:<slug>'.")
    if "scope" in data and not _one_of(scope, SCOPES):
        errors.append(f"Unknown scope {scope!r}. Pick one of: repo, user.")
    if "display_name" in data and not (
        isinstance(data["display_name"], str) and data["display_name"].strip()
    ):
        errors.append("Display name must be a non-empty string.")
    version = data.get("version")
    if version is not None and not (_is_int(version) and version >= 1):
        errors.append("Version must be an integer from 1.")
    release = data.get("release")
    if release is not None and not (isinstance(release, str) and release.strip()):
        errors.append("Release must be a non-empty string.")

    _validate_stage_list(data, errors)
    _validate_gate_policies(data, errors)
    _validate_designing(data, errors)
    _validate_caps(data, errors, ctx)
    _validate_settings(data, errors, ctx)
    _validate_safety_floor(data, errors)
    _validate_shipping_coherence(data, errors)
    return errors


def _validate_stage_list(data: dict[str, Any], errors: list[str]) -> None:
    stages = data.get("stage_list")
    if stages is None:
        return  # missing-field error already recorded
    if not isinstance(stages, list) or not all(isinstance(s, str) for s in stages):
        errors.append("Stage list must be a list of stage names.")
        return
    if not stages:
        errors.append(
            "A flow needs at least intake, planning, validation, execution, and "
            "integration, in that order. Add the missing stages."
        )
        return
    seen: set[str] = set()
    for stage in stages:
        if stage in seen:
            errors.append(
                f"Duplicate stage {pretty_stage(stage)!r}: each stage appears once. "
                "Remove the duplicate."
            )
        seen.add(stage)
        if stage not in CANONICAL_ORDER:
            errors.append(
                f"Unknown stage {stage!r}. The stages are {_STAGE_LIST_TEXT}. "
                "Remove the unknown stage or spell it as shown."
            )
    known = [s for s in stages if s in CANONICAL_ORDER]
    order = [CANONICAL_ORDER.index(s) for s in known]
    if order != sorted(order):
        # First out-of-order pair, named with its dependency.
        for i in range(len(known) - 1):
            if order[i] > order[i + 1]:
                late, early = known[i], known[i + 1]
                if late == "EXECUTING" and early == "VALIDATING":
                    errors.append(
                        "Execution cannot run before validation: execution needs the "
                        "approved plan. Put validation before execution."
                    )
                else:
                    errors.append(
                        f"{pretty_stage(early).capitalize()} cannot run before "
                        f"{pretty_stage(late)}: the canonical order is "
                        f"{_STAGE_LIST_TEXT}. Put {pretty_stage(late)} before "
                        f"{pretty_stage(early)}."
                    )
                break
    for stage in REQUIRED_STAGES:
        if stage not in stages:
            errors.append(
                f"{pretty_stage(stage).capitalize()} cannot be removed: "
                f"{REQUIRED_STAGE_DEPENDENCY[stage]}. Add {pretty_stage(stage)} back."
            )


def _validate_gate_policies(data: dict[str, Any], errors: list[str]) -> None:
    policies = data.get("gate_policies")
    if policies is None:
        return  # missing-field error already recorded
    if not isinstance(policies, dict):
        errors.append("Gate policies must be a map from stage to policy.")
        return
    stages = data.get("stage_list")
    stage_set = set(stages) if isinstance(stages, list) else set()
    for stage, policy in policies.items():
        if stage not in CANONICAL_ORDER:
            errors.append(
                f"Unknown stage {stage!r} in gate policies. The stages are {_STAGE_LIST_TEXT}."
            )
            continue
        if stage not in stage_set:
            errors.append(
                f"The gate policy for {pretty_stage(stage)} has no stage to attach to. "
                "Add the stage back, or remove its gate policy."
            )
        if stage == "INTAKE":
            errors.append("Intake has no waiting state, so it cannot carry a gate policy.")
        elif not _one_of(policy, GATE_POLICIES):
            errors.append(
                f"Unknown gate policy {policy!r} for {pretty_stage(stage)}. "
                "Pick one of: inherit, require, skip."
            )


def _validate_designing(data: dict[str, Any], errors: list[str]) -> None:
    designing = data.get("designing", "when_needed")
    if not _one_of(designing, DESIGNING_MODES):
        errors.append(f"Unknown designing mode {designing!r}. Pick one of: when_needed, never.")
        return
    stages = data.get("stage_list")
    if not isinstance(stages, list):
        return
    if designing == "never" and "DESIGNING" in stages:
        errors.append(
            "Designing is 'never' but the stage list includes design. Remove design "
            "from the stage list, or set designing to 'when_needed'."
        )
    if designing == "when_needed" and "DESIGNING" not in stages:
        errors.append(
            "Designing is 'when_needed' but the stage list omits design. Add design "
            "to the stage list, or set designing to 'never'."
        )


def _validate_caps(data: dict[str, Any], errors: list[str], ctx: ValidationContext) -> None:
    caps = (
        ("max_plan_rounds", "max_plan_rounds"),
        ("max_fix_rounds", "max_fix_rounds"),
        ("score_max_retries", "score_max_retries"),
        ("max_task_attempts", "max_task_attempts"),
    )
    ceilings = ctx.ceilings or {}
    for key, ceiling_key in caps:
        value = data.get(key)
        if value is None:
            continue
        if not _is_int(value) or value < 1:
            errors.append(f"{key} must be an integer of at least 1.")
            continue
        ceiling = ceilings.get(ceiling_key)
        if ceiling is not None and value > ceiling:
            errors.append(
                f"{key} {value} is above this repo's ceiling {ceiling}: lower it to "
                f"{ceiling} or below — it is never silently clamped."
            )


def _validate_settings(data: dict[str, Any], errors: list[str], ctx: ValidationContext) -> None:
    quality = data.get("quality_level")
    if quality is not None and not _one_of(quality, QUALITY_LEVELS):
        errors.append(
            f"Unknown quality level {quality!r}. Pick one of: " + ", ".join(QUALITY_LEVELS) + "."
        )
    threshold = data.get("score_threshold")
    if threshold is not None and not (_is_int(threshold) and 0 <= threshold <= 100):
        errors.append("score_threshold must be an integer between 0 and 100.")
    skip_human = data.get("score_skip_human")
    if skip_human is not None and not isinstance(skip_human, bool):
        errors.append("score_skip_human must be true or false.")
    role_models = data.get("role_models")
    if role_models is not None:
        if not isinstance(role_models, dict):
            errors.append("role_models must be a map from role to model name.")
        else:
            for role, model in role_models.items():
                if role not in ROLES:
                    errors.append(f"Unknown role {role!r}. The roles are " + ", ".join(ROLES) + ".")
                    continue
                allowed = (ctx.models_by_role or {}).get(role)
                if allowed is not None and allowed and model not in allowed:
                    errors.append(
                        f"Unknown model {model!r} for role {role}. Pick one of: "
                        + ", ".join(allowed)
                        + "."
                    )
    policy = data.get("planner_policy")
    if policy is not None and not _one_of(policy, PLANNER_POLICIES):
        errors.append(
            f"Unknown planner policy {policy!r}. Pick one of: " + ", ".join(PLANNER_POLICIES) + "."
        )
    cache_mode = data.get("cache_mode")
    if cache_mode is not None and not _one_of(cache_mode, CACHE_MODES):
        errors.append(
            f"Unknown cache mode {cache_mode!r}. Pick one of: " + ", ".join(CACHE_MODES) + "."
        )
    grading = data.get("cda_grading")
    if grading is not None and not _one_of(grading, CDA_GRADING):
        errors.append(
            f"Unknown CDA grading {grading!r}. Pick one of: " + ", ".join(CDA_GRADING) + "."
        )
    ship_enabled = data.get("ship_enabled")
    if ship_enabled is not None and not isinstance(ship_enabled, bool):
        errors.append("ship_enabled must be true or false.")
    after = data.get("after_approval")
    if after is not None and not _one_of(after, AFTER_APPROVAL):
        errors.append(
            f"Unknown after_approval {after!r}. Pick one of: " + ", ".join(AFTER_APPROVAL) + "."
        )
    learning = data.get("learning_profile")
    if learning is not None:
        if not isinstance(learning, str) or not learning.strip():
            errors.append("learning_profile must be a non-empty string.")
        elif ctx.learning_profiles is not None and learning not in ctx.learning_profiles:
            errors.append(
                f"Unknown learning profile {learning!r}: create it first, or pick one "
                "of the existing learning profiles."
            )
    goal = data.get("goal")
    if goal is not None and not _one_of(goal, GOALS):
        errors.append(f"Unknown goal {goal!r}. Pick one of: " + ", ".join(GOALS) + ".")
    priority = data.get("priority")
    if priority is not None and not isinstance(priority, str):
        errors.append("priority must be a string (display only).")
    origin = data.get("origin")
    if origin is not None and not _one_of(origin, ORIGINS):
        errors.append(f"Unknown origin {origin!r}. Pick one of: " + ", ".join(ORIGINS) + ".")


def _validate_safety_floor(data: dict[str, Any], errors: list[str]) -> None:
    """B1 refusals (design section 10.2): a profile that sets quality `none`
    while shipping is enabled, or `score_skip_human` while shipping is
    enabled, does not save. Configuration-inherited values are label-only
    and are handled by assurance, not by this refusal."""
    ship_on = data.get("ship_enabled") is True
    if not ship_on:
        return
    if data.get("quality_level") == "none":
        errors.append(
            'Refused: quality level "none" while shipping is enabled would ship '
            "un-gated code. Turn shipping off, or raise the quality level."
        )
    if data.get("score_skip_human") is True:
        errors.append(
            "Refused: score_skip_human while shipping is enabled would skip the "
            "ship score gate. Turn shipping off, or keep the score gate."
        )


def _validate_shipping_coherence(data: dict[str, Any], errors: list[str]) -> None:
    """Section 7.3: SHIPPING may be omitted only when shipping is off."""
    if data.get("ship_enabled") is True:
        stages = data.get("stage_list")
        if isinstance(stages, list) and "SHIPPING" not in stages:
            errors.append(
                "Shipping is omitted but shipping is on. Turn shipping off, or include shipping."
            )


def validate_profile_or_raise(
    data: object, *, context: ValidationContext | None = None
) -> dict[str, Any]:
    """Validate and return the profile dict, or raise with every error."""
    errors = validate_profile(data, context=context)
    if errors:
        raise ProfileValidationError(errors)
    assert isinstance(data, dict)
    return data


def normalize_profile(data: dict[str, Any]) -> dict[str, Any]:
    """Fill the defaults a stored file may omit: `designing` defaults to
    `when_needed`, and a stage without a gate policy entry inherits the
    existing behaviour. A wholly missing `gate_policies` map is still an
    error (the validator reports it); this only fills entries, never a map."""
    normalized = dict(data)
    normalized.setdefault("designing", "when_needed")
    policies = normalized.get("gate_policies")
    stages = normalized.get("stage_list")
    if isinstance(policies, dict) and isinstance(stages, list):
        filled = dict(policies)
        for stage in stages:
            if stage != "INTAKE" and stage not in filled:
                filled[stage] = "inherit"
        normalized["gate_policies"] = filled
    return normalized
