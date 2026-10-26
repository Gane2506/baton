"""Executable design-system skill catalog (issue #299).

The `.claude/skills/design-*` skills are the prose an agent reads; this module
is the same knowledge made runnable — the generator consults it, so a rule
that lives only in prose would drift from what generation actually emits.
Keep the two in agreement.

What it holds, per skill:

- `design-color`: color strategies (harmony schemes + hue rotation +
  saturation/lightness variation) and the seeded palette builder.
- `design-typography`: a type-pairing catalog (display/body/mono stacks,
  character, scale ratio) — deliberately not all Inter.
- `design-layout`: spacing/radius profiles (density + radius character +
  shadow language).
- `design-motion`: motion profiles (temperament -> durations/easings/stagger,
  on Atelier's weight scale).
- `design-accessibility`: the contrast gate — a palette that fails it is
  re-rolled, never shipped.

Selection is seeded: `random.Random(seed)`, deterministic per seed. The
business context and vibe keywords are a *steer* (they pick the anchor hue
and narrow the pairing pool), never a fixed answer — the same brief with a
different seed is a different system. A fingerprint of the key axes lets the
generator detect a collision with an existing system and re-roll.

Pure functions, stdlib-only, like `design_kit.py`.
"""

from __future__ import annotations

import colorsys
import hashlib
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jev_agent.core.design_kit import contrast_ratio, palette_for_vibe

if TYPE_CHECKING:
    from jev_agent.core.design_showcase import VibeProfile

MAX_REROLLS = 10


# ---------------------------------------------------------------------------
# Small color helpers (local copies: design_kit keeps its own privates)
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
    hue, _lightness, _saturation = colorsys.rgb_to_hls(r, g, b)
    return hue * 360.0


def _on_color(bg: str) -> str:
    """White or near-black, whichever reads better on `bg` (same rule as the
    design kit: the pick always keeps at least 3:1 on the background)."""
    white = contrast_ratio("#FFFFFF", bg)
    dark = contrast_ratio("#101418", bg)
    return "#FFFFFF" if white >= dark else "#101418"


# ---------------------------------------------------------------------------
# Color strategies (design-color)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColorStrategy:
    """One way to turn a steered anchor hue into a palette.

    `rotation` turns the anchor into the primary hue (0 keeps it; a range
    samples a hue-shift). `accent_offsets` are the secondary hues relative to
    the primary, in degrees — the harmony scheme. Saturation and lightness
    are sampled per system, so even the same scheme twice is not the same
    palette.
    """

    key: str
    name: str
    scheme: str
    rotation: tuple[float, float]  # (min, max) degrees added to the anchor hue
    accent_offsets: tuple[float, ...]
    saturation: tuple[float, float]  # (min, max)
    lightness: tuple[float, float]  # (min, max)
    description: str


COLOR_STRATEGIES: tuple[ColorStrategy, ...] = (
    ColorStrategy(
        key="analogous",
        name="Analogous",
        scheme="analogous",
        rotation=(0.0, 0.0),
        accent_offsets=(30.0, -30.0),
        saturation=(0.55, 0.75),
        lightness=(0.44, 0.54),
        description="Neighbors on the wheel; calm, cohesive, the safest default.",
    ),
    ColorStrategy(
        key="complementary",
        name="Complementary",
        scheme="complementary",
        rotation=(0.0, 0.0),
        accent_offsets=(180.0,),
        saturation=(0.60, 0.85),
        lightness=(0.44, 0.54),
        description="The anchor plus its opposite; high energy — the complement stays an accent.",
    ),
    ColorStrategy(
        key="split-complementary",
        name="Split complementary",
        scheme="split-complementary",
        rotation=(0.0, 0.0),
        accent_offsets=(150.0, 210.0),
        saturation=(0.60, 0.80),
        lightness=(0.44, 0.54),
        description="The energy of complementary with less clash.",
    ),
    ColorStrategy(
        key="triadic",
        name="Triadic",
        scheme="triadic",
        rotation=(0.0, 0.0),
        accent_offsets=(120.0, 240.0),
        saturation=(0.55, 0.75),
        lightness=(0.44, 0.54),
        description="Three hues 120° apart; vivid only when two of the three stay quiet.",
    ),
    ColorStrategy(
        key="monochromatic",
        name="Monochromatic",
        scheme="monochromatic",
        rotation=(0.0, 0.0),
        accent_offsets=(),
        saturation=(0.45, 0.70),
        lightness=(0.38, 0.56),
        description="One hue; lightness and saturation do all the work — the most restrained.",
    ),
    ColorStrategy(
        key="hue-shift",
        name="Hue shift",
        scheme="hue-shift",
        rotation=(45.0, 135.0),
        accent_offsets=(30.0,),
        saturation=(0.55, 0.80),
        lightness=(0.44, 0.54),
        description=(
            "Rotates the anchor 45-135° away from the obvious family hue. Same "
            "brief, different answer — the move that keeps two restaurant "
            "systems from sharing a palette."
        ),
    ),
)


