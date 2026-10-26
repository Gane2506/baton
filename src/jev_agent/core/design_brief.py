"""Brief compiler for the Design Studio (issue #300).

The research report's thesis (``research_notes/design-best-practices-20261026-0604``,
§10) is that generic AI UI is a missing-input failure: the workflow must
decide the design *before* building, never infer. This module is that
decision step. `compile_brief` turns a `BusinessContext` + vibe + app type
into a `DesignBrief`: concrete decisions on seven continuous axes (warmth,
contrast, density, geometry, formality, motion, type personality), an anchor
hue and OKLCH palette ramp, and named choices for harmony, type, layout and
motion — every value derived deterministically from the industry design
language (`design_business.INDUSTRY_LANGUAGES`), nudged by tone / audience /
description / vibe keywords, and finally overridden by explicit `overrides`.

The brief is the sole input the generator needs: `brief.generation_inputs()`
returns the `{seed, keywords, app_type, overrides}` the skills module's
`generate_unique_system` consumes (this module never imports
`design_skills` — the two are parallel workstreams; the key sets below
mirror its public API and must stay in agreement with it).

Also here: pure-Python OKLCH → sRGB conversion (report §2.2 — ramps built
in OKLCH so equal lightness steps *look* equal), the DNA fingerprint
(`brief_fingerprint`, a superset of the skills fingerprint — it also covers
scene and geometry, which the skills fingerprint lacks), and
`ensure_unique_brief`, which re-rolls the seed on a fingerprint collision.

Pure functions, stdlib-only, like `design_kit.py`.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field

from jev_agent.core.design_business import (
    AXIS_KEYS,
    DEFAULT_LANGUAGE,
    INDUSTRY_LANGUAGES,
    SCENE_TYPES,
    BusinessContext,
    IndustryLanguage,
    match_profile,
)
from jev_agent.core.design_kit import APP_TYPES

MAX_BRIEF_REROLLS = 10

# ---------------------------------------------------------------------------
# Valid choice keys — mirrors of the skills module's public catalogs
# (design_skills.COLOR_STRATEGIES / TYPE_PAIRINGS / LAYOUT_PROFILES /
# MOTION_PROFILES). Kept as literals here so this module never imports that
# module; if the skills catalogs gain a key, add it here too.
# ---------------------------------------------------------------------------

HARMONY_KEYS: frozenset[str] = frozenset(
    (
        "analogous",
        "complementary",
        "split-complementary",
        "triadic",
        "monochromatic",
        "hue-shift",
    )
)

TYPE_PAIRING_KEYS: frozenset[str] = frozenset(
    (
        "archivo-inter",
        "fraunces-newsreader",
        "space-grotesk-plex",
        "playfair-lato",
        "sora-manrope",
        "dm-serif-dm-sans",
        "nunito-quicksand",
        "libre-opensans",
        "bebas-roboto",
        "merriweather-source",
    )
)

LAYOUT_KEYS: frozenset[str] = frozenset(
    (
        "compact-sharp",
        "balanced-soft",
        "airy-rounded",
        "dense-square",
        "roomy-pill",
        "tight-crisp",
    )
)

MOTION_KEYS: frozenset[str] = frozenset(
    ("snappy", "smooth", "springy", "stately", "gentle", "brisk")
)

SCENE_KEY_SET: frozenset[str] = frozenset(SCENE_TYPES)

# Layout patterns — the per-app-type component emphasis, named like the
# skills module's COMPONENT_VARIANTS keys so a brief maps onto them.
LAYOUT_PATTERNS: frozenset[str] = frozenset(
    (
        "hero-led",
        "story-led",
        "conversion-led",
        "tab-led",
        "feed-led",
        "task-led",
        "table-led",
        "chart-led",
        "command-led",
        "shell-led",
        "content-led",
        "commerce-led",
    )
)


# ---------------------------------------------------------------------------
# OKLCH (report §2.2)
# ---------------------------------------------------------------------------
# Björn Ottosson's OKLCH → linear sRGB matrices. No new dependency: the
# conversion is ~15 lines of stdlib math. Out-of-gamut chroma (OKLCH's
# gamut exceeds sRGB, so a vivid hue at extreme lightness can fall outside
# it) is clamped to the sRGB cube on export — the ramp builder keeps chroma
# moderate, so clamping is a rare edge, never the norm.

RAMP_STEPS = 11
RAMP_LIGHTNESS_MIN = 0.15
RAMP_LIGHTNESS_MAX = 0.92


def oklch_to_linear_rgb(lightness: float, chroma: float, hue: float) -> tuple[float, float, float]:
    """OKLCH to linear sRGB floats (unclamped — may fall outside 0..1 when
    the colour is outside the sRGB gamut)."""
    angle = math.radians(hue)
    a = chroma * math.cos(angle)
    b = chroma * math.sin(angle)
    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.2914855480 * b
    l_cubed, m_cubed, s_cubed = l_**3, m_**3, s_**3
    return (
        +4.0767416621 * l_cubed - 3.3077115913 * m_cubed + 0.2309699292 * s_cubed,
        -1.2684380046 * l_cubed + 2.6097574011 * m_cubed - 0.3413193965 * s_cubed,
        -0.0041960863 * l_cubed - 0.7034186147 * m_cubed + 1.7076147010 * s_cubed,
    )


def _linear_to_srgb(value: float) -> float:
    """One linear-sRGB channel to gamma-encoded sRGB, gamut-clamped."""
    clamped = max(0.0, min(1.0, value))
    if clamped <= 0.0031308:
        return 12.92 * clamped
    return 1.055 * math.pow(clamped, 1.0 / 2.4) - 0.055


def oklch_to_hex(lightness: float, chroma: float, hue: float) -> str:
    """OKLCH to `#RRGGBB`, gamut-clamped to sRGB on export."""
    r, g, b = oklch_to_linear_rgb(lightness, chroma, hue)
    red = round(_linear_to_srgb(r) * 255)
    green = round(_linear_to_srgb(g) * 255)
    blue = round(_linear_to_srgb(b) * 255)
    return f"#{red:02X}{green:02X}{blue:02X}"


def oklch_ramp(
    hue: float,
    chroma: float,
    steps: int = RAMP_STEPS,
    lightness_min: float = RAMP_LIGHTNESS_MIN,
    lightness_max: float = RAMP_LIGHTNESS_MAX,
) -> tuple[str, ...]:
    """A tonal ramp: `steps` hex colours stepping lightness evenly at fixed
    chroma/hue, from `lightness_min` to `lightness_max`. Because the steps
    are even in OKLCH lightness, consecutive steps *look* equally different
    (report §2.2) — the same lightness across hues reads as equal weight,
    which HSL ramps cannot promise.

    Raises ValueError when `steps` is outside 9..11, chroma is negative,
    or the lightness window is empty/inverted.
    """
    if not 9 <= steps <= 11:
        raise ValueError(f"ramp steps must be 9..11, got {steps}")
    if chroma < 0.0:
        raise ValueError(f"ramp chroma must be >= 0, got {chroma}")
    if not 0.0 <= lightness_min < lightness_max <= 1.0:
        raise ValueError(
            "ramp lightness window must satisfy "
            f"0 <= min < max <= 1, got {lightness_min}..{lightness_max}"
        )
    span = lightness_max - lightness_min
    return tuple(
        oklch_to_hex(lightness_min + span * index / (steps - 1), chroma, hue)
        for index in range(steps)
    )


def _ramp_index_for_lightness(lightness: float, steps: int = RAMP_STEPS) -> int:
    """The ramp index whose lightness is nearest `lightness`."""
    fraction = (lightness - RAMP_LIGHTNESS_MIN) / (RAMP_LIGHTNESS_MAX - RAMP_LIGHTNESS_MIN)
    return max(0, min(steps - 1, round(fraction * (steps - 1))))


# ---------------------------------------------------------------------------
# The avoid-list (report §10.3 — the recognisable default mode, banned)
# ---------------------------------------------------------------------------

BASE_AVOID: tuple[str, ...] = (
    "no purple gradients",
    "no three-column icon feature grid",
    "no cards for everything — vary the container per content",
    "no Inter as the display face",
    "no pure #000000 text on pure #FFFFFF",
    "no gradient text in the hero",
    "no identical rounded containers with no hierarchy",
    "no placeholder or stock copy in the hero",
)

# ---------------------------------------------------------------------------
# Axis nudges from free text (tone / audience / description / vibe)
# ---------------------------------------------------------------------------
# Each group applies at most once (any keyword present), as small deltas on
# the industry preset — a steer, never a rewrite. Substring matching, in the
# style of `design_business.match_profile`.

_TONE_NUDGES: tuple[tuple[tuple[str, ...], dict[str, float]], ...] = (
    (
        ("playful", "fun", "whimsical", "joyful"),
        {"motion": 0.15, "type_personality": 0.15, "formality": -0.15, "geometry": -0.10},
    ),
    (
        ("formal", "professional", "serious", "corporate"),
        {"formality": 0.15, "motion": -0.10, "type_personality": -0.10},
    ),
    (("warm", "friendly", "welcoming", "cozy"), {"warmth": 0.15}),
    (
        ("bold", "energetic", "punchy", "loud"),
        {"contrast": 0.10, "motion": 0.10, "type_personality": 0.10},
    ),
    (
        ("calm", "minimal", "quiet", "serene"),
        {"contrast": -0.10, "motion": -0.15, "density": -0.10},
    ),
    (
        ("luxury", "premium", "elegant", "refined"),
        {"formality": 0.15, "contrast": 0.05, "density": -0.10, "type_personality": 0.10},
    ),
    (("modern", "clean", "sleek"), {"geometry": 0.10, "density": -0.05}),
    (
        ("kid", "children", "family"),
        {"motion": 0.15, "type_personality": 0.15, "formality": -0.20},
    ),
    (
        ("enterprise", "executive", "investor"),
        {"formality": 0.10, "density": 0.10, "motion": -0.10},
    ),
    (("creative", "designer", "artist"), {"type_personality": 0.15, "warmth": 0.05}),
)

# App-type nudges: a dashboard is denser and calmer than a marketing site.
_APP_TYPE_NUDGES: dict[str, dict[str, float]] = {
    "dashboard": {"density": 0.10, "motion": -0.05},
    "mobile": {"density": -0.05, "motion": 0.05},
    "website": {"type_personality": 0.05},
    "pwa": {"density": -0.05},
}


def _clamp_axis(value: float) -> float:
    return max(0.0, min(1.0, value))


def _apply_text_nudges(axes: dict[str, float], text: str) -> None:
    lowered = text.lower()
    for keywords, deltas in _TONE_NUDGES:
        if any(word in lowered for word in keywords):
            for axis, delta in deltas.items():
                axes[axis] = _clamp_axis(axes[axis] + delta)


def _weighted_pick(rng: random.Random, pool: tuple[str, ...]) -> str:
    """A seeded pick from an ordered preference pool — earlier entries are
    more likely (weights len, len-1, …, 1), so the language's preferred
    pick wins most often but the seed can still surprise."""
    weights = [len(pool) - index for index in range(len(pool))]
    return rng.choices(pool, weights=weights, k=1)[0]


def _layout_pattern(app_type: str, axes: dict[str, float]) -> str:
    """The per-screen layout emphasis: one focal pattern per app type,
    split by the axes that decide it (report §10.4 rule 6 — one focal
    point per screen, layout varies per task)."""
    if app_type == "dashboard":
        return "table-led" if axes["density"] >= 0.6 else "chart-led"
    if app_type == "website":
        return "story-led" if axes["type_personality"] >= 0.6 else "hero-led"
    if app_type == "mobile":
        return "feed-led" if axes["density"] < 0.5 else "tab-led"
    return "shell-led"  # pwa


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------
# An override names any derived value and always wins over the language
# preset, the text nudges, and the seeded pick. Axis values are clamped to
# 0..1; choice keys must be valid or compile_brief raises ValueError.

_AXIS_OVERRIDES: frozenset[str] = frozenset(AXIS_KEYS)
_SCALAR_OVERRIDES: frozenset[str] = frozenset(("anchor_hue", "anchor_chroma", "primary_lightness"))
_KEY_OVERRIDES: dict[str, frozenset[str]] = {
    "harmony": HARMONY_KEYS,
    "type_pairing": TYPE_PAIRING_KEYS,
    "layout": LAYOUT_KEYS,
    # "motion" is already an axis name, so the motion-profile choice is
    # overridden as "motion_profile".
    "motion_profile": MOTION_KEYS,
    "scene": SCENE_KEY_SET,
    "layout_pattern": LAYOUT_PATTERNS,
}
_TEXT_OVERRIDES: frozenset[str] = frozenset(("focal_point",))
# List overrides: "avoid" replaces the derived avoid-list wholesale (the
# direction step's editable avoid-list, issue #301) — a list/tuple of
# non-empty strings, capped so a forged override cannot bloat the brief.
_LIST_OVERRIDES: frozenset[str] = frozenset(("avoid",))
_MAX_AVOID_ITEMS = 50
_MAX_AVOID_LEN = 200
OVERRIDABLE_KEYS: frozenset[str] = (
    _AXIS_OVERRIDES
    | _SCALAR_OVERRIDES
    | frozenset(_KEY_OVERRIDES)
    | _TEXT_OVERRIDES
    | _LIST_OVERRIDES
)


def _validate_overrides(overrides: Mapping[str, object]) -> dict[str, object]:
    """Unknown keys and invalid choice values raise; axis/scalar values are
    type-checked here and clamped at apply time."""
    unknown = sorted(set(overrides) - OVERRIDABLE_KEYS)
    if unknown:
        raise ValueError(f"unknown brief override(s): {', '.join(unknown)}")
    for key, value in overrides.items():
        if key in _AXIS_OVERRIDES or key in _SCALAR_OVERRIDES:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"brief override {key!r} must be a number, got {value!r}")
        elif key in _KEY_OVERRIDES:
            if not isinstance(value, str) or value not in _KEY_OVERRIDES[key]:
                allowed = ", ".join(sorted(_KEY_OVERRIDES[key]))
                raise ValueError(f"brief override {key!r} must be one of {allowed}, got {value!r}")
        elif key in _TEXT_OVERRIDES and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"brief override {key!r} must be a non-empty string")
        elif key in _LIST_OVERRIDES:
            if not isinstance(value, (list, tuple)) or len(value) > _MAX_AVOID_ITEMS:
                raise ValueError(
                    f"brief override {key!r} must be a list of at most "
                    f"{_MAX_AVOID_ITEMS} strings, got {value!r}"
                )
            for item in value:
                if not isinstance(item, str) or not item.strip() or len(item) > _MAX_AVOID_LEN:
                    raise ValueError(
                        f"brief override {key!r} items must be non-empty strings "
                        f"under {_MAX_AVOID_LEN} characters, got {item!r}"
                    )
    return dict(overrides)


def _derive_seed(
    business: BusinessContext,
    vibe: str,
    app_type: str,
    industry: str | None,
    overrides: Mapping[str, object],
) -> int:
    """The deterministic seed for these inputs: same brief in, same seed
    out, so compilation is reproducible. The seed — not the inputs — is
    what the RNG draws the within-pool picks from."""
    canonical = "|".join(
        [
            industry or "",
            vibe.strip().lower(),
            app_type,
            business.industry.strip().lower(),
            business.audience.strip().lower(),
            business.tone.strip().lower(),
            business.offering.strip().lower(),
            business.description.strip().lower(),
            "|".join(f"{key}={overrides[key]!r}" for key in sorted(overrides)),
        ]
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (2**63)


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DesignBrief:
    """Everything decided before generation: axes, palette (OKLCH terms),
    named choices, layout, scene, and the avoid-list.

    `business_context`, `vibe`, `app_type` and `overrides` are kept so
    `ensure_unique_brief` can re-compile the same brief under a new seed.
    `keywords` is the steer string for the skills module; `seed` is the
    deterministic seed those inputs produced.
    """

    business_context: BusinessContext
    vibe: str
    app_type: str
    industry: str | None
    axes: dict[str, float]
    anchor_hue: float
    anchor_chroma: float
    primary_lightness: float
    harmony: str
    type_pairing: str
    layout: str
    motion: str
    layout_pattern: str
    focal_point: str
    scene: str
    avoid: tuple[str, ...]
    palette_ramp: tuple[str, ...]
    seed: int
    keywords: str
    overrides: dict[str, object] = field(default_factory=dict)

    @property
    def primary_hex(self) -> str:
        """The ramp step nearest the primary lightness — the system's
        primary colour in hex."""
        return self.palette_ramp[_ramp_index_for_lightness(self.primary_lightness)]

    def generation_inputs(self) -> dict[str, object]:
        """The inputs for the skills module's `generate_unique_system`:
        `seed`, `keywords` and `app_type` pass straight through (the caller
        supplies `existing_fingerprints`); `overrides` rides along so the
        generation call site can record what the brief forced."""
        return {
            "seed": self.seed,
            "keywords": self.keywords,
            "app_type": self.app_type,
            "overrides": dict(self.overrides),
        }


def compile_brief(
    business_context: BusinessContext,
    vibe: str = "",
    app_type: str = "website",
    overrides: Mapping[str, object] | None = None,
    seed: int | None = None,
) -> DesignBrief:
    """Compile business context + vibe + app type into a `DesignBrief`.

    Derivation order (each step is deterministic for the inputs): industry
    language presets → tone/audience/description/vibe keyword nudges →
    app-type nudges → seeded picks (anchor hue in the language's range;
    harmony / type / layout / motion weighted by pool order) → explicit
    `overrides`, which always win. `seed` defaults to the hash of the
    inputs; pass one explicitly to re-roll a brief (see
    `ensure_unique_brief`).

    Raises ValueError for an unknown app type, an unknown override key, or
    an invalid override value.
    """
    if app_type not in APP_TYPES:
        raise ValueError(f"unknown app type: {app_type!r} (expected one of {sorted(APP_TYPES)})")
    clean_overrides = _validate_overrides(overrides or {})

    industry = match_profile(business_context)
    language: IndustryLanguage = INDUSTRY_LANGUAGES.get(industry or "", DEFAULT_LANGUAGE)

    # 1. Axes: language preset, nudged by free text, then overridden.
    axes = dict(language.axes)
    _apply_text_nudges(
        axes,
        " ".join(
            (
                business_context.tone,
                business_context.audience,
                business_context.description,
                vibe,
            )
        ),
    )
    for axis, delta in _APP_TYPE_NUDGES.get(app_type, {}).items():
        axes[axis] = _clamp_axis(axes[axis] + delta)
    for axis in AXIS_KEYS:
        if axis in clean_overrides:
            axes[axis] = _clamp_axis(float(clean_overrides[axis]))  # type: ignore[arg-type]

    # 2. Seed + RNG: the seed decides the within-pool picks and the hue.
    resolved_seed = (
        seed
        if seed is not None
        else _derive_seed(business_context, vibe, app_type, industry, clean_overrides)
    )
    rng = random.Random(resolved_seed)

    # 3. Palette in OKLCH terms: hue sampled inside the language's range,
    # chroma from the warmer / more expressive axis, primary lightness from
    # contrast (higher contrast → a primary further from the background).
    anchor_hue = rng.uniform(*language.hue_range)
    anchor_chroma = round(0.05 + 0.09 * max(axes["warmth"], axes["type_personality"]), 3)
    primary_lightness = round(0.60 - 0.10 * (axes["contrast"] - 0.5), 3)
    if "anchor_hue" in clean_overrides:
        anchor_hue = float(clean_overrides["anchor_hue"]) % 360.0  # type: ignore[arg-type]
    if "anchor_chroma" in clean_overrides:
        anchor_chroma = max(0.0, min(0.40, float(clean_overrides["anchor_chroma"])))  # type: ignore[arg-type]
    if "primary_lightness" in clean_overrides:
        primary_lightness = max(0.0, min(1.0, float(clean_overrides["primary_lightness"])))  # type: ignore[arg-type]

    # 4. Named choices: weighted seeded picks from the language pools,
    # then explicit overrides (which always win).
    picked: dict[str, str] = {
        "harmony": _weighted_pick(rng, language.harmonies),
        "type_pairing": _weighted_pick(rng, language.type_pairings),
        "layout": _weighted_pick(rng, language.layouts),
        "motion": _weighted_pick(rng, language.motions),
        "scene": language.scene,
        "layout_pattern": _layout_pattern(app_type, axes),
        "focal_point": language.focal_point,
    }
    for key in picked:
        override_key = "motion_profile" if key == "motion" else key
        if override_key in clean_overrides:
            picked[key] = str(clean_overrides[override_key])

    keywords = " ".join(
        part
        for part in (vibe, business_context.industry, business_context.offering)
        if part.strip()
    )

    # 5. The avoid-list: the base list plus the language's additions, unless
    # the caller replaced it outright (the direction step's edited list).
    avoid: tuple[str, ...] = BASE_AVOID + language.avoid
    if "avoid" in clean_overrides:
        avoid = tuple(str(item) for item in clean_overrides["avoid"])  # type: ignore[arg-type]

    return DesignBrief(
        business_context=business_context,
        vibe=vibe,
        app_type=app_type,
        industry=industry,
        axes=axes,
        anchor_hue=anchor_hue,
        anchor_chroma=anchor_chroma,
        primary_lightness=primary_lightness,
        harmony=picked["harmony"],
        type_pairing=picked["type_pairing"],
        layout=picked["layout"],
        motion=picked["motion"],
        layout_pattern=picked["layout_pattern"],
        focal_point=picked["focal_point"],
        scene=picked["scene"],
        avoid=avoid,
        palette_ramp=oklch_ramp(anchor_hue, anchor_chroma),
        seed=resolved_seed,
        keywords=keywords,
        overrides=clean_overrides,
    )


# ---------------------------------------------------------------------------
# DNA fingerprint + uniqueness
# ---------------------------------------------------------------------------


def brief_fingerprint(brief: DesignBrief) -> str:
    """A DNA fingerprint of the brief: primary hex + anchor hue + harmony +
    type + layout + motion + scene + layout pattern + geometry + density.

    A superset of the skills module's `fingerprint_of` (primary + type +
    layout + motion + components): it also covers the scene and the
    geometry/density axes, which the skills fingerprint lacks — two briefs
    that differ only there must still fingerprint differently.
    """
    parts = "|".join(
        [
            brief.primary_hex.lower(),
            f"{brief.anchor_hue:.1f}",
            brief.harmony,
            brief.type_pairing,
            brief.layout,
            brief.motion,
            brief.scene,
            brief.layout_pattern,
            f"{brief.axes.get('geometry', 0.0):.2f}",
            f"{brief.axes.get('density', 0.0):.2f}",
        ]
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


def ensure_unique_brief(
    brief: DesignBrief,
    existing_fingerprints: set[str] | frozenset[str],
    max_attempts: int = MAX_BRIEF_REROLLS,
) -> DesignBrief:
    """Return `brief`, or a re-rolled copy of it whose fingerprint collides
    with nothing in `existing_fingerprints`. Re-rolling re-compiles the same
    inputs under seed+1, seed+2, … — the hue and the within-pool picks move
    while the industry, axes and overrides stay put.

    Raises ValueError when every attempt collides (with a 256-bit
    fingerprint space that means the library already holds every reachable
    brief for these inputs, not a routine outcome).
    """
    for attempt in range(max_attempts):
        candidate = (
            brief
            if attempt == 0
            else compile_brief(
                brief.business_context,
                brief.vibe,
                brief.app_type,
                overrides=brief.overrides,
                seed=brief.seed + attempt,
            )
        )
        if brief_fingerprint(candidate) not in existing_fingerprints:
            return candidate
    raise ValueError(
        f"could not compile a unique design brief in {max_attempts} attempts "
        "(every re-rolled seed collided with an existing system)"
    )
