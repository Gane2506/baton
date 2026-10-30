"""Built-in workflow presets (issue #368, phase 4, design sections 5, 6.2, 9.1).

The three preset files in this package are data in the profile format,
loaded with ``importlib.resources`` (the same pattern the roles package uses
for its designer files) and validated by the shared validator. A preset is
identified by the Baton release that shipped it (its ``release`` field, kept
in step with the package version), never by an integer version, and it is
never edited in place: a user who edits a preset creates a repo- or
user-scope copy under a new id.

What the files deliberately do NOT carry, and why (design section 9.1):

* No ``quality_level`` on Efficiency: the preset never lowers the repo's
  level, so the level resolves from the repo configuration. Accuracy carries
  ``production`` as a floor only: the resolver keeps a stricter repo level
  (``scalable`` stays ``scalable``).
* No ``planner_policy`` on either preset: a preset never narrows the repo's
  policy.
* No ``score_threshold`` on either preset: forcing 85 would lower a repo
  that configured a stricter threshold, and the table's point is that the
  bar is never lowered to save tokens. Under the default configuration the
  threshold resolves to 85, the section 9.1 value.
* No ``cache_mode`` on either preset: the preset never disables exact
  reuse, and never forces it on over a repo that deliberately turned the
  cache off. Under the default configuration (cache enabled) it resolves
  to ``exact``, the section 9.1 value.
* No ``role_models`` on Efficiency: the section 9.1 price rule names
  ``[dispatcher.cost]`` configured prices as the price source, and that
  section holds leg prices (native/OpenRouter per-million rates), not
  per-model prices — so no model has a configured price, every role is
  unpriced, and Efficiency leaves every role at the repo configuration.
  The picker says so (see ``preset_picker_entries``); inventing a price
  order would violate section 7.4 ("a profile never invents a price").
  Efficiency's savings therefore come from fewer rounds only, never from
  a cheaper-model claim the configuration cannot support.
* No ship, learning, or autonomy settings on any preset: those stay at
  the repo configuration / creation setting, as the table states.

Picker ``default`` always means no profile (design section 13.1, G1): it
is the legacy path, not the ``preset:default`` file. The file exists as the
data form of the default parameter set (the conformance reference) and as
an explicit ``preset:default`` pick, which resolves to the same parameters
with a snapshot recorded.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from jev_agent.core.workflow_profiles.assurance import (
    assurance_label_text,
    compute_assurance,
)
from jev_agent.core.workflow_profiles.schema import is_valid_slug
from jev_agent.core.workflow_profiles.validator import (
    ValidationContext,
    validate_profile,
)
from jev_agent.quality.levels import QualityLevel, rank

PRESET_NAMES: tuple[str, ...] = ("default", "efficiency", "accuracy")

# The unpriced-role notice (design section 9.1): shown on the Efficiency
# picker entry, where a cheaper-model claim would otherwise be implied.
UNPRICED_ROLES_NOTICE = (
    "No role has a configured model price: [dispatcher.cost] holds leg prices, "
    "not per-model prices, so every role stays at the repo configuration and "
    "Efficiency's savings come from fewer rounds only."
)

_EFFICIENCY_DESCRIPTION = (
    "Fewer rounds: plan at most 2, fix at most 1, score retries at most 1. "
    "Quality level, planner policy, and cache stay at the repo configuration "
    "(never lowered or narrowed). CDA grading off. "
)
_ACCURACY_DESCRIPTION = (
    "Full canonical stage list. CDA grading on (advisory only, threshold stays "
    "0.7; if no CDA backend is installed, creation refuses instead of running "
    "ungraded). Quality at least production: a stricter repo level is kept. "
    "Round caps as configured (plan 3, fix 2, score retries 2). "
)
_DEFAULT_DESCRIPTION = (
    "No profile: today's pipeline exactly as configured (the legacy path). "
    "Quality, models, and round caps come from jev.toml; no snapshot is stored. "
)


def preset_id(name: str) -> str:
    """The preset reference for a preset name: ``preset:<name>``."""
    return f"preset:{name}"


def parse_preset_ref(ref: str) -> str | None:
    """A creation-field preset reference (``preset:<slug>``) as its name, or
    None when the value is not that shape. Membership in PRESET_NAMES is the
    caller's check, so an unknown slug still names itself in the refusal."""
    if not ref.startswith("preset:"):
        return None
    slug = ref[len("preset:") :]
    return slug if is_valid_slug(slug) else None