def steer_hue(keywords: str) -> float:
    """The anchor hue a brief steers toward.

    Reuses the design kit's vibe-family mapping (fintech→blue, food→warm red,
    …) and its hash fallback, so the steer stays consistent with the rest of
    the generator; the seed — not the steer — decides the final palette.
    """
    return _hue_of(palette_for_vibe(keywords)["primary"])


# ---------------------------------------------------------------------------
# Type pairings (design-typography)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TypePairing:
    """A display/body pairing: two families that contrast on exactly one
    axis, plus the scale that separates them. Deliberately not all Inter —
    Inter is a fine body face, never the unexamined default for both faces.
    """

    key: str
    name: str
    character: str
    display_stack: str
    body_stack: str
    mono_stack: str
    display_weight: int
    tracking: float  # display letter-spacing, em
    scale_ratio: float  # 1.2 / 1.25 / 1.333


TYPE_PAIRINGS: tuple[TypePairing, ...] = (
    TypePairing(
        key="archivo-inter",
        name="Archivo / Inter",
        character="Bold grotesk headlines, neutral UI body.",
        display_stack='"Archivo", "Inter", system-ui, sans-serif',
        body_stack='"Inter", system-ui, sans-serif',
        mono_stack='"JetBrains Mono", ui-monospace, monospace',
        display_weight=800,
        tracking=-0.03,
        scale_ratio=1.333,
    ),
    TypePairing(
        key="fraunces-newsreader",
        name="Fraunces / Newsreader",
        character="Editorial serif throughout; literary and warm.",
        display_stack='"Fraunces", "Playfair Display", Georgia, serif',
        body_stack='"Newsreader", Georgia, serif',
        mono_stack='"IBM Plex Mono", ui-monospace, monospace',
        display_weight=600,
        tracking=-0.01,
        scale_ratio=1.25,
    ),
    TypePairing(
        key="space-grotesk-plex",
        name="Space Grotesk / IBM Plex Sans",
        character="Technical and precise; built for data-dense products.",
        display_stack='"Space Grotesk", "Archivo", system-ui, sans-serif',
        body_stack='"IBM Plex Sans", system-ui, sans-serif',
        mono_stack='"IBM Plex Mono", ui-monospace, monospace',
        display_weight=700,
        tracking=-0.02,
        scale_ratio=1.25,
    ),
    TypePairing(
        key="playfair-lato",
        name="Playfair Display / Lato",
        character="Classic editorial contrast: high-contrast serif, warm humanist body.",
        display_stack='"Playfair Display", Georgia, serif',
        body_stack='"Lato", system-ui, sans-serif',
        mono_stack='"SF Mono", ui-monospace, monospace',
        display_weight=700,
        tracking=-0.01,
        scale_ratio=1.333,
    ),
    TypePairing(
        key="sora-manrope",
        name="Sora / Manrope",
        character="Modern geometric; friendly without going round.",
        display_stack='"Sora", system-ui, sans-serif',
        body_stack='"Manrope", system-ui, sans-serif',
        mono_stack='"JetBrains Mono", ui-monospace, monospace',
        display_weight=700,
        tracking=-0.02,
        scale_ratio=1.25,
    ),
    TypePairing(
        key="dm-serif-dm-sans",
        name="DM Serif Display / DM Sans",
        character="Warm editorial; a serif with soft shoulders and a clean sans body.",
        display_stack='"DM Serif Display", Georgia, serif',
        body_stack='"DM Sans", system-ui, sans-serif',
        mono_stack='"Space Mono", ui-monospace, monospace',
        display_weight=600,
        tracking=-0.01,
        scale_ratio=1.25,
    ),
    TypePairing(
        key="nunito-quicksand",
        name="Nunito / Quicksand",
        character="Rounded and joyful; the playful end of the catalog.",
        display_stack='"Nunito", ui-rounded, "SF Pro Rounded", system-ui, sans-serif',
        body_stack='"Quicksand", ui-rounded, system-ui, sans-serif',
        mono_stack='"JetBrains Mono", ui-monospace, monospace',
        display_weight=800,
        tracking=-0.015,
        scale_ratio=1.2,
    ),
    TypePairing(
        key="libre-opensans",
        name="Libre Baskerville / Open Sans",
        character="Bookish and trustworthy; long-form reading first.",
        display_stack='"Libre Baskerville", Georgia, serif',
        body_stack='"Open Sans", system-ui, sans-serif',
        mono_stack='"IBM Plex Mono", ui-monospace, monospace',
        display_weight=700,
        tracking=-0.005,
        scale_ratio=1.2,
    ),
    TypePairing(
        key="bebas-roboto",
        name="Bebas Neue / Roboto",
        character="Condensed poster display; loud headlines, quiet body.",
        display_stack='"Bebas Neue", "Archivo Narrow", sans-serif',
        body_stack='"Roboto", system-ui, sans-serif',
        mono_stack='"Roboto Mono", ui-monospace, monospace',
        display_weight=400,
        tracking=0.02,
        scale_ratio=1.333,
    ),
    TypePairing(
        key="merriweather-source",
        name="Merriweather / Source Sans 3",
        character="Screen-first serif for reading, clean sans for UI.",
        display_stack='"Merriweather", Georgia, serif',
        body_stack='"Source Sans 3", system-ui, sans-serif',
        mono_stack='"Source Code Pro", ui-monospace, monospace',
        display_weight=700,
        tracking=-0.01,
        scale_ratio=1.2,
    ),
)

