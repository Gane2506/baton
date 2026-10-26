"""Brief compiler + OKLCH ramps + DNA uniqueness (issue #300).

Pins the contract the generator codes against: deterministic compilation
from business context, OKLCH ramp construction and perceptual uniformity,
industry-language separation, override precedence, the `generation_inputs`
shape the skills module consumes, and seed re-rolls on fingerprint
collision.
"""

import itertools

import pytest

from jev_agent.core.design_brief import (
    BASE_AVOID,
    HARMONY_KEYS,
    LAYOUT_KEYS,
    MOTION_KEYS,
    SCENE_KEY_SET,
    TYPE_PAIRING_KEYS,
    DesignBrief,
    brief_fingerprint,
    compile_brief,
    ensure_unique_brief,
    oklch_ramp,
    oklch_to_hex,
)
from jev_agent.core.design_business import (
    AXIS_KEYS,
    INDUSTRY_LANGUAGES,
    BusinessContext,
)
from jev_agent.core.design_kit import contrast_ratio

FINTECH = BusinessContext(industry="fintech startup", tone="formal", offering="trading app")
RESTAURANT = BusinessContext(
    industry="neighborhood restaurant", tone="warm", offering="wood-fired menu"
)
TRAVEL = BusinessContext(industry="travel booking agency", tone="friendly")

# The industry -> 3D scene type mapping (issue #300: exactly one per industry).
SCENES: dict[str, str] = {
    "fintech": "data-geometry",
    "restaurant": "warm-ambient",
    "saas": "product-orbit",
    "ecommerce": "storefront",
    "health": "calm-growth",
    "education": "campus",
    "travel": "journey",
}


def _luminance(hex_color: str) -> float:
    """WCAG relative luminance via the public contrast helper: contrast
    against black is (L + 0.05) / 0.05."""
    return contrast_ratio(hex_color, "#000000") * 0.05 - 0.05


# ---- OKLCH -------------------------------------------------------------------


def test_oklch_reference_values() -> None:
    # Björn Ottosson's published reference points (exact after rounding).
    assert oklch_to_hex(0.627955, 0.257704, 29.2339) == "#FF0000"
    assert oklch_to_hex(0.5, 0.0, 0.0) == "#636363"
    assert oklch_to_hex(1.0, 0.0, 0.0) == "#FFFFFF"
    assert oklch_to_hex(0.0, 0.0, 0.0) == "#000000"
    # Achromatic: hue is irrelevant when chroma is 0.
    assert oklch_to_hex(0.5, 0.0, 120.0) == oklch_to_hex(0.5, 0.0, 300.0)


def test_oklch_ramp_shape() -> None:
    ramp = oklch_ramp(217.0, 0.10)
    assert len(ramp) == 11
    assert all(isinstance(step, str) and len(step) == 7 and step.startswith("#") for step in ramp)
    # Even lightness steps at fixed chroma/hue: relative luminance rises
    # monotonically from a near-black first step to a near-white last one.
    luminances = [_luminance(step) for step in ramp]
    assert all(later > earlier for earlier, later in itertools.pairwise(luminances))
    assert luminances[0] < 0.02
    assert luminances[-1] > 0.75


@pytest.mark.parametrize("steps", [9, 10, 11])
def test_oklch_ramp_step_counts(steps: int) -> None:
    assert len(oklch_ramp(30.0, 0.08, steps=steps)) == steps


@pytest.mark.parametrize("steps", [0, 8, 12])
def test_oklch_ramp_bad_step_count_raises(steps: int) -> None:
    with pytest.raises(ValueError, match=r"9\.\.11"):
        oklch_ramp(30.0, 0.08, steps=steps)


def test_oklch_ramp_bad_chroma_and_window_raise() -> None:
    with pytest.raises(ValueError, match="chroma"):
        oklch_ramp(30.0, -0.01)
    with pytest.raises(ValueError, match="lightness window"):
        oklch_ramp(30.0, 0.08, lightness_min=0.9, lightness_max=0.1)


