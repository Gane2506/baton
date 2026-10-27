"""Quality gates for generated design systems (issue #303).

Core only — no dashboard, no `generate_from_state` edits; the integration
step owns the wiring. Pure functions, stdlib-only, like `design_kit.py`.

The gates encode the research report
(`research_notes/design-best-practices-20261026-0604/report.md`):

- `contrast_gate` — WCAG 2.2 AA contrast (§2.4, §9): body and muted text
  >= 4.5:1 on background, surface, and the raised surface badges render on
  (`surface_raised`, derived the `design_codegen` way), button text >= 4.5:1
  on both primary states, in BOTH light and dark themes. Issue #358 extends it to the
  pairs the #309 Mac scan found the gate passing: the primary hue as text
  (via the derived `primary_ink` role, never the raw primary), semantic
  text on its tinted background (`{role}_text` on `{role}_subtle`), and
  swatch hex labels (the `label_on_color` pick must read >= 4.5:1 on the
  swatch). Issue #359/#360 raise the button-text bar from 3:1 to 4.5:1:
  button labels are text, so the WCAG AA normal-text floor applies, and
  the builders walk the primary lightness until a shared pick clears it —
  the gate no longer passes readable text at 4.06-4.44:1. The
  light-mode-only check in
  `design_skills.check_palette_contrast` (issue #299, parallel work) is the
  same shape with a narrower scope; this gate is the one that covers both
  themes, and it reuses `design_kit.contrast_ratio` rather than
  reimplementing the ratio.
- `reduced_motion_gate` — `prefers-reduced-motion` (§6.5): the generated
  CSS must carry a reduced-motion block that neutralises animation and
  transition durations, and JS motion timelines must branch on the media
  query (or the motion spec must carry a reduced-motion variant). State
  changes still happen — instantly, without movement.
- `generic_detector` — the AI-default markers (§10.3) and the
  post-generation checklist (§10.4 item 10): Inter/system-ui-only type,
  a purple/indigo primary, 0.5rem radii everywhere, a 3-column icon grid,
  gradient text. Score = number of flags; `is_generic` at >= 2.
- `ensure_passing_gates` — re-roll helper: call the generator again until
  the violations are empty, bounded by `max_attempts`.

Every gate returns a list of named violations; empty means pass. No gate
returns a bare boolean.

Input shapes
------------
`contrast_gate` takes a flat palette dict with the `design_kit`
`PALETTE_KEYS` (`primary`, `primary_hover`, `primary_text`,
`background`, `surface`, `text`, `text_muted`, …). The dark palette is
either passed explicitly (`dark_palette`) or derived the way the design
kit derives it. Roles missing from a palette are skipped, matching
`design_skills.check_palette_contrast`.

`generic_detector` takes a plain dict::

    {
        "primary": "#6366F1",                    # hex
        "display_stack": '"Inter", system-ui, sans-serif',
        "body_stack": '"Inter", system-ui, sans-serif',
        "radii": {"sm": 8, "md": 8, "lg": 8},    # px, or "8px" / "0.5rem"
        "layout_pattern": "3-column icon grid",
        "gradient_text": True,
        "components": ["hero", "features", ...], # present for context; unused
    }

A missing font stack counts as the system default (Inter/system-ui), so
it flags. The `radii` check ignores the `none` (0) and `full` (pill)
keys — a system is generic when every *stepped* radius is 8px.
"""

from __future__ import annotations

import colorsys
import re
from collections.abc import Callable

from jev_agent.core.design_kit import (
    SEMANTIC_ROLES,
    THEMES,
    _dark_variant,
    contrast_ratio,
    derived_surface_raised,
    label_on_color,
)

# WCAG 2.2 AA minimums (report §9.2): normal text 4.5:1, UI components 3:1.
# Button labels are text, not bare UI components, so the button-text bar is
# the 4.5:1 normal-text floor too (issues #359/#360).
BODY_TEXT_MIN = 4.5
BUTTON_TEXT_MIN = 4.5

# The generic-detector threshold (report §10.4 item 10): two or more
# AI-default markers and the system is still at the centroid.
GENERIC_THRESHOLD = 2

# `ensure_passing_gates` gives up after this many attempts, matching the
# re-roll bound in `design_skills` (MAX_REROLLS).
MAX_GATE_ATTEMPTS = 10

