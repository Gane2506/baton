"""Design-kit content for `jev design new`: app types, stacks, token defaults,
vibe-to-palette mapping, and the starter design-system markdown.

Pure functions, stdlib-only. Persistence (the design library) lives in
`jev_agent.core.design_library`, built by a parallel workstream — this module
only produces the content the wizard writes into it.
"""

from __future__ import annotations

import colorsys
import hashlib
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
    "background",
    "surface",
    "text",
    "text_muted",
    "border",
    "success",
    "warning",
    "danger",
)

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
    the text color was not chosen for. Mid-luminance brand hues
    (reds/oranges/purples) admit no 4.5:1 text color on every state; the pick
    always keeps at least 3:1 (WCAG AA for UI components).
    """
    white = min(contrast_ratio("#FFFFFF", primary), contrast_ratio("#FFFFFF", primary_hover))
    dark = min(contrast_ratio("#101418", primary), contrast_ratio("#101418", primary_hover))
    return "#FFFFFF" if white >= dark else "#101418"


def _hsl(hue: float, saturation: float, lightness: float) -> str:
    """One palette color from HSL degrees / 0..1 / 0..1."""
    r, g, b = colorsys.hls_to_rgb((hue % 360.0) / 360.0, lightness, saturation)
    return _rgb_to_hex(r, g, b)


def _hue_of(hex_color: str) -> float:
    """The hue (0..360) of a `#RRGGBB` color."""
    r, g, b = _hex_to_rgb(hex_color)
    hue, _lightness, _saturation = colorsys.rgb_to_hls(r, g, b)
    return hue * 360.0


def _palette_from_hue(hue: float) -> dict[str, str]:
    """A full light-mode palette built around one hue."""
    primary = _hsl(hue, 0.72, 0.50)
    primary_hover = _hsl(hue, 0.72, 0.42)
    return {
        "primary": primary,
        "primary_hover": primary_hover,
        "primary_text": _primary_text(primary, primary_hover),
        "background": "#FFFFFF",
        "surface": _hsl(hue, 0.35, 0.96),
        "text": _hsl(hue, 0.30, 0.10),
        "text_muted": _hsl(hue, 0.15, 0.44),
        "border": _hsl(hue, 0.20, 0.88),
        "success": "#16A34A",
        "warning": "#D97706",
        "danger": "#DC2626",
    }


def _dark_variant(palette: dict[str, str]) -> dict[str, str]:
    """A dark-mode palette derived from a light one (same primary hue)."""
    hue = _hue_of(palette["primary"])
    primary = palette["primary"]
    primary_hover = _hsl(hue, 0.75, 0.62)
    return {
        "primary": primary,
        "primary_hover": primary_hover,
        "primary_text": _primary_text(primary, primary_hover),
        "background": _hsl(hue, 0.35, 0.07),
        "surface": _hsl(hue, 0.30, 0.12),
        "text": _hsl(hue, 0.20, 0.93),
        "text_muted": _hsl(hue, 0.15, 0.62),
        "border": _hsl(hue, 0.25, 0.20),
        "success": "#22C55E",
        "warning": "#F59E0B",
        "danger": "#F87171",
    }


def _luxury_palette() -> dict[str, str]:
    """Near-black + gold: the luxury vibe is inherently dark."""
    primary, primary_hover = "#C9A227", "#B08D1F"
    return {
        "primary": primary,
        "primary_hover": primary_hover,
        "primary_text": _primary_text(primary, primary_hover),
        "background": "#0C0B09",
        "surface": "#161410",
        "text": "#F5F0E4",
        "text_muted": "#A79B80",
        "border": "#2C2618",
        "success": "#16A34A",
        "warning": "#D97706",
        "danger": "#DC2626",
    }


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
        "(always at least 3:1, WCAG AA for UI components; 4.5:1 wherever white "
        "qualifies).",
        "- The stack sets token defaults and export targets only; no code was scaffolded.",
        "",
    ]
    return "\n".join(lines)
