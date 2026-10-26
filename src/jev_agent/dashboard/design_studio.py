"""The Design Studio's server-side wizard state and creation pipeline (issue #124).

The studio is a standalone creation interface at `/design-studio`, independent
from the main chat: a guided stepper (Purpose -> Business -> App type -> Stack ->
Brand -> Direction -> Generate) that mirrors the `jev design new` CLI wizard's six questions
plus the Design direction step (issue #301), where the compiled brief is shown
and tweaked before generation. The dashboard keeps each visitor's answers in a
session-keyed store (the same shape as the flash store in `dashboard/app.py`),
and the final step runs the `core/design_pipeline.py` pipeline (brief ->
brief-enforced seeded generation -> quality gates -> motion/scene files) and
saves with `save_library_system()`.

Nothing here touches credentials or the network; the state never leaves the
dashboard process.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any, cast

from jev_agent.core.design_brief import (
    HARMONY_KEYS,
    LAYOUT_KEYS,
    LAYOUT_PATTERNS,
    MOTION_KEYS,
    TYPE_PAIRING_KEYS,
    DesignBrief,
    brief_fingerprint,
    compile_brief,
)
from jev_agent.core.design_business import AXIS_KEYS, BusinessContext, propose_elements
from jev_agent.core.design_codegen import motion_files
from jev_agent.core.design_gates import contrast_gate
from jev_agent.core.design_kit import (
    APP_TYPES,
    STACKS,
    THEMES,
    design_system_markdown,
    palette_for_vibe,
    tokens_for_app_type,
)
from jev_agent.core.design_library import (
    library_fingerprints,
    library_system_path,
    list_library,
    save_library_system,
    validate_library_name,
)
from jev_agent.core.design_motion3d import SCENE_TYPES
from jev_agent.core.design_pipeline import brief_meta, generate_briefed_system
from jev_agent.core.design_showcase import VIBE_PROFILES, rich_tokens, vibe_for_keywords
from jev_agent.core.design_showcase_html import render_showcase
from jev_agent.core.design_skills import (
    COLOR_STRATEGIES,
    COMPONENT_VARIANTS,
    LAYOUT_PROFILES,
    MOTION_PROFILES,
    TYPE_PAIRINGS,
    rich_tokens_for_seeded,
    seeded_vibe_profile,
)
from jev_agent.core.logo import generate_mark, mark_template, mark_to_png, mark_to_svg
from jev_agent.dashboard.studio_decisions import (
    STACK_REC_KEY,
    StackRecommendation,
    default_recommendation,
)

STUDIO_STEPS: tuple[str, ...] = (
    "purpose",
    "business",
    "app_type",
    "stack",
    "brand",
    "direction",
    "generate",
)
"""The wizard's steps in order (mirrors the stepper labels on the page)."""

STEP_LABELS: dict[str, str] = {
    "purpose": "Purpose",
    "business": "Business",
    "app_type": "App type",
    "stack": "Stack",
    "brand": "Brand",
    "direction": "Direction",
    "generate": "Generate",
}

AXIS_LABELS: dict[str, str] = {
    "warmth": "Warmth",
    "contrast": "Contrast",
    "density": "Density",
    "geometry": "Geometry",
    "formality": "Formality",
    "motion": "Motion",
    "type_personality": "Type personality",
}
"""Human labels for the brief's seven axes (the Direction step's sliders)."""

# The direction step's choice selects: key -> (valid keys, brief attribute).
_DIRECTION_CHOICES: tuple[tuple[str, frozenset[str], str], ...] = (
    ("harmony", HARMONY_KEYS, "harmony"),
    ("type_pairing", TYPE_PAIRING_KEYS, "type_pairing"),
    ("layout", LAYOUT_KEYS, "layout"),
    ("motion_profile", MOTION_KEYS, "motion"),
    ("scene", frozenset(SCENE_TYPES), "scene"),
    ("layout_pattern", LAYOUT_PATTERNS, "layout_pattern"),
)

_MAX_PRODUCT_LEN = 120
_MAX_VIBE_LEN = 200


class WizardError(ValueError):
    """A wizard answer failed validation; the message is safe to show the user."""