# The purple/indigo band of the "Tailwind indigo-500" default CTA
# (report §10.3), in HSL degrees, with a saturation floor so a muted
# lavender or a grey-violet does not flag.
_PURPLE_BAND = (230.0, 265.0)
_PURPLE_SATURATION_MIN = 0.5

# Font families that carry no character: system fallbacks and the
# platform sans faces. Anything left after removing these is a named,
# chosen face; "Inter" alone is the AI default (report §10.3).
_GENERIC_FALLBACKS: frozenset[str] = frozenset(
    {
        "system-ui",
        "sans-serif",
        "serif",
        "monospace",
        "cursive",
        "fantasy",
        "sans",
        "-apple-system",
        "blinkmacsystemfont",
        "segoe ui",
        "ui-sans-serif",
        "ui-serif",
        "ui-monospace",
        "ui-rounded",
        "arial",
        "helvetica",
        "helvetica neue",
    }
)

# Keys of a motion spec that carry the reduced-motion variant (report
# §6.5). The JS branch on `prefers-reduced-motion` and a variant in the
# spec are interchangeable reduced-motion paths for JS timelines.
_REDUCED_VARIANT_KEYS: frozenset[str] = frozenset(
    {"reduced_motion", "reduced-motion", "prefers_reduced_motion", "reduced"}
)


# ---------------------------------------------------------------------------
# Small color helpers (local copies: design_kit keeps its own privates)
# ---------------------------------------------------------------------------


def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    """`#RRGGBB` to 0..1 floats (raises ValueError on anything else)."""
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"expected #RRGGBB, got {value!r}")
    return (
        int(text[0:2], 16) / 255.0,
        int(text[2:4], 16) / 255.0,
        int(text[4:6], 16) / 255.0,
    )


def _hue_saturation(hex_color: str) -> tuple[float, float] | None:
    """The (hue 0..360, saturation 0..1) of a `#RRGGBB` color, or None when
    the value is not a parseable hex color."""
    try:
        red, green, blue = _hex_to_rgb(hex_color)
    except ValueError:
        return None
    hue, _lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    return hue * 360.0, saturation


# ---------------------------------------------------------------------------
# 1. Contrast gate (WCAG 2.2 AA, both themes)
# ---------------------------------------------------------------------------


def _check_palette(palette: dict[str, str], label: str) -> list[str]:
    """The per-theme checks. Each violation names the role pair and the
    measured ratio.

    - body/muted text >= 4.5:1 on background and surface;
    - muted text >= 4.5:1 on the raised surface too (`surface_raised`,
      derived the `design_codegen` way — issue #359: eco muted reads
      4.36:1 on the raised surface badges render on, while clearing 4.5:1
      on background and surface);
    - button text >= 4.5:1 on both primary states (issues #359/#360:
      button labels are text, so the WCAG AA normal-text floor applies,
      not the 3:1 UI-component minimum);
    - `primary_ink` (the primary hue adjusted to read as text) >= 4.5:1 on
      background and surface — the raw primary is never text (issue #358:
      health primary `#DB26AB` reads 4.31:1 on white, 3.88:1 on its surface,
      so the gate checks the ink role, not the primary);
    - semantic text >= 4.5:1 on its tinted background (`success_text` on
      `success_subtle`, and so on - the raw semantic colors read 2.86-4.24:1
      on their tints, so the gate checks the text roles, not the bases);
    - swatch labels: the `label_on_color` pick must read >= 4.5:1 on the
      swatch color, for every palette color that renders as a swatch
      (primary, primary_hover, and the semantics). The rule itself
      guarantees this for any color, so the check pins the rule: a palette
      whose primary sits at the white/near-black crossover (`#DB26AB`,
      best pick 4.34:1) fails unless the pure-black fallback is in place.
    """
    violations: list[str] = []
    background = palette.get("background")
    surface = palette.get("surface")
    for role in ("text", "text_muted", "primary_ink"):
        color = palette.get(role)
        if color is None:
            continue
        for base_name, base in (("background", background), ("surface", surface)):
            if base is None:
                continue
            ratio = contrast_ratio(color, base)
            if ratio < BODY_TEXT_MIN:
                violations.append(
                    f"{label}: {role} on {base_name} is {ratio:.2f}:1 (minimum {BODY_TEXT_MIN}:1)"
                )
    raised = derived_surface_raised(palette)
    muted = palette.get("text_muted")
    if muted is not None and raised is not None:
        ratio = contrast_ratio(muted, raised)
        if ratio < BODY_TEXT_MIN:
            violations.append(
                f"{label}: text_muted on surface_raised is {ratio:.2f}:1 "
                f"(minimum {BODY_TEXT_MIN}:1)"
            )
    primary_text = palette.get("primary_text")
    if primary_text is not None:
        for state in ("primary", "primary_hover"):
            color = palette.get(state)
            if color is None:
                continue
            ratio = contrast_ratio(primary_text, color)
            if ratio < BUTTON_TEXT_MIN:
                violations.append(
                    f"{label}: primary_text on {state} is {ratio:.2f}:1 "
                    f"(minimum {BUTTON_TEXT_MIN}:1)"
                )
    for role in SEMANTIC_ROLES:
        text_color = palette.get(f"{role}_text")
        subtle = palette.get(f"{role}_subtle")
        if text_color is None or subtle is None:
            continue
        ratio = contrast_ratio(text_color, subtle)
        if ratio < BODY_TEXT_MIN:
            violations.append(
                f"{label}: {role}_text on {role}_subtle is {ratio:.2f}:1 "
                f"(minimum {BODY_TEXT_MIN}:1)"
            )
    for role in ("primary", "primary_hover", *SEMANTIC_ROLES):
        color = palette.get(role)
        if color is None:
            continue
        label_color = label_on_color(color)
        ratio = contrast_ratio(label_color, color)
        if ratio < BODY_TEXT_MIN:
            violations.append(
                f"{label}: swatch label {label_color} on {role} {color} is "
                f"{ratio:.2f}:1 (minimum {BODY_TEXT_MIN}:1)"
            )
    return violations