# Vibe keywords that narrow the pairing pool (a steer, not a fixed answer:
# the seed still picks within the pool, and the pool is never a single pairing).
_PAIRING_STEERS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "playful",
        ("playful", "fun", "kid", "joy", "whimsical", "cute"),
        ("nunito-quicksand", "sora-manrope"),
    ),
    (
        "elegant",
        ("elegant", "luxury", "premium", "refined", "sophisticated", "serif"),
        ("fraunces-newsreader", "playfair-lato", "libre-opensans", "dm-serif-dm-sans"),
    ),
    (
        "technical",
        ("technical", "developer", "data", "engineer", "api", "devops"),
        ("space-grotesk-plex", "merriweather-source", "archivo-inter"),
    ),
    (
        "bold",
        ("bold", "loud", "energetic", "punchy", "poster"),
        ("bebas-roboto", "archivo-inter", "space-grotesk-plex"),
    ),
)


def _pairing_pool(keywords: str) -> tuple[TypePairing, ...]:
    lowered = keywords.strip().lower()
    for _name, words, keys in _PAIRING_STEERS:
        if any(word in lowered for word in words):
            by_key = {pairing.key: pairing for pairing in TYPE_PAIRINGS}
            return tuple(by_key[key] for key in keys)
    return TYPE_PAIRINGS


# ---------------------------------------------------------------------------
# Layout profiles (design-layout)
# ---------------------------------------------------------------------------

_BASE_LADDER: tuple[int, ...] = (2, 4, 8, 12, 16, 24, 32, 48, 64, 96)
_LADDER_KEYS: tuple[str, ...] = (
    "3xs",
    "2xs",
    "xs",
    "sm",
    "md",
    "lg",
    "xl",
    "2xl",
    "3xl",
    "4xl",
)