def _str(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    return value.strip() if isinstance(value, str) else ""


def stack_options(app_type: str, recommendation: str | None = None) -> tuple[list[str], str]:
    """The stack choices for `app_type`: the ordered list and the recommended one.

    The recommendation comes first. DS-03: the wizard passes the cached Jev
    pick as `recommendation`; without one (or when it is not a listed stack) the
    recommendation is the design kit's static default — the first entry of
    `STACKS[app_type]`, like the CLI wizard's.
    """
    stacks = list(STACKS[app_type])
    first = stacks[0]
    if recommendation is not None and recommendation in stacks:
        first = recommendation
    return [first, *(s for s in stacks if s != first)], first


class StudioWizard:
    """Server-side wizard state, keyed by dashboard session.

    Mirrors the flash store's shape (bounded, TTL-pruned): the session cookie
    is attacker-influenceable, so a session that starts the wizard but never
    finishes must not grow the store without bound.
    """

    _TTL_S = 3600
    _MAX_SESSIONS = 200

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, dict[str, dict[str, str]]] = {}
        self._seen: dict[str, float] = {}

    def get(self, session: str) -> dict[str, dict[str, str]]:
        """This session's answers so far (`{step: {field: value}}`), possibly {}."""
        with self._lock:
            self._prune()
            return {step: dict(data) for step, data in self._states.get(session, {}).items()}

    def put(self, session: str, step: str, data: dict[str, str]) -> None:
        """Record a validated step's answers for this session."""
        with self._lock:
            self._prune()
            state = self._states.setdefault(session, {})
            state[step] = dict(data)
            self._seen[session] = time.monotonic()
            # Bound the store: drop the oldest sessions first.
            while len(self._states) > self._MAX_SESSIONS:
                oldest = min(self._seen, key=lambda key: self._seen[key])
                del self._states[oldest]
                del self._seen[oldest]

    def clear(self, session: str) -> None:
        """Forget a session's answers (start over, or a finished generation)."""
        with self._lock:
            self._states.pop(session, None)
            self._seen.pop(session, None)

    def _prune(self) -> None:
        """Drop sessions idle past the TTL (called with the lock held)."""
        cutoff = time.monotonic() - self._TTL_S
        for session in [key for key, seen in self._seen.items() if seen < cutoff]:
            del self._seen[session]
            self._states.pop(session, None)


def first_incomplete_step(state: Mapping[str, Mapping[str, str]]) -> str | None:
    """The earliest step with no answers yet (None when the wizard is complete)."""
    for step in STUDIO_STEPS:
        if step != "generate" and not state.get(step):
            return step
    return None


def validate_step(
    step: str, form: Mapping[str, object], state: Mapping[str, Mapping[str, str]]
) -> dict[str, str]:
    """Validate one wizard step's POSTed fields against the answers so far.

    Returns the step's answers to store; raises WizardError with a user-facing
    message when the step cannot be advanced. Earlier steps must already be
    complete, so a forged POST cannot skip ahead.
    """
    if step == "purpose":
        return _validate_purpose(form)
    if step == "business":
        return _validate_business(form, state)
    if step == "app_type":
        return _validate_app_type(form)
    if step == "stack":
        return _validate_stack(form, state)
    if step == "brand":
        return _validate_brand(form)
    if step == "direction":
        return _validate_direction(form, state)
    raise WizardError(f"unknown wizard step {step!r}")


def _require_earlier(state: Mapping[str, Mapping[str, str]], *steps: str) -> None:
    for earlier in steps:
        if not state.get(earlier):
            raise WizardError(f"answer {STEP_LABELS[earlier]!r} first, then come back to this step")


def _validate_purpose(form: Mapping[str, object]) -> dict[str, str]:
    """Step 1 (easy first): the product name — free text, one question."""
    product = _str(form, "product")
    if not product:
        raise WizardError("Give the product a name so the studio knows what it is designing for.")
    if len(product) > _MAX_PRODUCT_LEN:
        raise WizardError(f"Keep the product name under {_MAX_PRODUCT_LEN} characters.")
    return {"product": product}


def _validate_business(
    form: Mapping[str, object], state: Mapping[str, Mapping[str, str]]
) -> dict[str, str]:
    """Step 2 (issue #283): the business context — all five fields optional.

    The returned dict always carries all five keys, even when every field is
    empty: an all-empty dict is falsy, and `first_incomplete_step` would treat
    a skipped business step as unanswered and loop the wizard forever.
    """
    _require_earlier(state, "purpose")
    try:
        context = BusinessContext.from_form(form)
    except ValueError as exc:
        raise WizardError(str(exc)) from exc
    return {
        "industry": context.industry,
        "audience": context.audience,
        "tone": context.tone,
        "offering": context.offering,
        "description": context.description,
    }


