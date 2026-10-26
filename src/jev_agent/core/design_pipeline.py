"""The shared brief -> generation -> gates pipeline (issue #301).

This is the one path both call sites use — the dashboard's
`generate_from_state` and the CLI's `jev design new` — so a brief compiled
in one place generates the same way in the other:

1. `force_from_brief` turns the brief's decided choices into the `force`
   mapping `design_skills.generate_seeded_system` honours, so the generated
   system keeps the brief's harmony, type pairing, layout, motion profile,
   layout pattern, and anchor hue (the known gap: the seeded sampler used
   to pick all of those freely, and a fintech brief that decided
   `monochromatic` + `space-grotesk-plex` could generate `triadic` +
   `libre-opensans`).
2. `generate_briefed_system` generates from the brief's seed, re-rolling
   (seed+1, seed+2, …) on a fingerprint collision with the library or on
   any quality-gate failure, up to `max_attempts` attempts, via the gates
   module's `ensure_passing_gates`.
3. `gate_violations_for` runs the three gates over one candidate: contrast
   in both themes, the generic-detector, and reduced-motion over the real
   motion artifacts (`reduced_motion_css()` + the generated `motion.js` /
   `scene.js`).

The pipeline never invents a fallback palette: when every attempt fails
it raises ValueError, and the caller decides — the dashboard and the CLI
fall back to the legacy `palette_for_vibe` path and record the violations
in `meta.json` instead of failing generation (the legacy palette's known
muted-text contrast gap, issue #309, must never block creation).

Pure functions, stdlib-only, like `design_kit.py`.
"""

from __future__ import annotations

from jev_agent.core.design_brief import DesignBrief, brief_fingerprint
from jev_agent.core.design_gates import (
    MAX_GATE_ATTEMPTS,
    contrast_gate,
    ensure_passing_gates,
    generic_detector,
    is_generic,
    reduced_motion_gate,
)
from jev_agent.core.design_motion3d import (
    build_motion_js,
    build_motion_spec,
    build_scene_js,
    reduced_motion_css,
)
from jev_agent.core.design_skills import SeededSystem, generate_seeded_system

MAX_PIPELINE_ATTEMPTS = MAX_GATE_ATTEMPTS


def force_from_brief(brief: DesignBrief) -> dict[str, object]:
    """The brief's decided choices as the skills module's `force` mapping.

    Every named choice the brief made is pinned: harmony, type pairing,
    layout, motion profile, layout pattern, and the anchor hue (so the
    generated primary sits on the brief's OKLCH ramp, not on the keyword
    steer's hue). Axes stay out — they shaped the brief, they are not
    generation inputs.
    """
    return {
        "harmony": brief.harmony,
        "type_pairing": brief.type_pairing,
        "layout": brief.layout,
        "motion_profile": brief.motion,
        "layout_pattern": brief.layout_pattern,
        "anchor_hue": brief.anchor_hue,
    }


def brief_meta(brief: DesignBrief) -> dict[str, object]:
    """The brief as JSON-safe `meta.json` content (the decision record)."""
    return {
        "axes": dict(brief.axes),
        "anchor_hue": brief.anchor_hue,
        "anchor_chroma": brief.anchor_chroma,
        "primary_lightness": brief.primary_lightness,
        "primary_hex": brief.primary_hex,
        "palette_ramp": list(brief.palette_ramp),
        "harmony": brief.harmony,
        "type_pairing": brief.type_pairing,
        "layout": brief.layout,
        "motion": brief.motion,
        "layout_pattern": brief.layout_pattern,
        "focal_point": brief.focal_point,
        "scene": brief.scene,
        "avoid": list(brief.avoid),
        "industry": brief.industry,
        "vibe": brief.vibe,
        "app_type": brief.app_type,
        "seed": brief.seed,
        "keywords": brief.keywords,
        "overrides": dict(brief.overrides),
        "brief_fingerprint": brief_fingerprint(brief),
    }


def gate_violations_for(
    seeded: SeededSystem,
    brief: DesignBrief,
    *,
    system_name: str = "",
) -> list[str]:
    """The quality-gate violations for one generated candidate (empty passes).

    Contrast in both themes (the dark palette is the one the design kit
    derives from the light palette, so the gate checks what ships); the
    generic-detector over what was actually generated (primary, both font
    stacks, the layout's radii, the component pattern); reduced-motion
    over the real motion artifacts — the `prefers-reduced-motion` CSS block
    plus the generated `motion.js` / `scene.js`, which must branch on the
    media query.
    """
    violations = contrast_gate(seeded.palette, "both")
    score, flags = generic_detector(
        {
            "primary": seeded.palette.get("primary", ""),
            "display_stack": seeded.type_pairing.display_stack,
            "body_stack": seeded.type_pairing.body_stack,
            "radii": seeded.layout.radii,
            "layout_pattern": seeded.components.key,
        }
    )
    if is_generic(score):
        violations.extend(f"generic design: {flag}" for flag in flags)
    spec = build_motion_spec(seeded.motion.tokens)
    motion_js = build_motion_js(spec, system_name=system_name)
    scene_js = build_scene_js(brief.scene, palette=seeded.palette, system_name=system_name)
    violations.extend(
        reduced_motion_gate(reduced_motion_css(), js=motion_js + scene_js, motion=spec)
    )
    return violations


def generate_briefed_system(
    brief: DesignBrief,
    *,
    existing_fingerprints: set[str] | frozenset[str] = frozenset(),
    max_attempts: int = MAX_PIPELINE_ATTEMPTS,
    system_name: str = "",
) -> tuple[SeededSystem, dict[str, object]]:
    """Generate the system a brief decided, passing every quality gate.

    One seed per attempt (brief.seed, brief.seed+1, …): a fingerprint
    collision with the library or any gate violation re-rolls. The brief's
    choices stay forced on every attempt — only the sampled values move.
    Returns `(seeded, gates_result)` where the gates result records
    `passed`, `attempts` used, and the (empty) `violations` list. Raises
    ValueError when every attempt fails; the caller owns the fallback.
    """
    force = force_from_brief(brief)
    attempt = {"n": 0}

    def _attempt() -> tuple[dict[str, object], list[str]]:
        seed = brief.seed + attempt["n"]
        attempt["n"] += 1
        seeded = generate_seeded_system(
            seed,
            keywords=brief.keywords,
            app_type=brief.app_type,
            force=force,
        )
        violations = gate_violations_for(seeded, brief, system_name=system_name)
        if seeded.fingerprint in existing_fingerprints:
            violations = [
                *violations,
                "fingerprint collides with an existing design system",
            ]
        return {"seeded": seeded}, violations

    result, used = ensure_passing_gates(_attempt, max_attempts=max_attempts)
    seeded = result["seeded"]
    assert isinstance(seeded, SeededSystem)
    return seeded, {"passed": True, "attempts": used, "violations": []}