def contrast_gate(
    palette: dict[str, str],
    theme: str = "both",
    dark_palette: dict[str, str] | None = None,
) -> list[str]:
    """WCAG AA violations in a palette (empty means it passes).

    `palette` is the light-mode base (the `design_kit` palette shape).
    `theme` is "light", "dark" or "both". For the dark theme the palette
    is `dark_palette` when given, otherwise the one the design kit derives
    from the light palette — imported, not copied, so the gate can never
    drift from what the generator actually ships. A palette with no
    primary has no derivable dark variant, so the dark check is skipped.

    Beyond body/muted/button text, the gate covers the issue #358 pairs:
    `primary_ink` on background/surface, each `{role}_text` on its
    `{role}_subtle` tint, and the swatch-label pick on every swatch color.
    Roles missing from a palette are skipped, matching
    `design_skills.check_palette_contrast`.
    """
    if theme not in THEMES:
        raise ValueError(f"unknown theme: {theme!r} (expected one of {list(THEMES)})")
    violations: list[str] = []
    if theme in ("light", "both"):
        violations.extend(_check_palette(palette, "light"))
    if theme in ("dark", "both"):
        if dark_palette is not None:
            violations.extend(_check_palette(dark_palette, "dark"))
        elif "primary" in palette:
            violations.extend(_check_palette(_dark_variant(palette), "dark"))
    return violations


# ---------------------------------------------------------------------------
# 2. Reduced-motion gate (prefers-reduced-motion)
# ---------------------------------------------------------------------------

_REDUCED_MEDIA = re.compile(
    r"@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)", re.IGNORECASE
)
# A duration that is effectively instant: 0, 0ms, 0s, 0.01ms, 0.001s.
# The negative lookahead keeps "0.5s" (a real duration) from matching
# on its leading "0".
_ZERO_DURATION = r"0(?:\.0+1?)?\s*(?:ms|s)?(?![\d.])"
_ANIMATION_OFF = re.compile(
    rf"animation(?:-duration)?\s*:\s*(?:none\b|{_ZERO_DURATION})", re.IGNORECASE
)
_TRANSITION_OFF = re.compile(
    rf"transition(?:-duration)?\s*:\s*(?:none\b|{_ZERO_DURATION})", re.IGNORECASE
)
_KEYFRAMES = re.compile(r"@keyframes\s+([A-Za-z_][\w-]*)")
_UNIVERSAL_SELECTOR = re.compile(r"(?:^|[\s,{])\*")
_JS_MOTION = re.compile(r"requestAnimationFrame|\.animate\s*\(")


