"""World-class design system generation for the Design Studio (issue #155).

This module produces the rich, award-worthy outputs the studio generates:
vibe-driven aesthetic profiles, comprehensive design tokens (beyond basic
colors), and stunning standalone HTML showcase galleries.

The pipeline:
1. `VIBE_PROFILES` — rich aesthetic definitions (minimal, bold, playful,
   elegant, brutalist, etc.) with typography, radius, shadow, spacing, and
   motion character — not just hue.
2. `rich_tokens()` — full token sets: neutral ramps, accent ramps, semantic
   roles, dark-mode pairs, shadow/elevation scales, motion/easing, and a real
   typographic hierarchy (display/headline/body/caption with weight, tracking,
   leading).
3. `render_showcase()` — a self-contained HTML gallery: hero, palette
   visualization, typography specimens, component showcase (tokens applied),
   dark/light toggle. No external dependencies.

Pure functions, stdlib-only. The dashboard saves the showcase as
`showcase.html` alongside the system; the tokens merge into `tokens.json`.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass

from jev_agent.core.design_kit import (
    SEMANTIC_DARK,
    SEMANTIC_LIGHT,
    SEMANTIC_ROLES,
    SEMANTIC_SUBTLE_DARK,
    SEMANTIC_SUBTLE_LIGHT,
    _adaptive_hover,
    _primary_text,
    _walk_button_lightness,
    derive_text_roles,
    label_on_color,
)

# ---------------------------------------------------------------------------
# Color helpers (stdlib-only)
# ---------------------------------------------------------------------------


def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"expected #RRGGBB, got {value!r}")
    return (
        int(text[0:2], 16) / 255.0,
        int(text[2:4], 16) / 255.0,
        int(text[4:6], 16) / 255.0,
    )


def _rgb_to_hex(r: float, g: float, b: float) -> str:
    def clamp(v: float) -> int:
        return max(0, min(255, round(v * 255)))

    return f"#{clamp(r):02X}{clamp(g):02X}{clamp(b):02X}"


def _hsl(hue: float, saturation: float, lightness: float) -> str:
    r, g, b = colorsys.hls_to_rgb((hue % 360.0) / 360.0, lightness, saturation)
    return _rgb_to_hex(r, g, b)


def _hue_of(hex_color: str) -> float:
    r, g, b = _hex_to_rgb(hex_color)
    hue, _, _ = colorsys.rgb_to_hls(r, g, b)
    return hue * 360.0


# ---------------------------------------------------------------------------
# Vibe profiles — the aesthetic soul of each generated system
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VibeProfile:
    """A complete aesthetic direction, not just a hue.

    Each profile defines how the system *feels*: the typographic voice, the
    corner philosophy, the shadow language, the spacing rhythm, and the motion
    character. The token generator and showcase renderer both read from here,
    so the output truly embodies the vibe instead of slapping colors on a
    generic template.
    """

    key: str
    name: str
    tagline: str
    description: str
    # Palette character
    hue: float  # base hue 0..360
    saturation: float  # 0..1, primary saturation
    neutral_warmth: float  # -1 (cool) .. 1 (warm), tints the neutrals
    # Typography character
    display_stack: str  # CSS font-family for display/headline
    body_stack: str  # CSS font-family for body/UI
    mono_stack: str  # CSS font-family for code/labels
    display_weight: int  # 400..900
    tight_tracking: float  # letter-spacing for display, em
    # Shape character
    radius_base: int  # px, the "md" radius; scales derive from it
    radius_full: bool  # whether pill shapes are on-brand
    # Shadow character: "flat" | "soft" | "dramatic"
    shadow_style: str
    # Spacing character: "tight" | "balanced" | "generous"
    spacing_style: str
    # Motion character
    ease: str  # CSS easing for the primary motion
    # Hero treatment for the showcase
    hero_style: str  # "gradient" | "pattern" | "type" | "split" | "dark"


VIBE_PROFILES: dict[str, VibeProfile] = {
    "minimal": VibeProfile(
        key="minimal",
        name="Minimal",
        tagline="Less, but better.",
        description=(
            "Swiss-inspired restraint: one accent, generous whitespace, quiet "
            "neutrals, and typography that does the talking. Every element earns "
            "its place."
        ),
        hue=220.0,
        saturation=0.65,
        neutral_warmth=0.0,
        display_stack='"Inter", system-ui, -apple-system, sans-serif',
        body_stack='"Inter", system-ui, -apple-system, sans-serif',
        mono_stack='"SF Mono", "JetBrains Mono", ui-monospace, monospace',
        display_weight=600,
        tight_tracking=-0.025,
        radius_base=8,
        radius_full=True,
        shadow_style="soft",
        spacing_style="generous",
        ease="cubic-bezier(0.16, 1, 0.3, 1)",
        hero_style="type",
    ),
    "bold": VibeProfile(
        key="bold",
        name="Bold",
        tagline="Impossible to ignore.",
        description=(
            "High-contrast, high-energy: saturated color, heavy display type, "
            "tight spacing, and sharp corners. Built for brands that shout."
        ),
        hue=8.0,
        saturation=0.85,
        neutral_warmth=0.1,
        display_stack='"Archivo", "Inter", system-ui, sans-serif',
        body_stack='"Inter", system-ui, sans-serif',
        mono_stack='"JetBrains Mono", ui-monospace, monospace',
        display_weight=800,
        tight_tracking=-0.03,
        radius_base=4,
        radius_full=False,
        shadow_style="dramatic",
        spacing_style="tight",
        ease="cubic-bezier(0.34, 1.3, 0.64, 1)",
        hero_style="gradient",
    ),
    "playful": VibeProfile(
        key="playful",
        name="Playful",
        tagline="Joy is a feature.",
        description=(
            "Rounded, bouncy, and warm: candy colors, soft shapes, springy "
            "motion, and friendly type. Feels like a high-five."
        ),
        hue=32.0,
        saturation=0.80,
        neutral_warmth=0.6,
        display_stack='"Nunito", ui-rounded, "SF Pro Rounded", system-ui, sans-serif',
        body_stack='"Nunito", ui-rounded, system-ui, sans-serif',
        mono_stack='"JetBrains Mono", ui-monospace, monospace',
        display_weight=700,
        tight_tracking=-0.015,
        radius_base=16,
        radius_full=True,
        shadow_style="soft",
        spacing_style="balanced",
        ease="cubic-bezier(0.34, 1.56, 0.64, 1)",
        hero_style="pattern",
    ),
    "elegant": VibeProfile(
        key="elegant",
        name="Elegant",
        tagline="Quiet luxury.",
        description=(
            "Refined serif display, muted champagne-and-ink palette, hairline "
            "borders, and unhurried motion. Old money, new interface."
        ),
        hue=42.0,
        saturation=0.35,
        neutral_warmth=0.8,
        display_stack='"Fraunces", "Playfair Display", Georgia, serif',
        body_stack='"Inter", system-ui, sans-serif',
        mono_stack='"SF Mono", ui-monospace, monospace',
        display_weight=500,
        tight_tracking=-0.01,
        radius_base=6,
        radius_full=False,
        shadow_style="soft",
        spacing_style="generous",
        ease="cubic-bezier(0.65, 0, 0.35, 1)",
        hero_style="split",
    ),
    "brutalist": VibeProfile(
        key="brutalist",
        name="Brutalist",
        tagline="Raw and honest.",
        description=(
            "No shadows, no rounding, no apologies: thick borders, stark "
            "contrast, mono accents, and type set like a manifesto."
        ),
        hue=55.0,
        saturation=0.90,
        neutral_warmth=0.0,
        display_stack='"Space Grotesk", "Archivo", system-ui, sans-serif',
        body_stack='"Inter", system-ui, sans-serif',
        mono_stack='"Space Mono", "JetBrains Mono", ui-monospace, monospace',
        display_weight=700,
        tight_tracking=-0.02,
        radius_base=0,
        radius_full=False,
        shadow_style="flat",
        spacing_style="tight",
        ease="cubic-bezier(0.85, 0, 0.15, 1)",
        hero_style="dark",
    ),
    "calm": VibeProfile(
        key="calm",
        name="Calm",
        tagline="Breathe easy.",
        description=(
            "Soft greens and warm neutrals, airy spacing, gentle rounding, and "
            "slow motion. Designed for focus, not for fighting."
        ),
        hue=150.0,
        saturation=0.35,
        neutral_warmth=0.3,
        display_stack='"Inter", system-ui, sans-serif',
        body_stack='"Inter", system-ui, sans-serif',
        mono_stack='"SF Mono", ui-monospace, monospace',
        display_weight=600,
        tight_tracking=-0.02,
        radius_base=12,
        radius_full=True,
        shadow_style="soft",
        spacing_style="generous",
        ease="cubic-bezier(0.16, 1, 0.3, 1)",
        hero_style="gradient",
    ),
}


def vibe_for_keywords(keywords: str) -> VibeProfile:
    """Map free-text vibe keywords to the closest profile.

    Falls back to "minimal" for empty input — restraint is the safest default.
    """
    lowered = keywords.strip().lower()
    if not lowered:
        return VIBE_PROFILES["minimal"]
    scores: dict[str, int] = {}
    hints: dict[str, tuple[str, ...]] = {
        "minimal": ("minimal", "clean", "simple", "swiss", "quiet", "restraint"),
        "bold": ("bold", "loud", "energetic", "vibrant", "strong", "punchy"),
        "playful": ("playful", "fun", "friendly", "cute", "joy", "whimsical", "kid"),
        "elegant": ("elegant", "luxury", "premium", "refined", "sophisticated", "serif"),
        "brutalist": ("brutalist", "brutal", "raw", "punk", "harsh", "concrete"),
        "calm": ("calm", "peaceful", "serene", "soft", "gentle", "mindful", "wellness"),
    }
    for key, words in hints.items():
        scores[key] = sum(1 for w in words if w in lowered)
    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return VIBE_PROFILES["minimal"]
    return VIBE_PROFILES[best]


# ---------------------------------------------------------------------------
# Rich token generation
# ---------------------------------------------------------------------------


def _neutral_ramp(hue: float, warmth: float, dark: bool = False) -> dict[str, str]:
    """A 9-step neutral ramp, subtly tinted by the brand hue and warmth.

    Warmth shifts the hue toward warm (30°) or cool (220°) neutrals.
    """
    # Blend the brand hue toward a neutral anchor based on warmth.
    anchor = 30.0 if warmth >= 0 else 220.0
    strength = abs(warmth) * 0.6 + 0.15
    neutral_hue = (hue * strength + anchor * (1 - strength)) % 360.0
    sat = 0.04 + abs(warmth) * 0.05
    names = ["50", "100", "200", "300", "400", "500", "600", "700", "800", "900"]
    if dark:
        # Dark ramp: 50 is near-black, 950 is near-white (inverted lightness).
        steps = [0.06, 0.10, 0.15, 0.22, 0.32, 0.45, 0.60, 0.75, 0.88, 0.93]
    else:
        steps = [0.97, 0.93, 0.88, 0.80, 0.68, 0.52, 0.36, 0.24, 0.14, 0.08]
    # Add the 950 anchor.
    if dark:
        steps.append(0.97)
    else:
        steps.append(0.04)
    names.append("950")
    return {
        f"neutral_{name}": _hsl(neutral_hue, sat, lightness)
        for name, lightness in zip(names, steps, strict=True)
    }


def _accent_ramp(hue: float, saturation: float) -> dict[str, str]:
    """A 5-step accent ramp around the primary hue."""
    return {
        "accent_100": _hsl(hue, saturation * 0.7, 0.88),
        "accent_300": _hsl(hue, saturation * 0.85, 0.68),
        "accent_500": _hsl(hue, saturation, 0.50),
        "accent_700": _hsl(hue, saturation, 0.36),
        "accent_900": _hsl(hue, saturation * 0.9, 0.22),
    }


def _shadows(style: str) -> dict[str, str]:
    if style == "flat":
        none = "none"
        return {"xs": none, "sm": none, "md": none, "lg": none, "xl": none}
    if style == "dramatic":
        return {
            "xs": "0 1px 2px rgba(0,0,0,0.12)",
            "sm": "0 2px 8px rgba(0,0,0,0.14)",
            "md": "0 8px 24px rgba(0,0,0,0.16)",
            "lg": "0 16px 40px rgba(0,0,0,0.20)",
            "xl": "0 24px 64px rgba(0,0,0,0.24)",
        }
    # soft (default)
    return {
        "xs": "0 1px 2px rgba(0,0,0,0.05)",
        "sm": "0 2px 4px rgba(0,0,0,0.06)",
        "md": "0 4px 12px rgba(0,0,0,0.08)",
        "lg": "0 12px 24px rgba(0,0,0,0.10)",
        "xl": "0 20px 48px rgba(0,0,0,0.12)",
    }


def _radii(base: int, pill: bool) -> dict[str, int | str]:
    radii: dict[str, int | str] = {
        "none": 0,
        "xs": max(2, base // 4),
        "sm": max(4, base // 2),
        "md": base,
        "lg": base * 2,
        "xl": base * 3,
    }
    radii["full"] = 9999 if pill else base * 2
    return radii


def _spacing(style: str) -> dict[str, int]:
    if style == "tight":
        return {
            "3xs": 2,
            "2xs": 4,
            "xs": 6,
            "sm": 10,
            "md": 14,
            "lg": 20,
            "xl": 28,
            "2xl": 40,
            "3xl": 56,
            "4xl": 80,
        }
    if style == "generous":
        return {
            "3xs": 4,
            "2xs": 8,
            "xs": 12,
            "sm": 16,
            "md": 24,
            "lg": 40,
            "xl": 64,
            "2xl": 96,
            "3xl": 144,
            "4xl": 192,
        }
    return {
        "3xs": 2,
        "2xs": 4,
        "xs": 8,
        "sm": 12,
        "md": 16,
        "lg": 24,
        "xl": 32,
        "2xl": 48,
        "3xl": 64,
        "4xl": 96,
    }


def _typography(vibe: VibeProfile, overrides: dict[str, object] | None = None) -> dict[str, object]:
    """The type system: stacks, weights, tracking, and the display/headline/
    body/caption sizes. `overrides` (from a seeded type pairing) replaces the
    stacks, display weight, and tracking; the sizes stay the profile's."""
    overrides = overrides or {}
    display_stack = overrides.get("font_display")
    body_stack = overrides.get("font_body")
    mono_stack = overrides.get("font_mono")
    weight = overrides.get("display_weight")
    tracking = overrides.get("tracking")
    w = int(weight) if isinstance(weight, (int, float)) else vibe.display_weight
    tight = float(tracking) if isinstance(tracking, (int, float)) else vibe.tight_tracking
    return {
        "font_display": display_stack if isinstance(display_stack, str) else vibe.display_stack,
        "font_body": body_stack if isinstance(body_stack, str) else vibe.body_stack,
        "font_mono": mono_stack if isinstance(mono_stack, str) else vibe.mono_stack,
        "display_xl": {"size": 76, "weight": w, "tracking": tight, "leading": 1.02},
        "display_lg": {"size": 60, "weight": w, "tracking": tight, "leading": 1.05},
        "display_md": {"size": 48, "weight": w, "tracking": tight, "leading": 1.08},
        "display_sm": {"size": 36, "weight": w, "tracking": tight, "leading": 1.10},
        "headline_lg": {
            "size": 28,
            "weight": 650 if w > 600 else 600,
            "tracking": -0.01,
            "leading": 1.20,
        },
        "headline_md": {"size": 22, "weight": 600, "tracking": -0.008, "leading": 1.25},
        "headline_sm": {"size": 18, "weight": 600, "tracking": -0.005, "leading": 1.30},
        "body_lg": {"size": 17, "weight": 400, "tracking": 0.0, "leading": 1.60},
        "body_md": {"size": 15, "weight": 400, "tracking": 0.0, "leading": 1.60},
        "body_sm": {"size": 13, "weight": 400, "tracking": 0.01, "leading": 1.55},
        "caption": {"size": 12, "weight": 500, "tracking": 0.02, "leading": 1.45},
        "overline": {"size": 11, "weight": 600, "tracking": 0.12, "leading": 1.40},
    }


