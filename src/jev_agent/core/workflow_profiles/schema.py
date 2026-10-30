"""Workflow profile schema constants (issue #368, design section 6.6).

The field lists are normative: a profile file carries exactly these fields,
and anything else is refused rather than dropped (design section 14), so a
user never believes a setting is saved when it is not.
"""

from __future__ import annotations

import re

# Canonical work-state order (design section 7.1). A profile's stage list is
# a subsequence of this order; reordering is refused.
CANONICAL_ORDER: tuple[str, ...] = (
    "INTAKE",
    "PLANNING",
    "VALIDATING",
    "DESIGNING",
    "EXECUTING",
    "INTEGRATING",
    "SHIPPING",
)

# Plain stage names the editor shows and the catalogue messages use.
STAGE_NAMES: dict[str, str] = {
    "INTAKE": "intake",
    "PLANNING": "planning",
    "VALIDATING": "validation",
    "DESIGNING": "design",
    "EXECUTING": "execution",
    "INTEGRATING": "integration",
    "SHIPPING": "shipping",
}

# Stages a profile may not omit (design section 7.3, normative list).
REQUIRED_STAGES: tuple[str, ...] = ("INTAKE", "PLANNING", "VALIDATING", "EXECUTING", "INTEGRATING")

# Dependency each required stage's omission message names (section 7.5).
REQUIRED_STAGE_DEPENDENCY: dict[str, str] = {
    "INTAKE": "planning needs the captured idea",
    "PLANNING": "validation needs a plan to validate",
    "VALIDATING": "integration needs a validated plan",
    "EXECUTING": "integration needs executed code to integrate",
    "INTEGRATING": "shipping needs an integrated result",
}

GATE_POLICIES: tuple[str, ...] = ("inherit", "require", "skip")
DESIGNING_MODES: tuple[str, ...] = ("when_needed", "never")
QUALITY_LEVELS: tuple[str, ...] = ("none", "basic", "medium", "production", "scalable")
PLANNER_POLICIES: tuple[str, ...] = (
    "free_training_ok",
    "mixed",
    "mixed_no_training",
    "paid_only",
)
CACHE_MODES: tuple[str, ...] = ("exact", "exact_off")
CDA_GRADING: tuple[str, ...] = ("off", "on")
AFTER_APPROVAL: tuple[str, ...] = ("merge", "ask")
GOALS: tuple[str, ...] = ("efficiency", "accuracy", "custom")
ORIGINS: tuple[str, ...] = ("user", "suggested")
SCOPES: tuple[str, ...] = ("repo", "user")
ROLES: tuple[str, ...] = (
    "planner",
    "validator",
    "coder",
    "reviewer",
    "code_reviewer",
    "learner",
    "designer",
)

# Required fields (design section 6.6, marked *). `version` is required for
# user/repo profiles; presets carry `release` instead (phase 4).
REQUIRED_FIELDS: tuple[str, ...] = ("id", "display_name", "scope", "stage_list", "gate_policies")

OPTIONAL_FIELDS: tuple[str, ...] = (
    "version",
    "release",
    "designing",
    "max_plan_rounds",
    "max_fix_rounds",
    "score_max_retries",
    "max_task_attempts",
    "quality_level",
    "score_threshold",
    "score_skip_human",
    "role_models",
    "planner_policy",
    "cache_mode",
    "cda_grading",
    "ship_enabled",
    "after_approval",
    "learning_profile",
    "goal",
    "priority",
    "origin",
)

ALL_FIELDS: frozenset[str] = frozenset(REQUIRED_FIELDS) | frozenset(OPTIONAL_FIELDS)

# Profile JSON size cap (design section 14): a starting value, refused with
# the size stated when exceeded.
MAX_PROFILE_BYTES = 64 * 1024

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


def is_valid_slug(slug: str) -> bool:
    """The existing profile-name rule: letters, digits, `-` and `_`, starting
    with a letter or digit, at most 40 characters."""
    return bool(_SLUG_RE.match(slug))


def parse_profile_id(profile_id: str) -> tuple[str, str] | None:
    """`<scope>:<slug>` split, or None when the id is not that shape."""
    if ":" not in profile_id:
        return None
    scope, slug = profile_id.split(":", 1)
    if scope not in SCOPES or not is_valid_slug(slug):
        return None
    return scope, slug


def parse_profile_ref(ref: str) -> tuple[str, str, int | None] | None:
    """A creation field value: `<scope>:<slug>` (current version) or
    `<scope>:<slug>@v<n>` (pinned version, as queued). None when malformed."""
    version: int | None = None
    base = ref
    if "@v" in ref:
        base, _, tail = ref.rpartition("@v")
        if not tail.isdigit() or int(tail) < 1:
            return None
        version = int(tail)
    parsed = parse_profile_id(base)
    if parsed is None:
        return None
    return parsed[0], parsed[1], version


def profile_id_for(scope: str, slug: str) -> str:
    return f"{scope}:{slug}"


def pretty_stage(stage: str) -> str:
    return STAGE_NAMES.get(stage, stage.lower())