def _strip_css_comments(css: str) -> str:
    """CSS with `/* ... */` comments removed, so a commented-out block
    cannot pass the gate."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _find_media_block(css: str) -> tuple[int, int, str] | None:
    """The `(start, end, content)` of the first `prefers-reduced-motion`
    block, or None. Braces are counted so nested rules do not cut the
    block short."""
    match = _REDUCED_MEDIA.search(css)
    if match is None:
        return None
    start = css.find("{", match.end())
    if start == -1:
        return None
    depth = 0
    for index in range(start, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return start, index + 1, css[start + 1 : index]
    return None


def _has_reduced_variant(motion: dict[str, object] | None) -> bool:
    """Whether a motion spec carries a reduced-motion variant, top-level
    or under a `variants` key."""
    if not motion:
        return False
    for key in _REDUCED_VARIANT_KEYS:
        if motion.get(key) is not None:
            return True
    variants = motion.get("variants")
    if isinstance(variants, dict):
        return any(variants.get(key) is not None for key in _REDUCED_VARIANT_KEYS)
    return False


def reduced_motion_gate(
    css: str,
    js: str = "",
    motion: dict[str, object] | None = None,
) -> list[str]:
    """Reduced-motion violations in generated CSS/JS (empty means it
    passes).

    The CSS must contain a `@media (prefers-reduced-motion: reduce)`
    block that neutralises animation/transition durations (the report
    §6.5 pattern: `animation-duration: 0.01ms` / `transition-duration:
    0.01ms`, or `animation: none` / `transition: none`). Every
    `@keyframes` animation must have a reduced-motion path: a universal
    selector in the block, the animation named in the block, or
    `animation: none` in the block covers it; a duration neutraliser on
    a single selector does not cover other animations.

    JS motion timelines (`requestAnimationFrame`, `.animate(`) must
    branch on the media query, or the motion spec must carry a
    reduced-motion variant. JS with no motion timelines needs neither.
    State changes still happen under reduced motion — instantly, without
    movement — so pausing an animation (`animation-play-state: paused`)
    is not a neutraliser.
    """
    violations: list[str] = []
    clean = _strip_css_comments(css)
    found = _find_media_block(clean)
    if found is None:
        violations.append("CSS has no @media (prefers-reduced-motion: reduce) block")
    else:
        start, end, block = found
        outside = clean[:start] + clean[end:]
        animation_off = _ANIMATION_OFF.search(block) is not None
        transition_off = _TRANSITION_OFF.search(block) is not None
        if not animation_off and not transition_off:
            violations.append(
                "the prefers-reduced-motion block does not neutralise "
                "animation or transition durations"
            )
        else:
            if re.search(r"@keyframes|animation\s*:", outside) and not animation_off:
                violations.append(
                    "CSS animations have no reduced-motion path "
                    "(the block neutralises transitions only)"
                )
            if re.search(r"transition\s*:", outside) and not transition_off:
                violations.append(
                    "CSS transitions have no reduced-motion path "
                    "(the block neutralises animation only)"
                )
        if animation_off:
            universal = _UNIVERSAL_SELECTOR.search(block) is not None
            animation_none = re.search(r"animation\s*:\s*none\b", block, re.IGNORECASE)
            for name in _KEYFRAMES.findall(outside):
                covered = (
                    universal
                    or animation_none is not None
                    or re.search(rf"\b{re.escape(name)}\b", block) is not None
                )
                if not covered:
                    violations.append(f"animation '{name}' has no reduced-motion path")
    if (
        _JS_MOTION.search(js)
        and "prefers-reduced-motion" not in js
        and not _has_reduced_variant(motion)
    ):
        violations.append(
            "JS motion does not branch on prefers-reduced-motion and "
            "the motion spec carries no reduced-motion variant"
        )
    return violations


# ---------------------------------------------------------------------------
# 3. Generic-detector (the AI-default markers)
# ---------------------------------------------------------------------------


def _named_families(stack: str) -> set[str]:
    """The named (non-fallback) families in a font stack, lowercased."""
    return {
        name
        for name in (part.strip().strip("\"'").lower() for part in stack.split(","))
        if name and name not in _GENERIC_FALLBACKS
    }


def _is_default_type(system: dict[str, object]) -> bool:
    """Both faces are Inter/system-ui only, with no characterful display
    face. A missing stack counts as the system default."""
    display = system.get("display_stack")
    body = system.get("body_stack")
    display_named = _named_families(display) if isinstance(display, str) else set()
    body_named = _named_families(body) if isinstance(body, str) else set()
    return display_named <= {"inter"} and body_named <= {"inter"}


def _radius_px(value: object) -> float | None:
    """A radius value as pixels, or None when it is not a number. Accepts
    plain numbers (px), `"8px"`, and `"0.5rem"` (16px base)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().lower()
        factor = 1.0
        if text.endswith("rem"):
            factor = 16.0
            text = text[:-3]
        elif text.endswith("px"):
            text = text[:-2]
        try:
            return float(text) * factor
        except ValueError:
            return None
    return None