def test_oklch_perceptual_uniformity() -> None:
    """Same lightness across hues reads equal (report §2.2): at fixed L
    and C the relative-luminance spread across 12 hues is tiny — the same
    test in HSL at fixed lightness spreads ~0.86 (yellow screams, blue
    recedes), which is why ramps are built in OKLCH, not HSL."""
    for lightness, chroma in ((0.65, 0.12), (0.6, 0.10), (0.75, 0.08)):
        luminances = [_luminance(oklch_to_hex(lightness, chroma, hue)) for hue in range(0, 360, 30)]
        assert max(luminances) - min(luminances) < 0.10


# ---- compilation ---------------------------------------------------------------


def test_compile_brief_is_deterministic() -> None:
    first = compile_brief(FINTECH, "serious", "dashboard")
    second = compile_brief(FINTECH, "serious", "dashboard")
    assert first == second
    assert brief_fingerprint(first) == brief_fingerprint(second)


def test_compile_brief_detects_industry_and_scene() -> None:
    brief = compile_brief(FINTECH, "serious", "dashboard")
    assert brief.industry == "fintech"
    assert brief.scene == "data-geometry"
    assert brief.focal_point == "portfolio total"
    assert brief.layout_pattern == "table-led"  # dense dashboard
    restaurant = compile_brief(RESTAURANT, "cozy", "website")
    assert restaurant.industry == "restaurant"
    assert restaurant.scene == "warm-ambient"
    assert restaurant.focal_point == "signature dish"


@pytest.mark.parametrize("industry,scene", list(SCENES.items()))
def test_every_industry_has_its_exact_scene(industry: str, scene: str) -> None:
    assert INDUSTRY_LANGUAGES[industry].scene == scene
    assert scene in SCENE_KEY_SET


def test_compile_brief_unknown_industry_uses_default_language() -> None:
    brief = compile_brief(BusinessContext(industry="underwater basket weaving"), "", "website")
    assert brief.industry is None
    assert brief.scene == "warm-ambient"  # the neutral default scene
    assert brief.axes["warmth"] == 0.50
    assert brief.keywords == "underwater basket weaving"


def test_compile_brief_axes_come_from_language_preset() -> None:
    brief = compile_brief(FINTECH, "", "website")
    preset = INDUSTRY_LANGUAGES["fintech"].axes
    # No tone/vibe nudges apply to "formal"? "formal" IS a nudge word — so
    # compare against a context with no nudge words instead.
    plain = compile_brief(BusinessContext(industry="fintech startup"), "", "website")
    for axis in AXIS_KEYS:
        assert plain.axes[axis] == pytest.approx(preset[axis], abs=0.06)
    # The website nudge lifts type_personality slightly; the preset still shows.
    assert brief.axes["formality"] > 0.9


def test_compile_brief_tone_nudges_axes() -> None:
    calm = compile_brief(
        BusinessContext(industry="fintech startup", tone="calm minimal"), "", "website"
    )
    plain = compile_brief(BusinessContext(industry="fintech startup"), "", "website")
    assert calm.axes["motion"] < plain.axes["motion"]
    assert calm.axes["contrast"] < plain.axes["contrast"]
    playful = compile_brief(
        BusinessContext(industry="saas platform", tone="playful"), "", "website"
    )
    sober = compile_brief(BusinessContext(industry="saas platform"), "", "website")
    assert playful.axes["motion"] > sober.axes["motion"]
    assert playful.axes["formality"] < sober.axes["formality"]


def test_compile_brief_axes_stay_in_range() -> None:
    # Stacked nudges (playful + kids + enterprise) must not escape 0..1.
    brief = compile_brief(
        BusinessContext(
            industry="restaurant",
            tone="playful luxury",
            audience="kids and enterprise executives",
            description="bold energetic calm minimal",
        ),
        "playful",
        "dashboard",
    )
    assert set(brief.axes) == set(AXIS_KEYS)
    assert all(0.0 <= value <= 1.0 for value in brief.axes.values())


def test_compile_brief_palette_is_oklch_ramp() -> None:
    brief = compile_brief(RESTAURANT, "cozy", "website")
    assert brief.palette_ramp == oklch_ramp(brief.anchor_hue, brief.anchor_chroma)
    assert len(brief.palette_ramp) == 11
    assert 5.0 <= brief.anchor_hue <= 35.0  # inside the restaurant range
    assert 0.05 <= brief.anchor_chroma <= 0.14
    assert brief.primary_hex in brief.palette_ramp


