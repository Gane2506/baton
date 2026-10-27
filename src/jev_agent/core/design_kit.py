"""Design-kit content for `jev design new`: app types, stacks, token defaults,
vibe-to-palette mapping, and the starter design-system markdown.

Pure functions, stdlib-only. Persistence (the design library) lives in
`jev_agent.core.design_library`, built by a parallel workstream — this module
only produces the content the wizard writes into it.
"""

from __future__ import annotations

import colorsys
import hashlib
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jev_agent.core.design_business import BusinessContext
    from jev_agent.core.design_skills import LayoutProfile, MotionProfile, TypePairing

APP_TYPES: dict[str, str] = {
    "mobile": "Mobile app — touch-first UI, platform conventions, safe areas",
    "website": "Marketing/content site — type-forward, hero sections",
    "pwa": "Installable web app — app-like web UI, offline states",
    "dashboard": "Data-dense admin/analytics — tables, charts, dark-mode-first",
}

STACKS: dict[str, list[str]] = {
    "mobile": ["React Native", "Flutter", "SwiftUI", "Kotlin/Jetpack Compose"],
    "website": ["Web + Tailwind", "Web + plain CSS"],
    "pwa": ["Web + Tailwind", "Web + plain CSS"],
    "dashboard": ["Web + Tailwind", "Web + plain CSS"],
}
"""Stack options per app type. The first entry is the recommended default. The
stack only sets token defaults and export targets; the wizard never scaffolds code."""

PALETTE_KEYS: tuple[str, ...] = (
    "primary",
    "primary_hover",
    "primary_text",
    "primary_ink",
    "background",
    "surface",
    "text",
    "text_muted",
    "border",
    "success",
    "success_subtle",
    "success_text",
    "warning",
    "warning_subtle",
    "warning_text",
    "danger",
    "danger_subtle",
    "danger_text",
    "info",
    "info_subtle",
    "info_text",
)

# Semantic colors (issue #358): the base hues are fixed in both themes and
# duplicated across `design_system`, `design_codegen`, `design_showcase`,
# and `design_skills` — this module is the canonical source, the others
# import these tables. The `*_text` variants are the same hues adjusted to
# read on their tinted backgrounds (see `derive_text_roles`); the subtles
# are the tinted backgrounds badges/alerts render on.
SEMANTIC_ROLES: tuple[str, ...] = ("success", "warning", "danger", "info")

SEMANTIC_LIGHT: dict[str, str] = {
    "success": "#16A34A",
    "warning": "#D97706",
    "danger": "#DC2626",
    "info": "#2563EB",
}

SEMANTIC_DARK: dict[str, str] = {
    "success": "#22C55E",
    "warning": "#F59E0B",
    "danger": "#F87171",
    "info": "#60A5FA",
}

SEMANTIC_SUBTLE_LIGHT: dict[str, str] = {
    "success": "#DCFCE7",
    "warning": "#FEF3C7",
    "danger": "#FEE2E2",
    "info": "#DBEAFE",
}

SEMANTIC_SUBTLE_DARK: dict[str, str] = {
    "success": "#14532D",
    "warning": "#78350F",
    "danger": "#7F1D1D",
    "info": "#1E3A8A",
}

THEMES: tuple[str, ...] = ("light", "dark", "both")


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


def _rgb_to_hex(r: float, g: float, b: float) -> str:
    """0..1 floats to `#RRGGBB`, clamped and rounded."""

    def clamp(v: float) -> int:
        return max(0, min(255, round(v * 255)))

    return f"#{clamp(r):02X}{clamp(g):02X}{clamp(b):02X}"