@dataclass(frozen=True)
class LayoutProfile:
    """Density + radius character + shadow language, chosen once per system.

    `spacing_factor` scales the spacing ladder; `radius_base` is the "md"
    radius the rest derive from (same formula as the showcase's `_radii`).
    """

    key: str
    name: str
    density: str  # "tight" | "balanced" | "generous" — also the showcase spacing style
    spacing_factor: float
    radius_base: int
    radius_full: bool  # whether pill shapes are on-brand
    shadow_style: str  # "flat" | "soft" | "dramatic"
    description: str

    @property
    def spacing(self) -> dict[str, int]:
        """The 3xs..4xl spacing ladder, scaled by the density factor."""
        return {
            key: max(2, round(value * self.spacing_factor))
            for key, value in zip(_LADDER_KEYS, _BASE_LADDER, strict=True)
        }

    @property
    def radii(self) -> dict[str, int | str]:
        base = self.radius_base
        radii: dict[str, int | str] = {
            "none": 0,
            "xs": max(2, base // 4),
            "sm": max(4, base // 2),
            "md": base,
            "lg": base * 2,
            "xl": base * 3,
        }
        radii["full"] = 9999 if self.radius_full else base * 2
        return radii


LAYOUT_PROFILES: tuple[LayoutProfile, ...] = (
    LayoutProfile(
        key="compact-sharp",
        name="Compact / sharp",
        density="tight",
        spacing_factor=0.8,
        radius_base=4,
        radius_full=False,
        shadow_style="dramatic",
        description="Dense and crisp; sharp corners, deep shadows where they count.",
    ),
    LayoutProfile(
        key="balanced-soft",
        name="Balanced / soft",
        density="balanced",
        spacing_factor=1.0,
        radius_base=10,
        radius_full=True,
        shadow_style="soft",
        description="The middle of the road, done deliberately: even rhythm, soft elevation.",
    ),
    LayoutProfile(
        key="airy-rounded",
        name="Airy / rounded",
        density="generous",
        spacing_factor=1.3,
        radius_base=16,
        radius_full=True,
        shadow_style="soft",
        description="Generous whitespace and round corners; the editorial end of the range.",
    ),
    LayoutProfile(
        key="dense-square",
        name="Dense / square",
        density="tight",
        spacing_factor=0.75,
        radius_base=0,
        radius_full=False,
        shadow_style="flat",
        description="No rounding, no shadows — borders do the work. Formal and data-dense.",
    ),
    LayoutProfile(
        key="roomy-pill",
        name="Roomy / pill",
        density="generous",
        spacing_factor=1.25,
        radius_base=12,
        radius_full=True,
        shadow_style="soft",
        description="Roomy spacing with pill shapes; friendly without going bouncy.",
    ),
    LayoutProfile(
        key="tight-crisp",
        name="Tight / crisp",
        density="balanced",
        spacing_factor=0.85,
        radius_base=6,
        radius_full=False,
        shadow_style="dramatic",
        description="Tight but not dense; small radii, strong shadows, high contrast.",
    ),
)


# ---------------------------------------------------------------------------
# Motion profiles (design-motion)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MotionProfile:
    """One temperament: durations on Atelier's weight scale (micro 100-150,
    small 150-250, medium 250-400, large 400-600ms), exits ~20% faster than
    entrances, and a stagger for sequenced motion.
    """

    key: str
    name: str
    temperament: str
    durations: dict[str, str]  # instant/fast/normal/slow/slower, "Nms"
    ease_standard: str
    ease_out: str
    ease_in_out: str
    stagger_ms: int
    description: str

    @property
    def tokens(self) -> dict[str, str]:
        """The motion token dict the rich tokens carry. `duration_stagger`
        (not `stagger`) so the detail page's duration list shows it, and
        `ease_spring` stays present like the vibe default's."""
        return {
            "duration_instant": self.durations["instant"],
            "duration_fast": self.durations["fast"],
            "duration_normal": self.durations["normal"],
            "duration_slow": self.durations["slow"],
            "duration_slower": self.durations["slower"],
            "duration_stagger": f"{self.stagger_ms}ms",
            "ease_standard": self.ease_standard,
            "ease_out": self.ease_out,
            "ease_in_out": self.ease_in_out,
            "ease_spring": "cubic-bezier(0.34, 1.56, 0.64, 1)",
        }


MOTION_PROFILES: tuple[MotionProfile, ...] = (
    MotionProfile(
        key="snappy",
        name="Snappy",
        temperament="quick and decisive",
        durations={
            "instant": "90ms",
            "fast": "120ms",
            "normal": "200ms",
            "slow": "320ms",
            "slower": "500ms",
        },
        ease_standard="cubic-bezier(0.16, 1, 0.3, 1)",
        ease_out="cubic-bezier(0.16, 1, 0.3, 1)",
        ease_in_out="cubic-bezier(0.65, 0, 0.35, 1)",
        stagger_ms=24,
        description="Quick and decisive; feedback lands before the user notices the wait.",
    ),
    MotionProfile(
        key="smooth",
        name="Smooth",
        temperament="even and unhurried",
        durations={
            "instant": "90ms",
            "fast": "160ms",
            "normal": "260ms",
            "slow": "420ms",
            "slower": "700ms",
        },
        ease_standard="cubic-bezier(0.65, 0, 0.35, 1)",
        ease_out="cubic-bezier(0.16, 1, 0.3, 1)",
        ease_in_out="cubic-bezier(0.65, 0, 0.35, 1)",
        stagger_ms=40,
        description="Even and unhurried; the default when nothing needs to shout.",
    ),
    MotionProfile(
        key="springy",
        name="Springy",
        temperament="bouncy and alive",
        durations={
            "instant": "90ms",
            "fast": "140ms",
            "normal": "240ms",
            "slow": "380ms",
            "slower": "600ms",
        },
        ease_standard="cubic-bezier(0.34, 1.56, 0.64, 1)",
        ease_out="cubic-bezier(0.34, 1.56, 0.64, 1)",
        ease_in_out="cubic-bezier(0.65, 0, 0.35, 1)",
        stagger_ms=32,
        description="Bouncy and alive; overshoot on entrances, never on exits.",
    ),
    MotionProfile(
        key="stately",
        name="Stately",
        temperament="slow and deliberate",
        durations={
            "instant": "100ms",
            "fast": "200ms",
            "normal": "340ms",
            "slow": "560ms",
            "slower": "700ms",
        },
        ease_standard="cubic-bezier(0.65, 0, 0.35, 1)",
        ease_out="cubic-bezier(0.16, 1, 0.3, 1)",
        ease_in_out="cubic-bezier(0.65, 0, 0.35, 1)",
        stagger_ms=60,
        description="Slow and deliberate; motion that asks to be watched.",
    ),
    MotionProfile(
        key="gentle",
        name="Gentle",
        temperament="soft and calm",
        durations={
            "instant": "100ms",
            "fast": "180ms",
            "normal": "300ms",
            "slow": "480ms",
            "slower": "700ms",
        },
        ease_standard="cubic-bezier(0.16, 1, 0.3, 1)",
        ease_out="cubic-bezier(0.16, 1, 0.3, 1)",
        ease_in_out="cubic-bezier(0.65, 0, 0.35, 1)",
        stagger_ms=48,
        description="Soft and calm; nothing startles, everything settles.",
    ),
    MotionProfile(
        key="brisk",
        name="Brisk",
        temperament="fast and sharp",
        durations={
            "instant": "80ms",
            "fast": "100ms",
            "normal": "180ms",
            "slow": "280ms",
            "slower": "420ms",
        },
        ease_standard="cubic-bezier(0.85, 0, 0.15, 1)",
        ease_out="cubic-bezier(0.16, 1, 0.3, 1)",
        ease_in_out="cubic-bezier(0.65, 0, 0.35, 1)",
        stagger_ms=16,
        description="Fast and sharp; the snappiest end of the scale, for tools that keep up.",
    ),
)


# ---------------------------------------------------------------------------
# Component variants (design-layout: which components carry the system)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComponentVariant:
    """One emphasis for an app type: a genuinely different component set,
    not a reorder. The markdown's Components section renders the chosen
    variant, so two systems from the same brief do not ship the same kit.
    """

    key: str
    name: str
    emphasis: str
    components: tuple[tuple[str, str], ...]  # (name, guidance)


COMPONENT_VARIANTS: dict[str, tuple[ComponentVariant, ...]] = {
    "website": (
        ComponentVariant(
            key="hero-led",
            name="Hero-led",
            emphasis="One headline, one CTA, generous whitespace.",
            components=(
                ("Hero", "Display type, one headline, one primary CTA, generous whitespace."),
                ("Nav", "Sticky top nav; six items max; hamburger below 768px."),
                (
                    "Sections",
                    "Alternating rhythm, 64-128px vertical spacing; one idea per section.",
                ),
                ("Footer", "Sitemap columns, contact, legal; muted text on the surface color."),
                (
                    "CTA",
                    "Repeat the primary CTA after each major section; keep the label identical.",
                ),
            ),
        ),
        ComponentVariant(
            key="story-led",
            name="Story-led",
            emphasis="The page reads like a story: chapters, pull quotes, a timeline.",
            components=(
                (
                    "Narrative hero",
                    "A statement, not a pitch: display type over a quiet field of color.",
                ),
                (
                    "Chapter sections",
                    "Numbered chapters with generous leading; each ends on a beat, not a button.",
                ),
                (
                    "Pull quotes",
                    "Oversized serif quotes breaking the column; one per chapter at most.",
                ),
                ("Timeline", "The product's story as a vertical timeline; dates in the mono face."),
                ("Footer", "Sitemap columns, contact, legal; muted text on the surface color."),
            ),
        ),
        ComponentVariant(
            key="conversion-led",
            name="Conversion-led",
            emphasis="Every section earns the next click; proof before the ask.",
            components=(
                (
                    "Hero with form",
                    "Headline, one-line proof, and the signup form above the fold.",
                ),
                (
                    "Social proof",
                    "Logos and one-line quotes; proof before the feature list, never after.",
                ),
                (
                    "Pricing teaser",
                    "Three tiers as a summary, not the full table; the CTA is 'see full pricing'.",
                ),
                (
                    "FAQ",
                    "The five objections, answered in two lines each; the last ends on a CTA.",
                ),
                (
                    "Sticky CTA",
                    "A slim bar that appears after the first section; same label as the hero CTA.",
                ),
            ),
        ),
    ),
    "mobile": (
        ComponentVariant(
            key="tab-led",
            name="Tab-led",
            emphasis="Five destinations, thumb reach, sheets for the rest.",
            components=(
                (
                    "Tab bar",
                    "Bottom tab bar, five destinations max, 44pt touch targets, safe-area padding.",
                ),
                (
                    "Cards",
                    "One idea per card, 16pt radius, generous padding, actions in thumb reach.",
                ),
                ("Sheets", "Bottom sheets for secondary flows; drag handle; scrim dismisses."),
                (
                    "Touch buttons",
                    "Minimum 44x44pt hit area; primary button full-width on small screens.",
                ),
            ),
        ),
        ComponentVariant(
            key="feed-led",
            name="Feed-led",
            emphasis="A scrolling feed is the home screen; everything else is one tap away.",
            components=(
                (
                    "Feed list",
                    "Chronological cards with a clear newest-first order; pull to refresh.",
                ),
                (
                    "Card detail",
                    "Full-screen detail with a collapsing header; actions pinned to the bottom.",
                ),
                (
                    "Composer",
                    "One-tap compose from the feed; drafts autosave; keyboard never covers Send.",
                ),
                (
                    "Bottom nav",
                    "Three destinations plus compose; badges only for things needing action.",
                ),
            ),
        ),
        ComponentVariant(
            key="task-led",
            name="Task-led",
            emphasis="The home screen is a list of things to do, with progress visible.",
            components=(
                (
                    "Home dashboard",
                    "Today's tasks first, progress ring at the top; nothing else competes.",
                ),
                (
                    "Task list",
                    "Swipe to complete; overdue items surface with the danger color.",
                ),
                (
                    "Quick add",
                    "A floating action that opens a one-field add; the keyboard opens with it.",
                ),
                (
                    "Progress ring",
                    "The day's completion as a ring, not a bar; the number sits in the middle.",
                ),
            ),
        ),
    ),
    "dashboard": (
        ComponentVariant(
            key="table-led",
            name="Table-led",
            emphasis="Dense rows, sticky headers, filters above everything.",
            components=(
                (
                    "Data table",
                    "Dense rows, sticky header, tabular numerals, row hover, bulk actions.",
                ),
                (
                    "Charts",
                    "Use the 5-color categorical ramp; direct labels over legends; dark-first.",
                ),
                ("Filters", "Persistent filter bar above tables; saved views; clear-all control."),
                ("Sidebar", "Collapsible nav rail; sections per primary entity; command-K hint."),
            ),
        ),
        ComponentVariant(
            key="chart-led",
            name="Chart-led",
            emphasis="KPIs first, charts second, tables only for drill-down.",
            components=(
                (
                    "KPI strip",
                    "Four KPIs with sparklines; deltas in the mono face, tabular numerals.",
                ),
                (
                    "Charts",
                    "One chart per question; the 5-color ramp; direct labels, not legends.",
                ),
                (
                    "Drill-down table",
                    "Clicking a chart filters the table below it; the filter is always visible.",
                ),
                (
                    "Date picker",
                    "One range control drives every chart; presets over custom ranges.",
                ),
            ),
        ),
        ComponentVariant(
            key="command-led",
            name="Command-led",
            emphasis="Keyboard-first: command-K is the primary navigation.",
            components=(
                (
                    "Command palette",
                    "Command-K opens a palette over everything; fuzzy search, recent items first.",
                ),
                (
                    "Sidebar",
                    "Collapsible nav rail; sections per entity; the palette hint on every item.",
                ),
                (
                    "Data table",
                    "Dense rows, sticky header, tabular numerals; every row action in the palette.",
                ),
                (
                    "Saved views",
                    "Named views in the sidebar; the palette lists them before entities.",
                ),
            ),
        ),
    ),
    "pwa": (
        ComponentVariant(
            key="shell-led",
            name="Shell-led",
            emphasis="The shell paints from cache; install and offline are first-class.",
            components=(
                (
                    "App shell",
                    "Persistent header and bottom nav; the shell renders instantly from cache.",
                ),
                (
                    "Install prompt",
                    "Custom prompt after the second visit; explain the value before asking.",
                ),
                (
                    "Offline banner",
                    "Warning-color banner when offline; queued actions sync on reconnect.",
                ),
            ),
        ),
        ComponentVariant(
            key="content-led",
            name="Content-led",
            emphasis="Reading first; the shell gets out of the way.",
            components=(
                (
                    "App shell",
                    "Minimal header, no bottom nav while reading; the shell renders instantly.",
                ),
                (
                    "Article reader",
                    "Measure-capped body copy, generous leading; progress in the primary color.",
                ),
                (
                    "Offline banner",
                    "Warning-color banner when offline; saved articles stay readable.",
                ),
                (
                    "Share sheet",
                    "Native share first, then the in-app sheet; it never covers the article end.",
                ),
            ),
        ),
        ComponentVariant(
            key="commerce-led",
            name="Commerce-led",
            emphasis="Browse, cart, checkout — all offline-tolerant.",
            components=(
                (
                    "App shell",
                    "Persistent header with cart count; the shell renders instantly from cache.",
                ),
                (
                    "Product grid",
                    "Two-column grid, one product per card; price in tabular numerals.",
                ),
                (
                    "Cart drawer",
                    "Slides in from the right; totals update without a round trip; works offline.",
                ),
                (
                    "Offline banner",
                    "Warning-color banner when offline; the cart queues and syncs on reconnect.",
                ),
            ),
        ),
    ),
}


# ---------------------------------------------------------------------------
# Palette construction + the contrast gate (design-accessibility)
# ---------------------------------------------------------------------------


def build_palette(
    strategy: ColorStrategy,
    rng: random.Random,
    anchor_hue: float,
) -> tuple[dict[str, str], float, float, float]:
    """A full light-mode palette from a strategy, a seeded RNG, and an anchor.

    Returns (palette, primary hue, saturation, lightness). The construction
    mirrors the design kit's `_palette_from_hue` (same neutral/text roles, so
    the contrast properties carry over); the strategy varies the primary hue,
    saturation, lightness, and the accent hues.
    """
    rotation = rng.uniform(*strategy.rotation)
    if strategy.key == "hue-shift" and rng.random() < 0.5:
        rotation = -rotation
    hue = (anchor_hue + rotation + rng.uniform(-12.0, 12.0)) % 360.0
    saturation = rng.uniform(*strategy.saturation)
    lightness = rng.uniform(*strategy.lightness)
    primary = _hsl(hue, saturation, lightness)
    primary_hover = _hsl(hue, saturation, max(0.30, lightness - 0.08))
    accents = [
        _hsl((hue + offset) % 360.0, saturation, lightness) for offset in strategy.accent_offsets
    ]
    palette: dict[str, str] = {
        "primary": primary,
        "primary_hover": primary_hover,
        "primary_text": _on_color(primary),
        "background": "#FFFFFF",
        "surface": _hsl(hue, 0.35, 0.96),
        "text": _hsl(hue, 0.30, 0.10),
        "text_muted": _hsl(hue, 0.15, 0.44),
        "border": _hsl(hue, 0.20, 0.88),
        "success": "#16A34A",
        "warning": "#D97706",
        "danger": "#DC2626",
    }
    if accents:
        palette["accent"] = accents[0]
        palette["accent_hover"] = _hsl(
            (hue + strategy.accent_offsets[0]) % 360.0, saturation, max(0.30, lightness - 0.08)
        )
        if len(accents) > 1:
            palette["accent_secondary"] = accents[1]
    return palette, hue, saturation, lightness


def check_palette_contrast(palette: dict[str, str]) -> list[str]:
    """WCAG AA violations in a light-mode palette (empty means it passes).

    The gate, not a variable: body text ≥ 4.5:1 on background and surface,
    muted text ≥ 4.5:1 on background, button text ≥ 3:1 on both primary
    states. Dark variants are derived from the light palette by the design
    kit's `_dark_variant`, unchanged by this module.
    """
    violations: list[str] = []
    background = palette.get("background", "#FFFFFF")
    surface = palette.get("surface", "#FFFFFF")
    for role in ("text", "text_muted"):
        color = palette.get(role)
        if color is None:
            continue
        if contrast_ratio(color, background) < 4.5:
            violations.append(f"{role} on background is below 4.5:1")
        if contrast_ratio(color, surface) < 4.5:
            violations.append(f"{role} on surface is below 4.5:1")
    primary_text = palette.get("primary_text")
    for state in ("primary", "primary_hover"):
        color = palette.get(state)
        if (
            primary_text is not None
            and color is not None
            and contrast_ratio(primary_text, color) < 3.0
        ):
            violations.append(f"primary_text on {state} is below 3:1")
    return violations


def _ensure_contrast(palette: dict[str, str], hue: float) -> dict[str, str]:
    """Nudge a palette until the contrast gate passes (deterministic).

    Darkens muted text until it clears 4.5:1; if button text still falls
    short on a primary state, the primary lightness is walked toward the
    nearer extreme until the best available text color clears 3:1. The
    construction already keeps these passing for the sampled ranges — this
    is the gate's enforcement, not its hope.
    """
    fixed = dict(palette)
    for _ in range(12):
        violations = check_palette_contrast(fixed)
        if not violations:
            return fixed
        muted = fixed.get("text_muted")
        if muted is not None and any("text_muted" in v for v in violations):
            _r, _g, _b = _hex_to_rgb(muted)
            _h, lightness, _s = colorsys.rgb_to_hls(_r, _g, _b)
            fixed["text_muted"] = _hsl(hue, 0.15, max(0.10, lightness - 0.04))
            continue
        if any("primary_text" in v for v in violations):
            primary = fixed["primary"]
            _r, _g, _b = _hex_to_rgb(primary)
            _h, lightness, sat = colorsys.rgb_to_hls(_r, _g, _b)
            # Walk lightness toward the nearer extreme: a lighter primary
            # reads dark text better, a darker one reads white text better.
            step = 0.05 if lightness >= 0.5 else -0.05
            lightness = min(0.72, max(0.28, lightness + step))
            fixed["primary"] = _hsl(hue, sat, lightness)
            fixed["primary_hover"] = _hsl(hue, sat, max(0.28, lightness - 0.08))
            fixed["primary_text"] = _on_color(fixed["primary"])
            continue
        break
    return fixed


# ---------------------------------------------------------------------------
# Seeded generation + fingerprint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeededSystem:
    """Everything one seeded generation decided, so both call sites (CLI and
    dashboard) share one path and the choices can be stored in `meta.json`.
    """

    seed: int
    app_type: str
    keywords: str
    palette: dict[str, str]
    hue: float
    saturation: float
    lightness: float
    color_strategy: ColorStrategy
    type_pairing: TypePairing
    layout: LayoutProfile
    motion: MotionProfile
    components: ComponentVariant
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fingerprint", fingerprint_of(self.choices))

    @property
    def choices(self) -> dict[str, str]:
        """The chosen skill keys, stored in `meta.json` next to the seed."""
        return {
            "color_strategy": self.color_strategy.key,
            "type_pairing": self.type_pairing.key,
            "layout": self.layout.key,
            "motion": self.motion.key,
            "components": self.components.key,
            "primary": self.palette["primary"],
        }

    @property
    def typography_tokens(self) -> dict[str, object]:
        """The pairing as flat tokens (merged into `tokens.json`)."""
        return {
            "font_display": self.type_pairing.display_stack,
            "font_body": self.type_pairing.body_stack,
            "font_mono": self.type_pairing.mono_stack,
            "display_weight": self.type_pairing.display_weight,
            "tracking": self.type_pairing.tracking,
            "scale_ratio": self.type_pairing.scale_ratio,
        }


def fingerprint_of(choices: dict[str, str]) -> str:
    """A fingerprint of the key axes: primary hex + type pairing + layout +
    motion + component set. Two systems collide only when every axis agrees.
    """
    axes = "|".join(
        [
            str(choices.get("primary", "")).lower(),
            str(choices.get("type_pairing", "")),
            str(choices.get("layout", "")),
            str(choices.get("motion", "")),
            str(choices.get("components", "")),
        ]
    )
    return hashlib.sha256(axes.encode("utf-8")).hexdigest()


def new_seed() -> int:
    """A fresh per-system seed from the OS RNG (creation time, not input)."""
    return random.SystemRandom().getrandbits(63)


def generate_seeded_system(
    seed: int,
    *,
    keywords: str = "",
    app_type: str = "website",
) -> SeededSystem:
    """Generate one system's seeded choices, deterministically per seed.

    Same seed + same inputs -> identical output (reproducible). Different
    seed -> different palette, pairing, layout, motion, and components, even
    for identical keywords. Unknown app types fall back to the website
    component variants.
    """
    rng = random.Random(seed)
    anchor = steer_hue(keywords)
    strategy = rng.choice(COLOR_STRATEGIES)
    palette, hue, saturation, lightness = build_palette(strategy, rng, anchor)
    palette = _ensure_contrast(palette, hue)
    pairing = rng.choice(_pairing_pool(keywords))
    layout = rng.choice(LAYOUT_PROFILES)
    motion = rng.choice(MOTION_PROFILES)
    variants = COMPONENT_VARIANTS.get(app_type, COMPONENT_VARIANTS["website"])
    components = rng.choice(variants)
    return SeededSystem(
        seed=seed,
        app_type=app_type,
        keywords=keywords,
        palette=palette,
        hue=hue,
        saturation=saturation,
        lightness=lightness,
        color_strategy=strategy,
        type_pairing=pairing,
        layout=layout,
        motion=motion,
        components=components,
    )


def generate_unique_system(
    seed: int,
    *,
    existing_fingerprints: set[str] | frozenset[str] = frozenset(),
    keywords: str = "",
    app_type: str = "website",
    max_attempts: int = MAX_REROLLS,
) -> tuple[SeededSystem, int]:
    """Generate a system whose fingerprint collides with nothing in the
    library, re-rolling the seed (seed+1, seed+2, …) on collision.

    Returns (system, attempts used). Raises ValueError when every attempt
    collides — with a 256-bit fingerprint space that means the library
    already holds every reachable system, not a routine outcome.
    """
    for attempt in range(max_attempts):
        system = generate_seeded_system(seed + attempt, keywords=keywords, app_type=app_type)
        if system.fingerprint not in existing_fingerprints:
            return system, attempt + 1
    raise ValueError(
        f"could not generate a unique design system in {max_attempts} attempts "
        "(every re-rolled seed collided with an existing system)"
    )


def seeded_vibe_profile(vibe_profile: VibeProfile, seeded: SeededSystem) -> VibeProfile:
    """The vibe profile with the seeded choices applied: hue/saturation,
    type pairing, layout radii/shadows/spacing, and motion ease. The
    showcase renderer reads the profile for its hero treatment, so passing
    this (not the base profile) is what makes the showcase match the tokens.
    """
    from dataclasses import replace

    return replace(
        vibe_profile,
        hue=seeded.hue,
        saturation=seeded.saturation,
        display_stack=seeded.type_pairing.display_stack,
        body_stack=seeded.type_pairing.body_stack,
        mono_stack=seeded.type_pairing.mono_stack,
        display_weight=seeded.type_pairing.display_weight,
        tight_tracking=seeded.type_pairing.tracking,
        radius_base=seeded.layout.radius_base,
        radius_full=seeded.layout.radius_full,
        shadow_style=seeded.layout.shadow_style,
        spacing_style=seeded.layout.density,
        ease=seeded.motion.ease_standard,
    )


def rich_tokens_for_seeded(
    vibe_profile: VibeProfile,
    seeded: SeededSystem,
    theme: str = "both",
) -> dict[str, object]:
    """Rich tokens for a seeded system: the vibe profile with the seeded
    hue/saturation, type pairing, layout, and motion applied, so the stored
    `design_tokens` and the showcase render the same choices the flat tokens
    carry. The rich primary is overridden with the seeded palette's exact
    primary so the detail page and the exports never disagree.
    """
    from jev_agent.core.design_showcase import rich_tokens

    profile = seeded_vibe_profile(vibe_profile, seeded)
    rich = rich_tokens(
        profile,
        theme,
        spacing=seeded.layout.spacing,
        radii=seeded.layout.radii,
        shadow_style=seeded.layout.shadow_style,
        motion=seeded.motion.tokens,
        typography={
            "font_display": seeded.type_pairing.display_stack,
            "font_body": seeded.type_pairing.body_stack,
            "font_mono": seeded.type_pairing.mono_stack,
            "display_weight": seeded.type_pairing.display_weight,
            "tracking": seeded.type_pairing.tracking,
        },
    )
    # Override the rich primary with the seeded palette's exact primary, so
    # the detail page's Colors section and the flat tokens agree. Only the
    # primary roles are overridden — the dark palette keeps its own
    # backgrounds, surfaces, and text.
    dark_primary = _hsl(seeded.hue, min(1.0, seeded.saturation + 0.1), 0.62)
    dark_hover = _hsl(seeded.hue, min(1.0, seeded.saturation + 0.1), 0.70)
    dark_overrides = {
        "primary": dark_primary,
        "primary_hover": dark_hover,
        "primary_text": _on_color(dark_primary),
    }
    colors = rich.get("colors")
    if isinstance(colors, dict):
        if theme == "dark":
            colors.update(dark_overrides)
        elif theme == "light":
            colors.update(seeded.palette)
        else:
            light_section = colors.get("light")
            dark_section = colors.get("dark")
            if isinstance(light_section, dict):
                light_section.update(seeded.palette)
            if isinstance(dark_section, dict):
                dark_section.update(dark_overrides)
    return rich