def _motion(vibe: VibeProfile) -> dict[str, str]:
    return {
        "duration_instant": "90ms",
        "duration_fast": "160ms",
        "duration_normal": "260ms",
        "duration_slow": "420ms",
        "duration_slower": "700ms",
        "ease_standard": vibe.ease,
        "ease_out": "cubic-bezier(0.16, 1, 0.3, 1)",
        "ease_in_out": "cubic-bezier(0.65, 0, 0.35, 1)",
        "ease_spring": "cubic-bezier(0.34, 1.56, 0.64, 1)",
    }


def rich_tokens(
    vibe: VibeProfile,
    theme: str = "both",
    *,
    spacing: dict[str, int] | None = None,
    radii: dict[str, int | str] | None = None,
    shadow_style: str | None = None,
    motion: dict[str, str] | None = None,
    typography: dict[str, object] | None = None,
) -> dict[str, object]:
    """The complete token set for a vibe: color ramps, dark pairs, typography,
    spacing, radii, shadows, and motion. Backwards-compatible with the basic
    token shape (colors/type_scale/spacing/radii keys still present).

    The keyword overrides (issue #299) let a seeded generation replace the
    vibe's spacing, radii, shadow style, motion, and typography with the
    choices the seed made, instead of the profile's fixed values.
    """
    hue, sat = vibe.hue, vibe.saturation
    neutrals = _neutral_ramp(hue, vibe.neutral_warmth)
    dark_neutrals = _neutral_ramp(hue, vibe.neutral_warmth, dark=True)
    accent = _accent_ramp(hue, sat)

    # The button pair is walked, not fixed: a fixed accent_500 at lightness
    # 0.50 admits no 4.5:1 shared button text for orange/magenta hues
    # (worst 3.24:1 across hues at sat 0.8), and the dark primary at 0.62
    # admits none for blue hues (worst 3.52:1). The walk moves the primary
    # lightness minimally (closest first) until the shared pick clears
    # 4.6:1 on both states; accent_500/700 follow the walked pair so the
    # ramp and the primary token never disagree.
    light_lightness = _walk_button_lightness(
        hue,
        sat,
        0.50,
        hover_for=lambda _primary, candidate, saturation: [
            _hsl(hue, saturation, round(max(0.05, candidate - 0.14), 2))
        ],
    )
    primary = _hsl(hue, sat, light_lightness)
    primary_hover = _hsl(hue, sat, round(max(0.05, light_lightness - 0.14), 2))
    accent = {**accent, "accent_500": primary, "accent_700": primary_hover}

    dark_sat = min(1.0, sat + 0.1)
    dark_lightness = _walk_button_lightness(
        hue,
        dark_sat,
        0.62,
        hover_for=lambda candidate, candidate_lightness, saturation: [
            _adaptive_hover(candidate, saturation, round(candidate_lightness + 0.08, 2))
        ],
    )
    dark_primary = _hsl(hue, dark_sat, dark_lightness)
    dark_hover = _adaptive_hover(dark_primary, dark_sat, round(dark_lightness + 0.08, 2))

    # Light semantic colors (canonical tables in `design_kit`; the `*_text`
    # roles are derived below so badge text reads on the tints — issue #358).
    light_colors: dict[str, str] = {
        **{f"neutral_{k.split('_')[1]}": v for k, v in neutrals.items()},
        **accent,
        "primary": primary,
        "primary_hover": primary_hover,
        "primary_text": _primary_text(primary, primary_hover),
        "primary_subtle": accent["accent_100"],
        "background": neutrals["neutral_50"],
        "surface": "#FFFFFF",
        "surface_raised": neutrals["neutral_100"],
        "surface_overlay": "#FFFFFF",
        "text": neutrals["neutral_900"],
        "text_muted": neutrals["neutral_600"],
        "text_faint": neutrals["neutral_400"],
        "border": neutrals["neutral_200"],
        "border_strong": neutrals["neutral_300"],
        **{role: SEMANTIC_LIGHT[role] for role in SEMANTIC_ROLES},
        **{f"{role}_subtle": SEMANTIC_SUBTLE_LIGHT[role] for role in SEMANTIC_ROLES},
        # Text on the danger button itself (issue #359 F): white on the
        # light danger base, near-black on the dark one — the `*_text`
        # roles are text on the tints, not on the bases.
        "on_danger": label_on_color(SEMANTIC_LIGHT["danger"]),
    }
    light_colors.update(derive_text_roles(light_colors))
    # Dark semantic colors
    dark_colors: dict[str, str] = {
        **{f"neutral_{k.split('_')[1]}": v for k, v in dark_neutrals.items()},
        "primary": dark_primary,
        "primary_hover": dark_hover,
        "primary_text": _primary_text(dark_primary, dark_hover),
        "primary_subtle": _hsl(hue, sat * 0.6, 0.18),
        "background": dark_neutrals["neutral_50"],
        "surface": dark_neutrals["neutral_100"],
        "surface_raised": dark_neutrals["neutral_200"],
        "surface_overlay": dark_neutrals["neutral_200"],
        "text": dark_neutrals["neutral_900"],
        "text_muted": dark_neutrals["neutral_600"],
        "text_faint": dark_neutrals["neutral_500"],
        "border": dark_neutrals["neutral_200"],
        "border_strong": dark_neutrals["neutral_300"],
        **{role: SEMANTIC_DARK[role] for role in SEMANTIC_ROLES},
        **{f"{role}_subtle": SEMANTIC_SUBTLE_DARK[role] for role in SEMANTIC_ROLES},
        "on_danger": label_on_color(SEMANTIC_DARK["danger"]),
    }
    dark_colors.update(derive_text_roles(dark_colors))

    if theme == "dark":
        colors: object = dark_colors
    elif theme == "light":
        colors = light_colors
    else:
        colors = {"light": light_colors, "dark": dark_colors}

    spacing = _spacing(vibe.spacing_style) if spacing is None else dict(spacing)
    # Backwards-compatible aliases
    type_scale = {
        "xs": 12,
        "sm": 14,
        "base": 16,
        "lg": 20,
        "xl": 24,
        "2xl": 32,
    }
    return {
        "vibe": vibe.key,
        "vibe_name": vibe.name,
        "vibe_tagline": vibe.tagline,
        "theme": theme,
        "colors": colors,
        "typography": _typography(vibe, typography),
        "spacing": spacing,
        "radii": _radii(vibe.radius_base, vibe.radius_full) if radii is None else dict(radii),
        "shadows": _shadows(shadow_style if shadow_style is not None else vibe.shadow_style),
        "motion": _motion(vibe) if motion is None else dict(motion),
        # Backwards-compatible keys for older consumers
        "type_scale": type_scale,
    }