def _validate_app_type(form: Mapping[str, object]) -> dict[str, str]:
    """Step 3: the app type — one choice from the design kit's four."""
    app_type = _str(form, "app_type")
    if app_type not in APP_TYPES:
        raise WizardError("Pick one of the four app types to continue.")
    return {"app_type": app_type}


def _validate_stack(
    form: Mapping[str, object], state: Mapping[str, Mapping[str, str]]
) -> dict[str, str]:
    """Step 4: the stack — the recommended option is listed first."""
    _require_earlier(state, "app_type")
    app_type = state["app_type"]["app_type"]
    stack = _str(form, "stack")
    options, _recommended = stack_options(app_type)
    if stack not in options:
        raise WizardError("Pick one of the listed stacks to continue.")
    return {"stack": stack}


def _validate_brand(form: Mapping[str, object]) -> dict[str, str]:
    """Step 5: the brand — system name (validated), vibe profile + keywords, theme."""
    name = _str(form, "name")
    try:
        validate_library_name(name)
    except ValueError as exc:
        raise WizardError(str(exc)) from exc
    if name in list_library():
        raise WizardError(
            f'a design system named "{name}" already exists in the library — '
            "pick a different name (the studio only creates new systems)"
        )
    vibe_profile = _str(form, "vibe_profile")
    if vibe_profile and vibe_profile not in VIBE_PROFILES:
        raise WizardError("Pick one of the listed vibes.")
    vibe = _str(form, "vibe")
    if len(vibe) > _MAX_VIBE_LEN:
        raise WizardError(f"Keep the vibe keywords under {_MAX_VIBE_LEN} characters.")
    theme = _str(form, "theme") or "both"
    if theme not in THEMES:
        raise WizardError("Pick a theme: light, dark, or both.")
    return {"name": name, "vibe": vibe, "vibe_profile": vibe_profile, "theme": theme}


# ---- the Design direction step (issue #301) -------------------------------------------
# The brief is compiled from the answers so far (business + vibe + app type)
# and shown for tweaking. Only *changes* from the compiled brief are stored:
# the form always submits every slider/select (even untouched), so storing
# the submitted values verbatim would turn "no tweaks" into explicit
# overrides — and the brief's seed is derived from its overrides, which
# would move the anchor hue away from the preview the visitor just saw.
# A submitted step with no changes stores just {"complete": "1"} (a
# non-empty dict, so `first_incomplete_step` counts the step as done and
# generation compiles the brief with no overrides — exactly the preview).


def _state_business_context(state: Mapping[str, Mapping[str, str]]) -> BusinessContext:
    business = state.get("business") or {}
    return BusinessContext(
        industry=business.get("industry", ""),
        audience=business.get("audience", ""),
        tone=business.get("tone", ""),
        offering=business.get("offering", ""),
        description=business.get("description", ""),
    )


def _compile_brief_from_state(
    state: Mapping[str, Mapping[str, str]],
    overrides: Mapping[str, object] | None = None,
) -> DesignBrief:
    """The brief for the wizard state so far (business + vibe + app type).

    Raises WizardError when the earlier answers are missing or the app
    type is unknown, so a forged or stale state never reaches generation.
    """
    app_type = (state.get("app_type") or {}).get("app_type", "")
    if app_type not in APP_TYPES:
        raise WizardError("Pick one of the four app types to continue.")
    brand = state.get("brand") or {}
    try:
        return compile_brief(
            _state_business_context(state),
            brand.get("vibe", ""),
            app_type,
            overrides=overrides,
        )
    except ValueError as exc:
        raise WizardError(str(exc)) from exc