def test_compile_brief_avoid_list() -> None:
    brief = compile_brief(FINTECH, "serious", "dashboard")
    for banned in BASE_AVOID:
        assert banned in brief.avoid
    # Plus the fintech additions, in order after the base list.
    assert brief.avoid[len(BASE_AVOID) :] == INDUSTRY_LANGUAGES["fintech"].avoid
    assert "no playful rounded cards for money data" in brief.avoid


def test_compile_brief_unknown_app_type_raises() -> None:
    with pytest.raises(ValueError, match="unknown app type"):
        compile_brief(FINTECH, "", "smartwatch")


def test_compile_brief_layout_pattern_per_app_type() -> None:
    # A website with an expressive type axis tells a story; a dashboard
    # with a dense axis leads with a table.
    assert compile_brief(RESTAURANT, "cozy", "website").layout_pattern == "story-led"
    assert compile_brief(FINTECH, "serious", "dashboard").layout_pattern == "table-led"
    assert compile_brief(TRAVEL, "", "mobile").layout_pattern == "feed-led"
    assert compile_brief(TRAVEL, "", "pwa").layout_pattern == "shell-led"


# ---- generation inputs ---------------------------------------------------------


def test_generation_inputs_shape() -> None:
    brief = compile_brief(FINTECH, "serious", "dashboard")
    inputs = brief.generation_inputs()
    assert set(inputs) == {"seed", "keywords", "app_type", "overrides"}
    assert inputs["seed"] == brief.seed
    assert inputs["keywords"] == "serious fintech startup trading app"
    assert inputs["app_type"] == "dashboard"
    assert inputs["overrides"] == {}
    # The seed/keywords/app_type pass straight into the skills module's
    # generate_unique_system(seed, keywords=..., app_type=...) signature.
    assert isinstance(inputs["seed"], int) and inputs["seed"] >= 0


def test_generation_inputs_carries_overrides() -> None:
    brief = compile_brief(FINTECH, "", "website", overrides={"warmth": 0.1})
    assert brief.generation_inputs()["overrides"] == {"warmth": 0.1}


# ---- overrides -----------------------------------------------------------------


def test_overrides_win_over_derived_values() -> None:
    brief = compile_brief(
        FINTECH,
        "serious",
        "dashboard",
        overrides={
            "warmth": 0.05,
            "harmony": "triadic",
            "type_pairing": "bebas-roboto",
            "layout": "airy-rounded",
            "motion_profile": "gentle",
            "scene": "journey",
            "layout_pattern": "story-led",
            "focal_point": "the one number",
            "anchor_hue": 123.0,
            "anchor_chroma": 0.12,
            "primary_lightness": 0.5,
        },
    )
    assert brief.axes["warmth"] == 0.05
    assert brief.harmony == "triadic"
    assert brief.type_pairing == "bebas-roboto"
    assert brief.layout == "airy-rounded"
    assert brief.motion == "gentle"
    assert brief.scene == "journey"
    assert brief.layout_pattern == "story-led"
    assert brief.focal_point == "the one number"
    assert brief.anchor_hue == 123.0
    assert brief.anchor_chroma == 0.12
    assert brief.primary_lightness == 0.5
    assert brief.palette_ramp == oklch_ramp(123.0, 0.12)


def test_override_beats_tone_nudge() -> None:
    # "playful" would raise motion and drop formality; the override wins.
    brief = compile_brief(
        BusinessContext(industry="saas platform", tone="playful"),
        "",
        "website",
        overrides={"motion": 0.0, "formality": 1.0},
    )
    assert brief.axes["motion"] == 0.0
    assert brief.axes["formality"] == 1.0