def is_preset_ref(ref: str | None) -> bool:
    """Whether a creation-field value is a preset reference at all."""
    return ref is not None and parse_preset_ref(ref) is not None


def load_preset(name: str) -> dict[str, Any]:
    """One preset file as profile-format JSON. Raises ValueError naming the
    preset when the name is unknown or the file cannot be read — a preset
    failure is never a silent fallback to another value."""
    if name not in PRESET_NAMES:
        raise ValueError(
            f"unknown workflow preset {name!r}: pick one of: " + ", ".join(PRESET_NAMES) + "."
        )
    try:
        text = (
            files("jev_agent.core.workflow_profiles.presets")
            .joinpath(f"{name}.json")
            .read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError(f"workflow preset {name!r} cannot be read: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"workflow preset {name!r} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"workflow preset {name!r} must be one profile object")
    return data


def validate_presets(context: ValidationContext | None = None) -> dict[str, list[str]]:
    """Every shipped preset against the shared validator, keyed by preset
    name. An empty list for a preset means it validates against the given
    live configuration; the dashboard runs this at startup and run creation
    runs it again, so a stale preset (a removed model, a renamed check, a
    cap above a lowered ceiling) refuses before anything is spent."""
    return {name: validate_profile(load_preset(name), context=context) for name in PRESET_NAMES}


def _autonomy_mode(config: Any) -> str:
    mode = getattr(getattr(config, "autonomy", None), "mode", "standard")
    return getattr(mode, "value", mode)


def _plan_gate_present(autonomy_mode: str, plan_approval_required: bool) -> bool:
    if autonomy_mode == "autonomous":
        return False
    if autonomy_mode == "ask_every_step":
        return True
    return bool(plan_approval_required)


def preset_picker_entries(config: Any, *, plan_approval_required: bool) -> list[dict[str, Any]]:
    """The run-creation picker's preset entries with their documented
    behaviour shown before selection (design section 13.1): the behaviour
    text, the assurance preview computed from the repo's current settings
    with the same assurance function creation uses (a per-run flag chosen
    after the preview can still change it; the creation value is stored),
    the Accuracy priority row, and the unpriced-role notice on Efficiency.
    Picker ``default`` is the first entry and always means no profile."""
    autonomy_mode = _autonomy_mode(config)
    repo_quality: QualityLevel = config.quality.level
    plan_gate = _plan_gate_present(autonomy_mode, plan_approval_required)

    def assurance_for(quality: QualityLevel) -> dict[str, Any]:
        assurance, absent = compute_assurance(
            quality_level=quality,
            score_skip_human=bool(config.quality.score_skip_human),
            autonomy_mode=autonomy_mode,
            plan_gate_present=plan_gate,
        )
        return {
            "assurance": assurance,
            "assurance_absent": absent,
            "assurance_label": assurance_label_text(assurance, absent),
        }

    entries: list[dict[str, Any]] = [
        {
            "ref": "default",
            "name": "Default",
            "goal": None,
            "priority": None,
            "description": _DEFAULT_DESCRIPTION,
            "unpriced_notice": None,
            **assurance_for(repo_quality),
        }
    ]
    for name in ("efficiency", "accuracy"):
        preset = load_preset(name)
        if name == "efficiency":
            quality: QualityLevel = repo_quality
            description = _EFFICIENCY_DESCRIPTION + UNPRICED_ROLES_NOTICE
            notice = UNPRICED_ROLES_NOTICE
        else:
            quality = repo_quality if rank(repo_quality) >= rank("production") else "production"
            description = _ACCURACY_DESCRIPTION
            notice = None
        entries.append(
            {
                "ref": preset_id(name),
                "name": str(preset.get("display_name", name)),
                "goal": preset.get("goal"),
                "priority": preset.get("priority"),
                "description": description,
                "unpriced_notice": notice,
                **assurance_for(quality),
            }
        )
    return entries


__all__ = [
    "PRESET_NAMES",
    "UNPRICED_ROLES_NOTICE",
    "is_preset_ref",
    "load_preset",
    "parse_preset_ref",
    "preset_id",
    "preset_picker_entries",
    "validate_presets",
]