def _relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance of a `#RRGGBB` color."""
    r, g, b = _hex_to_rgb(hex_color)

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast_ratio(first: str, second: str) -> float:
    """WCAG contrast ratio (1..21) between two `#RRGGBB` colors."""
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def _primary_text(primary: str, primary_hover: str) -> str:
    """The text color for content sitting on the primary button color.

    Picks whichever of white and near-black reads better on the *worse* of the
    primary and its hover state, so the button label never lands on the state
    the text color was not chosen for. The pick alone guarantees nothing —
    the builders walk the primary lightness (`_walk_button_lightness`) until
    the pick clears 4.5:1 on both states, and the gate enforces it.
    """
    white = min(contrast_ratio("#FFFFFF", primary), contrast_ratio("#FFFFFF", primary_hover))
    dark = min(contrast_ratio("#101418", primary), contrast_ratio("#101418", primary_hover))
    return "#FFFFFF" if white >= dark else "#101418"


def _mix_hex(first: str, second: str, first_weight: float) -> str:
    """Blend two `#RRGGBB` colors (same rule as `design_codegen._mix`)."""
    r1, g1, b1 = _hex_to_rgb(first)
    r2, g2, b2 = _hex_to_rgb(second)
    return _rgb_to_hex(
        r1 * first_weight + r2 * (1.0 - first_weight),
        g1 * first_weight + g2 * (1.0 - first_weight),
        b1 * first_weight + b2 * (1.0 - first_weight),
    )


def derived_surface_raised(palette: dict[str, str]) -> str | None:
    """The raised surface badges render muted text on: 94% surface + 6%
    text — the `design_codegen._derived_colors` derivation, canonical here
    so the gate, the kit's muted-text rule, and codegen all derive the same
    color (issue #359: the showcase's `surface_raised` is its neutral-100,
    a touch lighter than this mix, so a muted text clearing 4.5:1 on the
    mix clears it on the showcase value too). None when surface or text
    is missing from the palette."""
    surface = palette.get("surface")
    text = palette.get("text")
    if surface is None or text is None:
        return None
    return _mix_hex(surface, text, 0.94)


def _adaptive_hover(primary: str, saturation: float, nominal: float, *, target: float = 4.6) -> str:
    """A hover lightness for a dark-theme primary: start at `nominal` (0.62
    in the kit's dark variant, primary + 0.08 in the showcase) and step
    0.01 at a time — down while a white button text fails `target`, up
    while a dark one fails — until the shared pick clears `target` on the
    hover. Issue #360 at the 4.5 bar: the old unconditional 0.62 lighten
    needs dark text at hue 240 (3.38:1), so the step goes up there, not
    just down; and a dark primary needs the step down (playful seed 6:
    white on the 0.62 hover is 2.85:1). The pick is computed once at the
    nominal lightness and never flips: stepping down only helps white,
    stepping up only helps dark.

    The hue is read off the primary hex, never passed in: the gate
    re-derives the dark variant from the stored hex (`_dark_variant`),
    and a hue carried at full precision disagrees with the hex-rounded
    one by enough to flip a near-tie (hue 289: the walk cleared 4.6 with
    the exact hue while the gate measured 3.47 with the hex hue).
    """
    hue = _hue_of(primary)
    lightness = nominal
    hover = _hsl(hue, saturation, round(lightness, 2))
    text = _primary_text(primary, hover)
    if text == "#FFFFFF":
        while lightness > 0.20 and contrast_ratio(text, hover) < target:
            lightness = round(lightness - 0.01, 2)
            hover = _hsl(hue, saturation, lightness)
    else:
        while lightness < 0.90 and contrast_ratio(text, hover) < target:
            lightness = round(lightness + 0.01, 2)
            hover = _hsl(hue, saturation, lightness)
    return hover


def _walk_button_lightness(
    hue: float,
    saturation: float,
    lightness: float,
    *,
    hover_for: Callable[[str, float, float], list[str]],
    target: float = 4.6,
    lo: float = 0.28,
    hi: float = 0.75,
) -> float:
    """The primary lightness closest to `lightness` whose shared button text
    clears `target` on every hover `hover_for(primary, lightness,
    saturation)` derives. Searches outward in 0.01 steps (lighter first on
    ties — the nearer extreme for a mid primary, matching the old
    `_ensure_contrast` direction). When no lightness in [lo, hi] clears,
    returns the best candidate instead of raising, so a builder never
    crashes on an unusual hue. The hue sweeps prove every hue clears within
    0.11 of 0.50, so the fallback is unreachable for the builders' ranges.
    """

    def worst_ratio(candidate: float) -> float:
        primary = _hsl(hue, saturation, round(candidate, 2))
        worst = 21.0
        for hover in hover_for(primary, round(candidate, 2), saturation):
            text = _primary_text(primary, hover)
            worst = min(worst, contrast_ratio(text, primary), contrast_ratio(text, hover))
        return worst

    start = round(min(max(lightness, lo), hi), 2)
    best_lightness, best_ratio = start, worst_ratio(start)
    if best_ratio >= target:
        return start
    steps = round((hi - lo) / 0.01)
    for i in range(1, steps + 1):
        for candidate in (round(start + i * 0.01, 2), round(start - i * 0.01, 2)):
            if candidate < lo or candidate > hi:
                continue
            ratio = worst_ratio(candidate)
            if ratio > best_ratio:
                best_lightness, best_ratio = candidate, ratio
            if ratio >= target:
                return candidate
    return best_lightness


def _hsl(hue: float, saturation: float, lightness: float) -> str:
    """One palette color from HSL degrees / 0..1 / 0..1."""
    r, g, b = colorsys.hls_to_rgb((hue % 360.0) / 360.0, lightness, saturation)
    return _rgb_to_hex(r, g, b)


def _hue_of(hex_color: str) -> float:
    """The hue (0..360) of a `#RRGGBB` color."""
    r, g, b = _hex_to_rgb(hex_color)
    hue, _lightness, _saturation = colorsys.rgb_to_hls(r, g, b)
    return hue * 360.0


def _muted_text(hue: float, background: str, surface: str, raised: str | None = None) -> str:
    """Muted text dark enough to clear 4.5:1 on background, surface, and the
    raised surface badges render on (issue #359: eco muted reads 4.36:1 on
    `surface_raised`, which the old two-base rule never checked).

    Green/cyan hues carry most of their luminance in the green channel, so
    a fixed lightness of 0.44 is too light for them (health 4.32:1, eco
    4.36:1, travel-on-surface 4.18:1 — issue #309). Darken in 0.01 steps
    until the ratio clears 4.6:1 on every base; the 0.1 margin keeps hex
    rounding from dipping below the 4.5:1 floor. Hues that already pass
    keep their exact current color, so unaffected vibes do not move.
    """
    bases = [background, surface] + ([raised] if raised is not None else [])
    lightness = 0.44
    while lightness > 0.10:
        color = _hsl(hue, 0.15, lightness)
        if all(contrast_ratio(color, base) >= 4.6 for base in bases):
            return color
        lightness = round(lightness - 0.01, 2)
    return _hsl(hue, 0.15, 0.10)


def _hsl_parts(hex_color: str) -> tuple[float, float, float]:
    """The (hue 0..360, saturation 0..1, lightness 0..1) of a `#RRGGBB` color."""
    red, green, blue = _hex_to_rgb(hex_color)
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    return hue * 360.0, saturation, lightness


def _adjust_toward_contrast(color: str, bases: list[str], *, target: float = 4.6) -> str:
    """`color` moved in lightness until it clears `target` on every base.

    Same approach as `_muted_text` (issue #309): 0.01 lightness steps, 0.1
    margin over the 4.5:1 floor, exact color kept when it already passes.
    The direction is away from the bases' luminance — a color darker than
    its bases darkens further, a lighter one lightens — so the same helper
    darkens text on light backgrounds and lightens it on dark ones (the
    luxury palette's near-black background lightens instead of darkening).
    Steps are bounded to lightness 0.05..0.95; a color that cannot clear the
    target on a mid-luminance base returns the closest reachable step. All
    bases this module derives against (white/light subtles, near-black/dark
    subtles) are reachable — the hue sweeps in `test_design_gates` prove it.
    """
    if all(contrast_ratio(color, base) >= target for base in bases):
        return color
    hue, saturation, lightness = _hsl_parts(color)
    mean_luminance = sum(_relative_luminance(base) for base in bases) / len(bases)
    step = -0.01 if _relative_luminance(color) < mean_luminance else 0.01
    current = lightness
    best = color
    while True:
        current = round(current + step, 2)
        if current < 0.05 or current > 0.95:
            return best
        candidate = _hsl(hue, saturation, current)
        if all(contrast_ratio(candidate, base) >= target for base in bases):
            return candidate
        best = candidate


def label_on_color(bg: str) -> str:
    """The label color for text sitting on a swatch of `bg`.

    White or near-black, whichever reads better — except mid-luminance
    colors, where neither clears 4.5:1 (issue #358: white on `#DB24AA`
    4.33, near-black 4.27). Pure black is the fallback there: it is
    visually indistinguishable from near-black at label sizes, and the
    best of white/black always clears 4.58:1 for any background, so the
    label always passes. The showcase's old `_is_dark` rule (white below a
    0.35 luminance threshold) picked white for light pinks and mid-greens
    at 2.68-3.57:1; this rule replaces it.
    """
    white = contrast_ratio("#FFFFFF", bg)
    dark = contrast_ratio("#101418", bg)
    if max(white, dark) >= 4.5:
        return "#FFFFFF" if white >= dark else "#101418"
    return "#000000"


def derive_text_roles(palette: dict[str, str]) -> dict[str, str]:
    """The text-safe roles for a palette (issue #358).

    - `primary_ink`: the primary hue adjusted to read as text on
      `background` and `surface` — the hero tagline, overlines, and ghost
      buttons render in this, never in the raw primary (health primary
      `#DB26AB` reads 4.31:1 on white, 3.88:1 on its surface).
    - `{role}_text` for each semantic role: the semantic hue adjusted to
      read on its `{role}_subtle` tint — badge text renders in this, never
      in the raw semantic color (warning `#D97706` reads 2.86:1 on
      `#FEF3C7`).

    The primary itself is never touched, so brand/hue identity is unchanged.
    Roles whose bases are missing from the palette are skipped, matching the
    contrast gate's missing-role rule. Always recompute from the current
    bases — never `setdefault` — so a palette whose primary moved (the
    seeded `_ensure_contrast` walk) gets a fresh ink.
    """
    derived: dict[str, str] = {}
    background = palette.get("background")
    surface = palette.get("surface")
    primary = palette.get("primary")
    if primary is not None and background is not None and surface is not None:
        derived["primary_ink"] = _adjust_toward_contrast(primary, [background, surface])
    for role in SEMANTIC_ROLES:
        base = palette.get(role)
        subtle = palette.get(f"{role}_subtle")
        if base is not None and subtle is not None:
            derived[f"{role}_text"] = _adjust_toward_contrast(base, [subtle])
    return derived


def _palette_from_hue(hue: float) -> dict[str, str]:
    """A full light-mode palette built around one hue.

    The primary lightness is walked (`_walk_button_lightness`) until the
    shared button text clears 4.6:1 on the light pair (primary, hover) and
    the dark pair (primary, adaptive dark hover) — a fixed lightness of
    0.50 admits no 4.5:1 pick for 68 of 360 hues, so the walk, not the
    fixed value, is what makes the hue sweep pass.
    """
    lightness = _walk_button_lightness(
        hue,
        0.72,
        0.50,
        hover_for=lambda primary, candidate, saturation: [
            _hsl(hue, saturation, round(max(0.28, candidate - 0.08), 2)),
            _adaptive_hover(primary, 0.75, 0.62),
        ],
    )
    primary = _hsl(hue, 0.72, lightness)
    primary_hover = _hsl(hue, 0.72, round(max(0.28, lightness - 0.08), 2))
    background = "#FFFFFF"
    surface = _hsl(hue, 0.35, 0.96)
    text = _hsl(hue, 0.30, 0.10)
    palette = {
        "primary": primary,
        "primary_hover": primary_hover,
        "primary_text": _primary_text(primary, primary_hover),
        "background": background,
        "surface": surface,
        "text": text,
        "text_muted": _muted_text(
            hue, background, surface, derived_surface_raised({"surface": surface, "text": text})
        ),
        "border": _hsl(hue, 0.20, 0.88),
        **{role: SEMANTIC_LIGHT[role] for role in SEMANTIC_ROLES},
        **{f"{role}_subtle": SEMANTIC_SUBTLE_LIGHT[role] for role in SEMANTIC_ROLES},
    }
    palette.update(derive_text_roles(palette))
    return palette


def _dark_variant(palette: dict[str, str]) -> dict[str, str]:
    """A dark-mode palette derived from a light one (same primary hue).

    The dark hover is adaptive (`_adaptive_hover`): the old unconditional
    lighten to 0.62 admits no shared button text at the 4.5 bar for a dark
    primary (playful seed 6: white on the hover is 2.85:1 — issue #360)
    and for blue primaries needing dark text (3.38:1 at hue 240), so the
    hover steps down for white text and up for dark text until the shared
    pick clears 4.6:1 on it.
    """
    hue = _hue_of(palette["primary"])
    primary = palette["primary"]
    primary_hover = _adaptive_hover(primary, 0.75, 0.62)
    dark = {
        "primary": primary,
        "primary_hover": primary_hover,
        "primary_text": _primary_text(primary, primary_hover),
        "background": _hsl(hue, 0.35, 0.07),
        "surface": _hsl(hue, 0.30, 0.12),
        "text": _hsl(hue, 0.20, 0.93),
        "text_muted": _hsl(hue, 0.15, 0.62),
        "border": _hsl(hue, 0.25, 0.20),
        **{role: SEMANTIC_DARK[role] for role in SEMANTIC_ROLES},
        **{f"{role}_subtle": SEMANTIC_SUBTLE_DARK[role] for role in SEMANTIC_ROLES},
    }
    dark.update(derive_text_roles(dark))
    return dark


def _luxury_palette() -> dict[str, str]:
    """Near-black + gold: the luxury vibe is inherently dark."""
    primary, primary_hover = "#C9A227", "#B08D1F"
    palette = {
        "primary": primary,
        "primary_hover": primary_hover,
        "primary_text": _primary_text(primary, primary_hover),
        "background": "#0C0B09",
        "surface": "#161410",
        "text": "#F5F0E4",
        "text_muted": "#A79B80",
        "border": "#2C2618",
        **{role: SEMANTIC_LIGHT[role] for role in SEMANTIC_ROLES},
        **{f"{role}_subtle": SEMANTIC_SUBTLE_LIGHT[role] for role in SEMANTIC_ROLES},
    }
    palette.update(derive_text_roles(palette))
    return palette


# (family, base hue or None for a bespoke palette, matching keywords)
_VIBE_FAMILIES: tuple[tuple[str, float | None, tuple[str, ...]], ...] = (
    (
        "fintech",
        217.0,
        ("fintech", "finance", "bank", "money", "payment", "crypto", "trading", "invest"),
    ),
    (
        "playful",
        16.0,
        ("playful", "fun", "kid", "game", "gaming", "party", "vibrant", "creative"),
    ),
    (
        "health",
        152.0,
        ("health", "fitness", "wellness", "medical", "doctor", "gym", "mindful"),
    ),
    ("luxury", None, ("luxury", "premium", "elegant", "fashion", "jewel", "watch", "couture")),
    ("food", 8.0, ("food", "restaurant", "recipe", "coffee", "bakery", "pizza", "dining")),
    ("travel", 190.0, ("travel", "hotel", "flight", "trip", "tour", "booking")),
    ("music", 270.0, ("music", "audio", "podcast", "song", "band", "streaming")),
    (
        "education",
        232.0,
        ("education", "school", "learn", "course", "student", "teach", "university"),
    ),
    (
        "eco",
        130.0,
        ("eco", "green", "sustainab", "garden", "plant", "nature", "solar", "climate"),
    ),
    ("social", 322.0, ("social", "community", "chat", "friend", "dating", "network")),
    (
        "productivity",
        222.0,
        ("productiv", "task", "todo", "project", "note", "calendar", "organize"),
    ),
)


def palette_for_vibe(keywords: str) -> dict[str, str]:
    """Map free-text vibe keywords to a base palette.

    Keyword families (fintech→blue, playful→warm vibrant, health→green,
    luxury→near-black+gold, …) win when one of their keywords appears in the
    input. Anything else — including empty input — falls back to a hue derived
    from a SHA-256 hash of the input, so the same input always yields the same
    palette.
    """
    lowered = keywords.strip().lower()
    for _family, hue, words in _VIBE_FAMILIES:
        if any(word in lowered for word in words):
            if hue is None:  # luxury
                return _luxury_palette()
            return _palette_from_hue(hue)
    digest = hashlib.sha256(lowered.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") % 360
    return _palette_from_hue(float(hue))


def tokens_for_app_type(
    app_type: str,
    palette: dict[str, str],
    theme: str,
    *,
    layout: LayoutProfile | None = None,
    pairing: TypePairing | None = None,
    motion: MotionProfile | None = None,
) -> dict[str, object]:
    """Type-aware design tokens for the wizard's chosen app type.

    `palette` is the light-mode base from `palette_for_vibe`; `theme` is one of
    "light", "dark" or "both". Per-type defaults:

    - mobile: 44px minimum touch target, safe-area spacing keys, 16px base type,
      generous radii.
    - dashboard: dark-first (a dark palette variant is always included), a
      5-color categorical chart ramp, a compact density spacing scale, and a
      tabular-numerals note.
    - website: type-forward scale with display sizes and generous line-heights.
    - pwa: website-like tokens plus install/splash/offline guidance.

    The seeded overrides (issue #299) vary what used to be fixed per app
    type: `layout` scales the spacing ladder (safe-area keys are platform
    constants and never scale) and replaces the radii, `pairing` adds a
    `typography` section with the chosen display/body/mono stacks, and
    `motion` adds the chosen temperament's durations and easings. Omitting
    all three reproduces the historical fixed tokens exactly.
    """
    if app_type not in APP_TYPES:
        raise ValueError(f"unknown app type: {app_type!r} (expected one of {sorted(APP_TYPES)})")
    if theme not in THEMES:
        raise ValueError(f"unknown theme: {theme!r} (expected one of {list(THEMES)})")
    light = dict(palette)
    dark = _dark_variant(light)
    if theme == "light":
        colors: object = light
    elif theme == "dark":
        colors = dark
    else:
        colors = {"light": light, "dark": dark}
    tokens: dict[str, object] = {"app_type": app_type, "theme": theme, "colors": colors}
    if app_type == "mobile":
        tokens.update(
            {
                "type_scale": {
                    "xs": 11,
                    "sm": 13,
                    "base": 16,
                    "lg": 19,
                    "xl": 23,
                    "2xl": 29,
                    "3xl": 35,
                },
                "spacing": {
                    "xs": 4,
                    "sm": 8,
                    "md": 16,
                    "lg": 24,
                    "xl": 32,
                    "2xl": 48,
                    "safe_area_top": 24,
                    "safe_area_bottom": 34,
                    "safe_area_horizontal": 16,
                },
                "radii": {"sm": 8, "md": 12, "lg": 16, "xl": 24, "full": 9999},
                "touch": {"min_target": 44, "min_spacing": 8},
            }
        )
    elif app_type == "dashboard":
        hue = _hue_of(light["primary"])
        tokens.update(
            {
                "color_scheme": "dark-first",
                # The dark variant ships even for a light theme: dark is the
                # default surface for dashboards.
                "dark_colors": dark,
                "chart_palette": [_hsl((hue + i * 72.0) % 360.0, 0.68, 0.60) for i in range(5)],
                "type_scale": {
                    "xs": 11,
                    "sm": 12,
                    "base": 13,
                    "lg": 15,
                    "xl": 18,
                    "2xl": 22,
                },
                "spacing": {"xs": 2, "sm": 4, "md": 8, "lg": 12, "xl": 16, "2xl": 24},
                "radii": {"sm": 4, "md": 6, "lg": 8, "full": 9999},
                "notes": [
                    "Numeric columns and KPIs use tabular figures "
                    "(font-variant-numeric: tabular-nums) so digits align."
                ],
            }
        )
    else:  # website and pwa share the type-forward foundation
        tokens.update(
            {
                "type_scale": {
                    "xs": 12,
                    "sm": 14,
                    "base": 16,
                    "lg": 20,
                    "xl": 24,
                    "2xl": 32,
                    "display_sm": 36,
                    "display_md": 48,
                    "display_lg": 64,
                    "display_xl": 80,
                },
                "line_heights": {
                    "display": 1.05,
                    "heading": 1.2,
                    "body": 1.65,
                    "caption": 1.5,
                },
                "spacing": {
                    "xs": 4,
                    "sm": 8,
                    "md": 16,
                    "lg": 32,
                    "xl": 64,
                    "2xl": 96,
                    "3xl": 128,
                },
                "radii": {"sm": 6, "md": 10, "lg": 14, "full": 9999},
            }
        )
        if app_type == "pwa":
            tokens["platform"] = {
                "install_prompt": (
                    "Show the install prompt after the second meaningful visit, "
                    "never on first paint; explain the value before asking."
                ),
                "splash_note": (
                    "Ship a 512px maskable icon and a theme-color meta tag so the "
                    "splash screen matches the brand."
                ),
                "offline_note": (
                    "Cache the app shell; the offline banner uses the warning color "
                    "and queued actions sync on reconnect."
                ),
            }
    if layout is not None:
        spacing = tokens.get("spacing")
        if isinstance(spacing, dict):
            tokens["spacing"] = {
                key: (
                    value
                    if not isinstance(value, (int, float)) or key.startswith("safe_area")
                    else max(2, round(value * layout.spacing_factor))
                )
                for key, value in spacing.items()
            }
        tokens["radii"] = dict(layout.radii)
    if pairing is not None:
        tokens["typography"] = {
            "font_display": pairing.display_stack,
            "font_body": pairing.body_stack,
            "font_mono": pairing.mono_stack,
            "display_weight": pairing.display_weight,
            "tracking": pairing.tracking,
            "scale_ratio": pairing.scale_ratio,
        }
    if motion is not None:
        tokens["motion"] = dict(motion.tokens)
    return tokens


_COMPONENTS: dict[str, list[tuple[str, str]]] = {
    "mobile": [
        (
            "Tab bar",
            "Bottom tab bar, five destinations max, 44pt touch targets, safe-area padding.",
        ),
        ("Cards", "One idea per card, 16pt radius, generous padding, actions in thumb reach."),
        ("Sheets", "Bottom sheets for secondary flows; drag handle; scrim dismisses."),
        ("Touch buttons", "Minimum 44x44pt hit area; primary button full-width on small screens."),
    ],
    "dashboard": [
        ("Data table", "Dense rows, sticky header, tabular numerals, row hover, bulk actions."),
        ("Charts", "Use the 5-color categorical ramp; direct labels over legends; dark-first."),
        ("Filters", "Persistent filter bar above tables; saved views; clear-all control."),
        ("Sidebar", "Collapsible nav rail; sections per primary entity; command-K hint."),
    ],
    "website": [
        ("Hero", "Display type, one headline, one primary CTA, generous whitespace."),
        ("Nav", "Sticky top nav; six items max; hamburger below 768px."),
        ("Sections", "Alternating rhythm, 64-128px vertical spacing; one idea per section."),
        ("Footer", "Sitemap columns, contact, legal; muted text on the surface color."),
        ("CTA", "Repeat the primary CTA after each major section; keep the label identical."),
    ],
    "pwa": [
        ("App shell", "Persistent header and bottom nav; the shell renders instantly from cache."),
        (
            "Install prompt",
            "Custom prompt after the second visit; explain the value before asking.",
        ),
        ("Offline banner", "Warning-color banner when offline; queued actions sync on reconnect."),
    ],
}

_WORDMARK_STACKS: dict[str, str] = {
    "mobile": 'ui-rounded, "SF Pro Rounded", "Nunito", system-ui, sans-serif',
    "website": '"Fraunces", "Playfair Display", Georgia, serif',
    "pwa": 'system-ui, -apple-system, "Segoe UI", sans-serif',
    "dashboard": '"Inter", system-ui, sans-serif',
}


def design_system_markdown(
    name: str,
    app_type: str,
    stack: str,
    palette: dict[str, str],
    theme: str,
    business: BusinessContext | None = None,
    *,
    components: list[tuple[str, str]] | None = None,
    choices: dict[str, str] | None = None,
    pairing: TypePairing | None = None,
) -> str:
    """The starter design system as markdown: token summary, the app type's
    component set, and logo/wordmark guidance.

    The wordmark is styled text (font stacks below) — it is never rendered into
    the geometric mark. When `business` is given and non-empty, a business
    context section records the industry/audience/tone/offering/description
    and the elements proposed for it (issue #283).

    The seeded overrides (issue #299): `components` replaces the app type's
    fixed component set with the chosen variant, `pairing` sets the wordmark
    stack from the chosen type pairing, and `choices` (the seed's skill keys)
    is recorded in a Design choices section so the markdown itself says how
    this system differs from its siblings.
    """
    if app_type not in APP_TYPES:
        raise ValueError(f"unknown app type: {app_type!r}")
    # The palette highlights show the theme the user actually picked: the dark
    # variant for a dark theme, the light base otherwise (for "both" the dark
    # values live in tokens.json).
    display = _dark_variant(palette) if theme == "dark" else palette
    lines = [
        f"# {name}",
        "",
        f"A **{app_type}** design system ({APP_TYPES[app_type].split(' — ')[1]}), "
        f"built for **{stack}** with a **{theme}** theme. Generated by `jev design new`.",
        "",
        "## Tokens",
        "",
        "The structured values live in `tokens.json` (colors, type_scale, spacing, radii). "
        "Palette highlights:",
        "",
    ]
    for key in (
        "primary",
        "primary_text",
        "background",
        "surface",
        "text",
        "text_muted",
        "border",
    ):
        lines.append(f"- `{key}`: {display.get(key, '—')}")
    if choices:
        lines += ["", "## Design choices", ""]
        lines.append(
            "Seeded from the design skills (`design-color`, `design-typography`, "
            "`design-layout`, `design-motion`): the same brief with a different "
            "seed is a different system on every axis below."
        )
        lines.append("")
        for key in ("color_strategy", "type_pairing", "layout", "motion", "components"):
            if key in choices:
                lines.append(f"- `{key}`: {choices[key]}")
    lines += ["", "## Components", ""]
    for component, guidance in components if components is not None else _COMPONENTS[app_type]:
        lines.append(f"- **{component}**: {guidance}")
    if business is not None and not business.is_empty():
        from jev_agent.core.design_business import propose_elements

        lines += ["", "## Business context", ""]
        for field, label in (
            ("industry", "Industry"),
            ("audience", "Audience"),
            ("tone", "Tone"),
            ("offering", "Offering"),
            ("description", "Description"),
        ):
            value = getattr(business, field, "").strip()
            if value:
                lines.append(f"- **{label}**: {value}")
        proposed = propose_elements(business)
        if proposed:
            lines.append("")
            lines.append("Proposed business elements:")
            lines.append("")
            for element in proposed:
                lines.append(f"- **{element.title}** ({element.key}): {element.why}")
    # The wordmark stack follows the seeded pairing when one was chosen,
    # else the app type's fixed stack (the historical default).
    wordmark_stack = pairing.display_stack if pairing is not None else _WORDMARK_STACKS[app_type]
    wordmark_weight = pairing.display_weight if pairing is not None else "600-700"
    wordmark_tracking = pairing.tracking if pairing is not None else -0.01
    lines += [
        "",
        "## Logo",
        "",
        "The mark (`logo.svg`) is abstract geometry — concentric shapes in the primary "
        "palette. It never contains text.",
        "",
        "**Wordmark** (the product name set in type, never baked into the mark):",
        "",
        f"- Font stack: `{wordmark_stack}`",
        f"- Weight {wordmark_weight}, tight letter-spacing ({wordmark_tracking}em) "
        "for the product name.",
        "- On dark surfaces use the text color; the mark carries the brand color.",
        "",
        "## Usage notes",
        "",
    ]
    if app_type == "mobile":
        lines.append(
            "- Respect the safe-area spacing keys; every interactive element meets the "
            "44pt minimum touch target."
        )
    elif app_type == "dashboard":
        lines.append(
            "- Dark is the default surface; charts use the 5-color categorical ramp from "
            "`tokens.json`; numbers are tabular."
        )
    elif app_type == "website":
        lines.append(
            "- Lead with display type and whitespace; body copy never exceeds 65 characters "
            "per line."
        )
    else:
        lines.append(
            "- The app shell must paint from cache; the install prompt and offline banner "
            "are first-class components, not afterthoughts."
        )
    lines += [
        "- Text on primary buttons uses `primary_text`: whichever of white and "
        "near-black reads better on the worse of the primary and its hover state "
        "(always at least 4.5:1, the WCAG AA normal-text floor, on both states "
        "in both themes; the primary lightness is walked until a shared pick "
        "clears it).",
        "- The stack sets token defaults and export targets only; no code was scaffolded.",
        "",
    ]
    return "\n".join(lines)