def _direction_overrides(state: Mapping[str, Mapping[str, str]]) -> dict[str, object]:
    """The stored Direction tweaks as `compile_brief` overrides ({} when
    the step was skipped or submitted unchanged). Stored values are strings;
    axes parse back to floats, the avoid-list back to a list of lines.
    Unparseable values are ignored — the step validated them on submit, so
    a bad value here means a tampered session, and the compiled default is
    the safe answer."""
    direction = state.get("direction") or {}
    overrides: dict[str, object] = {}
    for axis in AXIS_KEYS:
        raw = (direction.get(f"axis_{axis}") or "").strip()
        if not raw:
            continue
        try:
            overrides[axis] = float(raw)
        except ValueError:
            continue
    for key, _allowed, _attr in _DIRECTION_CHOICES:
        raw = (direction.get(key) or "").strip()
        if raw:
            overrides[key] = raw
    focal = (direction.get("focal_point") or "").strip()
    if focal:
        overrides["focal_point"] = focal
    if "avoid" in direction:
        overrides["avoid"] = [
            line.strip() for line in direction["avoid"].splitlines() if line.strip()
        ]
    return overrides


def _validate_direction(
    form: Mapping[str, object], state: Mapping[str, Mapping[str, str]]
) -> dict[str, str]:
    """Step 6: the design direction — every field optional, each tweak
    stored only when it differs from the compiled brief.

    Sliders post 0..100 (percent); the brief's axes are 0..1. Selects post
    a catalog key. The avoid-list posts one rule per line. A non-numeric
    slider or an unknown choice key is a WizardError; an out-of-range
    slider is clamped to the brief's 0..1 range.
    """
    _require_earlier(state, "purpose", "business", "app_type", "stack", "brand")
    brief = _compile_brief_from_state(state)
    tweaks: dict[str, str] = {}
    for axis in AXIS_KEYS:
        raw = _str(form, f"axis_{axis}")
        if not raw:
            continue
        try:
            posted = int(raw)
        except ValueError:
            try:
                posted = int(float(raw))
            except ValueError:
                raise WizardError(
                    f"{AXIS_LABELS[axis]} must be a number between 0 and 100."
                ) from None
        posted = max(0, min(100, posted))
        if posted != round(brief.axes[axis] * 100):
            tweaks[f"axis_{axis}"] = f"{posted / 100:.2f}"
    for key, allowed, attr in _DIRECTION_CHOICES:
        raw = _str(form, key)
        if not raw:
            continue
        if raw not in allowed:
            raise WizardError(f"Pick one of the listed {key.replace('_', ' ')} options.")
        if key == "layout_pattern":
            app_type = state["app_type"]["app_type"]
            variants = COMPONENT_VARIANTS.get(app_type, COMPONENT_VARIANTS["website"])
            if raw not in {variant.key for variant in variants}:
                raise WizardError(
                    f"Pick one of the listed layout patterns for {APP_TYPES[app_type]}."
                )
        if raw != getattr(brief, attr):
            tweaks[key] = raw
    focal = _str(form, "focal_point")
    if focal and focal != brief.focal_point:
        tweaks["focal_point"] = focal
    avoid_raw = _str(form, "avoid")
    if "avoid" in form:
        lines = [line.strip() for line in avoid_raw.splitlines() if line.strip()]
        if lines != list(brief.avoid):
            tweaks["avoid"] = "\n".join(lines)
    return {"complete": "1", **tweaks}