def test_override_axes_are_clamped() -> None:
    brief = compile_brief(FINTECH, "", "website", overrides={"warmth": 2.5, "contrast": -1.0})
    assert brief.axes["warmth"] == 1.0
    assert brief.axes["contrast"] == 0.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"nope": 1.0},
        {"harmony": "tetradic"},
        {"type_pairing": "comic-sans"},
        {"layout": "masonry"},
        {"motion_profile": "wobbly"},
        {"motion": "wobbly"},  # the axis override must be a number
        {"scene": "underwater"},
        {"layout_pattern": "sidebar-led"},
        {"warmth": "hot"},
        {"anchor_hue": "blue"},
        {"focal_point": "   "},
    ],
)
def test_invalid_overrides_raise(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        compile_brief(FINTECH, "", "website", overrides=overrides)


# ---- fingerprints + uniqueness ---------------------------------------------------


def test_two_different_briefs_produce_different_systems() -> None:
    """Report §10.4 rule 2: two different briefs must produce two
    mathematically different systems, every time."""
    fintech = compile_brief(FINTECH, "serious", "dashboard")
    restaurant = compile_brief(RESTAURANT, "cozy", "website")
    assert brief_fingerprint(fintech) != brief_fingerprint(restaurant)
    assert fintech.primary_hex != restaurant.primary_hex
    assert fintech.type_pairing != restaurant.type_pairing or fintech.layout != restaurant.layout
    # Same industry, different vibe/audience: still a different system.
    other_fintech = compile_brief(
        BusinessContext(industry="fintech startup", tone="playful", audience="creatives"),
        "playful",
        "mobile",
    )
    assert brief_fingerprint(fintech) != brief_fingerprint(other_fintech)


def test_fingerprint_covers_scene_and_geometry() -> None:
    """The DNA fingerprint is a superset of the skills fingerprint: two
    briefs that differ only in scene or geometry must still differ."""
    base = compile_brief(FINTECH, "serious", "dashboard")
    rescene = compile_brief(FINTECH, "serious", "dashboard", overrides={"scene": "journey"})
    assert brief_fingerprint(base) != brief_fingerprint(rescene)
    regeometry = compile_brief(FINTECH, "serious", "dashboard", overrides={"geometry": 0.05})
    assert brief_fingerprint(base) != brief_fingerprint(regeometry)


def test_ensure_unique_brief_rerolls_on_collision() -> None:
    brief = compile_brief(FINTECH, "serious", "dashboard")
    unique = ensure_unique_brief(brief, {brief_fingerprint(brief)})
    assert brief_fingerprint(unique) not in {brief_fingerprint(brief)}
    assert unique.seed == brief.seed + 1
    # Everything the brief decided from its inputs is preserved; only the
    # seed-driven picks moved.
    assert unique.industry == brief.industry
    assert unique.axes == brief.axes
    assert unique.overrides == brief.overrides


def test_ensure_unique_brief_no_collision_is_identity() -> None:
    brief = compile_brief(FINTECH, "serious", "dashboard")
    assert ensure_unique_brief(brief, set()) == brief


def test_ensure_unique_brief_exhaustion_raises() -> None:
    brief = compile_brief(FINTECH, "serious", "dashboard")
    existing = {
        brief_fingerprint(compile_brief(FINTECH, "serious", "dashboard", seed=brief.seed + attempt))
        for attempt in range(10)
    }
    with pytest.raises(ValueError, match="could not compile a unique design brief"):
        ensure_unique_brief(brief, existing)


def test_ensure_unique_brief_skips_multiple_collisions() -> None:
    brief = compile_brief(FINTECH, "serious", "dashboard")
    first_two = {
        brief_fingerprint(compile_brief(FINTECH, "serious", "dashboard", seed=brief.seed + attempt))
        for attempt in range(2)
    }
    unique = ensure_unique_brief(brief, first_two)
    assert unique.seed == brief.seed + 2
    assert brief_fingerprint(unique) not in first_two


# ---- compatibility with the merged skills module (issue #299) -----------------
# generation_inputs() must pass straight into the real
# design_skills.generate_unique_system: same seed, keywords and app type.


def test_generation_inputs_work_with_real_generate_unique_system() -> None:
    from jev_agent.core.design_skills import fingerprint_of, generate_unique_system

    brief = compile_brief(FINTECH, "serious", "dashboard")
    inputs = brief.generation_inputs()
    system, attempts = generate_unique_system(
        inputs["seed"],  # type: ignore[arg-type]
        keywords=inputs["keywords"],  # type: ignore[arg-type]
        app_type=inputs["app_type"],  # type: ignore[arg-type]
        existing_fingerprints=frozenset(),
    )
    assert system.seed == brief.seed
    assert attempts == 1
    assert system.fingerprint == fingerprint_of(system.choices)
    # A collision in the skills fingerprint space re-rolls the seed.
    rerolled, attempts = generate_unique_system(
        inputs["seed"],  # type: ignore[arg-type]
        keywords=inputs["keywords"],  # type: ignore[arg-type]
        app_type=inputs["app_type"],  # type: ignore[arg-type]
        existing_fingerprints={system.fingerprint},
    )
    assert rerolled.seed == brief.seed + 1
    assert attempts == 2
    assert rerolled.fingerprint != system.fingerprint


def test_brief_key_sets_match_merged_skills_catalogs() -> None:
    """The literal key sets in design_brief mirror the merged skills
    catalogs; if the skills module gains a key, this test fails first."""
    from jev_agent.core.design_skills import (
        COLOR_STRATEGIES,
        LAYOUT_PROFILES,
        MOTION_PROFILES,
        TYPE_PAIRINGS,
    )

    assert {strategy.key for strategy in COLOR_STRATEGIES} == set(HARMONY_KEYS)
    assert {pairing.key for pairing in TYPE_PAIRINGS} == set(TYPE_PAIRING_KEYS)
    assert {profile.key for profile in LAYOUT_PROFILES} == set(LAYOUT_KEYS)
    assert {profile.key for profile in MOTION_PROFILES} == set(MOTION_KEYS)
    for language in INDUSTRY_LANGUAGES.values():
        assert set(language.harmonies) <= set(HARMONY_KEYS)
        assert set(language.type_pairings) <= set(TYPE_PAIRING_KEYS)
        assert set(language.layouts) <= set(LAYOUT_KEYS)
        assert set(language.motions) <= set(MOTION_KEYS)


# ---- industry languages ----------------------------------------------------------


def test_all_seven_industry_languages_differ_on_three_axes_each() -> None:
    """Every pair of languages differs by >= 0.15 on at least 3 of the 7
    axes — the presets are spaced, not clustered around a centroid."""
    for first_key, second_key in itertools.combinations(INDUSTRY_LANGUAGES, 2):
        first, second = INDUSTRY_LANGUAGES[first_key], INDUSTRY_LANGUAGES[second_key]
        differing = [
            axis for axis in AXIS_KEYS if abs(first.axes[axis] - second.axes[axis]) >= 0.15
        ]
        assert len(differing) >= 3, f"{first_key} vs {second_key} differ on only {differing}"


def test_industry_languages_are_complete() -> None:
    """Each language: all 7 axes in range, a valid hue window, non-empty
    pools of valid skills keys, a valid scene, and a focal point."""
    assert set(INDUSTRY_LANGUAGES) == {
        "fintech",
        "restaurant",
        "saas",
        "ecommerce",
        "health",
        "education",
        "travel",
    }
    for key, language in INDUSTRY_LANGUAGES.items():
        assert set(language.axes) == set(AXIS_KEYS), key
        assert all(0.0 <= value <= 1.0 for value in language.axes.values()), key
        low, high = language.hue_range
        assert 0.0 <= low < high <= 360.0, key
        assert language.harmonies and set(language.harmonies) <= HARMONY_KEYS, key
        assert language.type_pairings and set(language.type_pairings) <= TYPE_PAIRING_KEYS, key
        assert language.layouts and set(language.layouts) <= LAYOUT_KEYS, key
        assert language.motions and set(language.motions) <= MOTION_KEYS, key
        assert language.scene in SCENE_KEY_SET, key
        assert language.focal_point, key


def test_brief_dataclass_is_frozen() -> None:
    brief = compile_brief(FINTECH, "serious", "dashboard")
    assert isinstance(brief, DesignBrief)
    with pytest.raises(AttributeError):
        brief.seed = 1  # type: ignore[misc]


def test_avoid_override_replaces_the_derived_list() -> None:
    context = BusinessContext(industry="fintech")
    brief = compile_brief(context, "", "dashboard", overrides={"avoid": ["custom rule"]})
    assert brief.avoid == ("custom rule",)
    assert "no purple gradients" not in brief.avoid


def test_avoid_override_rejects_bad_values() -> None:
    context = BusinessContext(industry="fintech")
    with pytest.raises(ValueError):
        compile_brief(context, "", "dashboard", overrides={"avoid": "not a list"})
    with pytest.raises(ValueError):
        compile_brief(context, "", "dashboard", overrides={"avoid": [""]})
    with pytest.raises(ValueError):
        compile_brief(context, "", "dashboard", overrides={"avoid": [123]})