def _all_radii_eight(radii: dict[str, object]) -> bool:
    """Every *stepped* radius is 8px (0.5rem everywhere). The `none` (0)
    and `full` (pill) keys are not steps, so they are ignored; an empty
    stepped set does not flag."""
    stepped = [_radius_px(value) for key, value in radii.items() if key not in ("none", "full")]
    return bool(stepped) and all(
        value is not None and abs(value - 8.0) < 0.001 for value in stepped
    )


def _is_three_column_icon_grid(pattern: str) -> bool:
    """The layout pattern is a 3-column icon grid ("3-column", "3 col",
    "three-column" all count; "icon" and "grid" must both appear)."""
    text = re.sub(r"[\s\-_]+", " ", pattern.strip().lower())
    return (
        re.search(r"\b(3|three)\s?col(umns?)?\b", text) is not None
        and "icon" in text
        and "grid" in text
    )


def generic_detector(system: dict[str, object]) -> tuple[int, list[str]]:
    """The AI-default markers in a generated system (report §10.3).

    Returns `(score, flags)`: one flag per marker, score = number of
    flags. The markers are: Inter/system-ui-only type; primary hue in
    the purple/indigo band 230-265 degrees with saturation > 0.5; every stepped
    radius 8px; a 3-column icon grid layout; gradient text. See the
    module docstring for the input shape.
    """
    flags: list[str] = []
    if _is_default_type(system):
        flags.append(
            "display and body faces are Inter/system-ui only — no characterful display face"
        )
    primary = system.get("primary")
    hue_sat = _hue_saturation(primary) if isinstance(primary, str) else None
    if hue_sat is not None:
        hue, saturation = hue_sat
        if _PURPLE_BAND[0] <= hue <= _PURPLE_BAND[1] and saturation > _PURPLE_SATURATION_MIN:
            flags.append(
                f"primary {primary} sits in the purple/indigo band "
                f"(hue {hue:.0f}°, saturation {saturation:.2f})"
            )
    radii = system.get("radii")
    if isinstance(radii, dict) and _all_radii_eight(radii):
        flags.append("every radius is 8px (0.5rem everywhere)")
    layout = system.get("layout_pattern")
    if isinstance(layout, str) and _is_three_column_icon_grid(layout):
        flags.append("layout is a 3-column icon grid")
    if system.get("gradient_text"):
        flags.append("gradient text is present")
    return len(flags), flags


def is_generic(score: int) -> bool:
    """Whether a generic-detector score means the system is still at the
    AI-default centroid: True at >= 2 flags."""
    return score >= GENERIC_THRESHOLD


# ---------------------------------------------------------------------------
# 4. Re-roll helper
# ---------------------------------------------------------------------------


def ensure_passing_gates(
    generate: Callable[[], tuple[dict[str, object], list[str]]],
    max_attempts: int = MAX_GATE_ATTEMPTS,
) -> tuple[dict[str, object], int]:
    """Re-roll a zero-arg generator until its violations are empty.

    `generate` returns `(system_dict, violations)`; an empty violations
    list means the system passes. Returns `(system, attempts_used)`.
    Raises ValueError when every attempt fails. Always terminates:
    at most `max_attempts` calls, never an unbounded loop.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    for attempt in range(1, max_attempts + 1):
        system, violations = generate()
        if not violations:
            return system, attempt
    raise ValueError(
        f"could not generate a system passing all quality gates in {max_attempts} attempts"
    )