def _brief_preview(brief: DesignBrief, state: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    """The compiled brief as template context: axes (0..100 for the
    sliders), the OKLCH palette ramp, the named choices with their display
    names, the option lists for the selects, and the avoid-list — plus the
    *effective* values (stored tweaks over the compiled defaults) the form
    fields render, so revisiting the step shows what was submitted."""
    direction = state.get("direction") or {}
    app_type = brief.app_type
    variants = COMPONENT_VARIANTS.get(app_type, COMPONENT_VARIANTS["website"])
    current: dict[str, object] = {"complete": direction.get("complete", "")}
    for axis in AXIS_KEYS:
        raw = (direction.get(f"axis_{axis}") or "").strip()
        try:
            current[f"axis_{axis}"] = (
                round(float(raw) * 100) if raw else round(brief.axes[axis] * 100)
            )
        except ValueError:
            current[f"axis_{axis}"] = round(brief.axes[axis] * 100)
    for key, _allowed, attr in _DIRECTION_CHOICES:
        current[key] = direction.get(key) or getattr(brief, attr)
    current["focal_point"] = direction.get("focal_point") or brief.focal_point
    current["avoid"] = direction.get("avoid") or "\n".join(brief.avoid)
    # Display names for the *effective* choices (stored tweaks over the
    # compiled defaults), for the Generate step's summary.
    harmonies = [{"key": s.key, "name": s.name} for s in COLOR_STRATEGIES]
    type_pairings = [{"key": p.key, "name": p.name} for p in TYPE_PAIRINGS]
    layouts = [{"key": p.key, "name": p.name} for p in LAYOUT_PROFILES]
    motions = [{"key": p.key, "name": p.name} for p in MOTION_PROFILES]
    scenes = [{"key": key, "label": value["label"]} for key, value in SCENE_TYPES.items()]
    layout_patterns = [{"key": v.key, "name": v.name} for v in variants]

    def _name(options: list[dict[str, str]], key: object, field: str = "name") -> str:
        return next((o[field] for o in options if o["key"] == key), str(key))

    current_display = {
        "harmony": _name(harmonies, current["harmony"]),
        "type_pairing": _name(type_pairings, current["type_pairing"]),
        "layout": _name(layouts, current["layout"]),
        "motion": _name(motions, current["motion_profile"]),
        "scene": _name(scenes, current["scene"], "label"),
        "layout_pattern": _name(layout_patterns, current["layout_pattern"]),
        "focal_point": str(current["focal_point"]),
    }
    return {
        "axes": [
            {
                "key": axis,
                "label": AXIS_LABELS[axis],
                "value": round(brief.axes[axis] * 100),
            }
            for axis in AXIS_KEYS
        ],
        "palette_ramp": list(brief.palette_ramp),
        "primary_hex": brief.primary_hex,
        "anchor_hue": round(brief.anchor_hue, 1),
        "industry": brief.industry,
        "harmony": {
            "key": brief.harmony,
            "name": next(s.name for s in COLOR_STRATEGIES if s.key == brief.harmony),
        },
        "harmonies": harmonies,
        "type_pairing": {
            "key": brief.type_pairing,
            "name": next(p.name for p in TYPE_PAIRINGS if p.key == brief.type_pairing),
        },
        "type_pairings": type_pairings,
        "layout": {
            "key": brief.layout,
            "name": next(p.name for p in LAYOUT_PROFILES if p.key == brief.layout),
        },
        "layouts": layouts,
        "motion": {
            "key": brief.motion,
            "name": next(p.name for p in MOTION_PROFILES if p.key == brief.motion),
        },
        "motions": motions,
        "scene": {
            "key": brief.scene,
            "label": SCENE_TYPES[brief.scene]["label"],
            "description": SCENE_TYPES[brief.scene]["description"],
        },
        "scenes": scenes,
        "layout_pattern": {
            "key": brief.layout_pattern,
            "name": next(v.name for v in variants if v.key == brief.layout_pattern),
        },
        "layout_patterns": layout_patterns,
        "focal_point": brief.focal_point,
        "avoid": list(brief.avoid),
        "current": current,
        "current_display": current_display,
    }


def generate_from_state(state: Mapping[str, Mapping[str, str]]) -> str:
    """Run the brief -> generation -> gates pipeline and save the system
    into the user library.

    The brief is compiled from the wizard state (business + vibe + app
    type + the Direction tweaks), the seeded generator honours the brief's
    choices, and the quality gates re-roll on failure (new seed, up to 10
    attempts). Every new system ships `motion.js` + `scene.js`, and
    `meta.json` records the brief, the fingerprint, the gates result, and
    the scene type. Returns the system name; raises WizardError when the
    name is taken (a race between the Brand step and generation) or the
    answers are incomplete.

    The legacy `palette_for_vibe` path is only a fallback, when the seeded
    path is unavailable (every gated attempt failed): its palette is used
    as-is, its contrast violations are recorded in `meta.json`, and
    generation proceeds — the legacy palette's known muted-text contrast
    gap (issue #309) must never block creation.
    """
    if first_incomplete_step(state) is not None:
        raise WizardError("Finish every wizard step before generating.")
    purpose = state["purpose"]
    context = _state_business_context(state)
    app_type = state["app_type"]["app_type"]
    stack = state["stack"]["stack"]
    brand = state["brand"]
    name = brand["name"]
    product = purpose["product"]
    vibe = brand["vibe"]
    theme = brand["theme"]

    try:
        validate_library_name(name)
    except ValueError as exc:
        raise WizardError(str(exc)) from exc
    if name in list_library():
        raise WizardError(
            f'a design system named "{name}" already exists in the library — '
            "pick a different name (the studio only creates new systems)"
        )

    brief = _compile_brief_from_state(state, overrides=_direction_overrides(state))
    vibe_profile = VIBE_PROFILES.get(brand.get("vibe_profile", "")) or vibe_for_keywords(vibe)

    # Seeded generation (issue #301): the brief's choices are forced, the
    # gates re-roll on failure, and the seed + chosen skill keys are stored
    # in meta.json for reproducibility.
    seeded = None
    gates: dict[str, object]
    legacy_palette: dict[str, str] | None = None
    try:
        seeded, gates = generate_briefed_system(
            brief,
            existing_fingerprints=set(library_fingerprints().values()),
            system_name=name,
        )
    except Exception as exc:  # the seeded path is unavailable: legacy fallback
        legacy_palette = palette_for_vibe(
            " ".join(part for part in (vibe, context.industry) if part.strip())
        )
        violations = contrast_gate(legacy_palette, "both")
        gates = {
            "passed": not violations,
            "attempts": 0,
            "violations": violations,
            "fallback": "legacy-palette",
            "fallback_reason": str(exc),
        }
    if seeded is not None:
        palette = seeded.palette
        tokens = tokens_for_app_type(
            app_type,
            palette,
            theme,
            layout=seeded.layout,
            pairing=seeded.type_pairing,
            motion=seeded.motion,
        )
        # Rich tokens (issue #155): the full design-token set — ramps, dark pairs,
        # typography, shadows, motion — merged under "design_tokens" so legacy
        # consumers (exports, prompts) keep reading the flat keys untouched. The
        # seeded choices flow into the rich tokens (and the showcase) through the
        # vibe profile, so the detail page shows the same palette, pairing,
        # spacing, radii, and motion the flat tokens carry.
        tokens["design_tokens"] = rich_tokens_for_seeded(vibe_profile, seeded, theme)
        markdown = design_system_markdown(
            name,
            app_type,
            stack,
            palette,
            theme,
            business=context,
            components=list(seeded.components.components),
            choices=seeded.choices,
            pairing=seeded.type_pairing,
        )
        showcase_profile = seeded_vibe_profile(vibe_profile, seeded)
        design_seed = seeded.seed
        design_fingerprint = seeded.fingerprint
        design_choices: dict[str, str] = dict(seeded.choices)
    else:
        assert legacy_palette is not None  # set in the except branch above
        palette = legacy_palette
        tokens = tokens_for_app_type(app_type, palette, theme)
        tokens["design_tokens"] = rich_tokens(vibe_profile, theme)
        markdown = design_system_markdown(name, app_type, stack, palette, theme, business=context)
        showcase_profile = vibe_profile
        design_seed = brief.seed
        design_fingerprint = brief_fingerprint(brief)
        design_choices = {}
    seed = f"{name}:{product}"
    template = mark_template(seed)
    meta_extra: dict[str, object] = {
        "name": name,
        "app_type": app_type,
        "stack": stack,
        "source": "studio",
        "product_name": product,
        "theme": theme,
        "vibe": vibe,
        "vibe_profile": vibe_profile.key,
        "logo_template": template,
        "design_seed": design_seed,
        "design_fingerprint": design_fingerprint,
        "design_choices": design_choices,
        "design_brief": brief_meta(brief),
        "quality_gates": gates,
        "scene_type": brief.scene,
    }
    if not context.is_empty():
        meta_extra["business_context"] = context.to_meta()
        proposed = propose_elements(context)
        if proposed:
            meta_extra["business_elements"] = [element.key for element in proposed]
    save_library_system(name, markdown, tokens, meta_extra=meta_extra)

    # Showcase gallery (issue #155): a stunning standalone preview saved
    # alongside the system. It renders from the seeded profile so the hero
    # matches the tokens, not the base vibe.
    showcase_html = render_showcase(
        name,
        showcase_profile,
        cast("dict[str, object]", tokens["design_tokens"]),
        theme,
        business=context,
    )
    system_path = library_system_path(name)
    (system_path / "showcase.html").write_bytes(showcase_html.encode("utf-8"))

    # Motion & 3D (issue #302): every new system ships the real GSAP
    # timeline (`motion.js`) and the Three.js scene (`scene.js`) for the
    # brief's scene type, built from the same tokens the download uses.
    for filename, content in motion_files(
        name, tokens, scene_type=brief.scene, context=context
    ).items():
        (system_path / filename).write_text(content, encoding="utf-8")

    mark = generate_mark(seed, palette)
    assets: dict[str, bytes] = {
        "logo.svg": mark_to_svg(mark, 512).encode("utf-8"),
        "favicon.svg": mark_to_svg(mark, 64).encode("utf-8"),
        "favicon-16.png": mark_to_png(mark, 16),
        "favicon-32.png": mark_to_png(mark, 32),
        "favicon-180.png": mark_to_png(mark, 180),
        "favicon-192.png": mark_to_png(mark, 192),
        "favicon-512.png": mark_to_png(mark, 512),
    }
    for filename, data in assets.items():
        (system_path / filename).write_bytes(data)
    return name


def wizard_context(
    session_state: Mapping[str, Mapping[str, str]],
    step: str,
    entered: Mapping[str, object] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """The shared template context for every wizard step page.

    `entered` holds the raw POSTed values when a step re-renders after a
    validation error, so the user's input is not lost.
    """
    done = {name for name in STUDIO_STEPS if name != "generate" and session_state.get(name)}
    current = STUDIO_STEPS.index(step)
    steps = [
        {
            "key": name,
            "label": STEP_LABELS[name],
            "done": name in done,
            "current": name == step,
            "index": index + 1,
        }
        for index, name in enumerate(STUDIO_STEPS)
    ]
    context: dict[str, Any] = {
        "wizard_steps": steps,
        "wizard_step": step,
        "wizard_step_index": current,
        "wizard_last": step == STUDIO_STEPS[-1],
        "answers": {
            name: {
                **dict(session_state.get(name, {})),
                **(
                    {key: str(value) for key, value in entered.items()}
                    if entered is not None and name == step
                    else {}
                ),
            }
            for name in STUDIO_STEPS
        },
        "app_types": APP_TYPES,
        "themes": THEMES,
        "vibe_profiles": [
            {"key": p.key, "name": p.name, "tagline": p.tagline, "description": p.description}
            for p in VIBE_PROFILES.values()
        ],
        **extra,
    }
    if step == "stack":
        # Issue #200: a POST to /stack with no prior app_type fails validation
        # (WizardError) and then re-renders this context — the old direct index
        # raised KeyError here, turning the friendly error into a bare 500.
        # With no app type yet there is nothing to list: render the step with
        # empty options and let the error message (and the step page's
        # incomplete-step redirect) send the visitor back to App type.
        app_type = (session_state.get("app_type") or {}).get("app_type")
        if app_type is None:
            context["stack_options"] = []
            context["stack_recommended"] = ""
            context["stack_app_type"] = ""
            context["stack_app_type_label"] = ""
            context["stack_rec"] = None
            return context
        # The Jev pick, decided once when the App type step was submitted and
        # cached in the wizard state; without a cache the static default applies.
        cached = StackRecommendation.from_state(session_state.get(STACK_REC_KEY))
        rec = cached or default_recommendation(app_type, "not_asked")
        options, recommended = stack_options(app_type, cached.stack if cached else None)
        context["stack_options"] = options
        context["stack_recommended"] = recommended
        context["stack_app_type"] = app_type
        context["stack_app_type_label"] = APP_TYPES[app_type]
        context["stack_rec"] = {
            "source": rec.source,
            "rationale": rec.rationale,
        }
    if step in ("direction", "generate"):
        # The compiled brief, for the Direction step's preview and the
        # Generate step's summary. A step re-rendered after a validation
        # error overlays the just-POSTed values (axis sliders post 0..100,
        # stored tweaks are 0..1) so the visitor's input is not lost.
        preview_state: dict[str, dict[str, str]] = {
            name: dict(data) for name, data in session_state.items()
        }
        if entered is not None and step == "direction":
            overlay: dict[str, str] = {}
            for key, value in entered.items():
                if not isinstance(value, str):
                    continue
                if key.startswith("axis_"):
                    try:
                        overlay[key] = f"{max(0, min(100, int(float(value)))) / 100:.2f}"
                    except ValueError:
                        continue
                elif key != "csrf_token":
                    overlay[key] = value
            preview_state["direction"] = {
                **preview_state.get("direction", {}),
                **overlay,
            }
        try:
            preview_brief = _compile_brief_from_state(preview_state)
        except WizardError:
            preview_brief = None
        if preview_brief is not None:
            context["direction_brief"] = _brief_preview(preview_brief, preview_state)
    return context
