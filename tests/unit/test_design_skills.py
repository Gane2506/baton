"""Seeded design-system uniqueness (issue #299): the executable skill catalog
(`core/design_skills.py`), the fingerprint collision check, the contrast gate,
and the seeded wiring in the design kit and the library.

The acceptance bar in one file: the same business input with different seeds
differs on palette, type pairing, and components; the same seed reproduces
itself exactly; a fingerprint collision is detected and re-rolled; every
generated palette still passes the WCAG contrast gate.
"""

from pathlib import Path

import pytest

from jev_agent.core.design_kit import (
    design_system_markdown,
    palette_for_vibe,
    tokens_for_app_type,
)
from jev_agent.core.design_library import library_fingerprints, save_library_system
from jev_agent.core.design_showcase import VIBE_PROFILES
from jev_agent.core.design_skills import (
    COLOR_STRATEGIES,
    COMPONENT_VARIANTS,
    LAYOUT_PROFILES,
    MOTION_PROFILES,
    TYPE_PAIRINGS,
    check_palette_contrast,
    fingerprint_of,
    generate_seeded_system,
    generate_unique_system,
    rich_tokens_for_seeded,
    steer_hue,
)

KEYWORDS = "restaurant warm friendly"
APP_TYPE = "website"


# ---- the catalog itself ----------------------------------------------------------


def test_type_pairing_catalog_is_varied_and_not_all_inter() -> None:
    assert len(TYPE_PAIRINGS) >= 8
    keys = {pairing.key for pairing in TYPE_PAIRINGS}
    assert len(keys) == len(TYPE_PAIRINGS)
    display_stacks = [pairing.display_stack for pairing in TYPE_PAIRINGS]
    inter_displays = [stack for stack in display_stacks if stack.startswith('"Inter"')]
    assert inter_displays == []
    assert {pairing.scale_ratio for pairing in TYPE_PAIRINGS} >= {1.2, 1.25, 1.333}


def test_color_strategies_cover_the_harmony_schemes() -> None:
    schemes = {strategy.scheme for strategy in COLOR_STRATEGIES}
    assert {
        "analogous",
        "complementary",
        "split-complementary",
        "triadic",
        "monochromatic",
        "hue-shift",
    } <= schemes


def test_layout_and_motion_catalogs_are_varied() -> None:
    assert len(LAYOUT_PROFILES) >= 4
    assert {profile.density for profile in LAYOUT_PROFILES} >= {"tight", "balanced", "generous"}
    assert {profile.shadow_style for profile in LAYOUT_PROFILES} >= {"flat", "soft", "dramatic"}
    assert len(MOTION_PROFILES) >= 4
    assert {profile.key for profile in MOTION_PROFILES} >= {
        "snappy",
        "smooth",
        "springy",
        "stately",
    }


def test_component_variants_differ_per_app_type() -> None:
    for app_type, variants in COMPONENT_VARIANTS.items():
        assert len(variants) >= 3, app_type
        sets = [{name for name, _ in variant.components} for variant in variants]
        assert len({frozenset(names) for names in sets}) == len(sets), app_type


def test_steer_hue_follows_the_vibe_family() -> None:
    # The steer reuses the design kit's family mapping: food warms toward 8°.
    assert abs(steer_hue("restaurant cozy") - 8.0) < 1.0
    assert abs(steer_hue("fintech banking") - 217.0) < 1.0


# ---- seeded generation: same input, different seeds differ -----------------------


def test_same_input_different_seeds_differ_on_every_axis() -> None:
    systems = [
        generate_seeded_system(seed, keywords=KEYWORDS, app_type=APP_TYPE) for seed in range(30)
    ]
    primaries = {system.palette["primary"] for system in systems}
    pairings = {system.type_pairing.key for system in systems}
    layouts = {system.layout.key for system in systems}
    motions = {system.motion.key for system in systems}
    components = {system.components.key for system in systems}
    assert len(primaries) > 1
    assert len(pairings) > 1
    assert len(layouts) > 1
    assert len(motions) > 1
    assert len(components) > 1
    # And at least one pair differs on palette, pairing, AND components at once.
    found = any(
        first.palette["primary"] != second.palette["primary"]
        and first.type_pairing.key != second.type_pairing.key
        and first.components.key != second.components.key
        for first in systems
        for second in systems
        if first is not second
    )
    assert found


def test_same_seed_reproduces_identically() -> None:
    first = generate_seeded_system(42, keywords=KEYWORDS, app_type=APP_TYPE)
    second = generate_seeded_system(42, keywords=KEYWORDS, app_type=APP_TYPE)
    assert first.palette == second.palette
    assert first.choices == second.choices
    assert first.fingerprint == second.fingerprint
    assert first.type_pairing == second.type_pairing
    assert first.layout == second.layout
    assert first.motion == second.motion
    assert first.components == second.components


def test_fingerprint_covers_the_key_axes() -> None:
    system = generate_seeded_system(7, keywords=KEYWORDS, app_type=APP_TYPE)
    changed = dict(system.choices)
    changed["type_pairing"] = "other-pairing"
    assert fingerprint_of(changed) != system.fingerprint
    changed = dict(system.choices)
    changed["primary"] = "#000000"
    assert fingerprint_of(changed) != system.fingerprint


# ---- fingerprint collision: detect and re-roll -----------------------------------


def test_collision_is_detected_and_rerolled() -> None:
    existing_system = generate_seeded_system(11, keywords=KEYWORDS, app_type=APP_TYPE)
    system, attempts = generate_unique_system(
        11,
        existing_fingerprints={existing_system.fingerprint},
        keywords=KEYWORDS,
        app_type=APP_TYPE,
    )
    assert attempts == 2
    assert system.seed == 12
    assert system.fingerprint != existing_system.fingerprint
    assert system.fingerprint not in {existing_system.fingerprint}


def test_collision_exhaustion_raises() -> None:
    blocked = {
        generate_seeded_system(seed, keywords=KEYWORDS, app_type=APP_TYPE).fingerprint
        for seed in range(10)
    }
    with pytest.raises(ValueError, match="could not generate a unique design system"):
        generate_unique_system(
            0,
            existing_fingerprints=blocked,
            keywords=KEYWORDS,
            app_type=APP_TYPE,
            max_attempts=10,
        )


def test_library_fingerprints_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    tokens: dict[str, object] = {"colors": {"primary": "#2563EB"}}
    save_library_system(
        "seeded-one",
        "# Seeded one\n",
        tokens,
        meta_extra={"design_fingerprint": "abc123", "design_seed": 5},
    )
    save_library_system("plain-one", "# Plain one\n", tokens)
    assert library_fingerprints() == {"seeded-one": "abc123"}


# ---- the contrast gate -----------------------------------------------------------


@pytest.mark.parametrize(
    "keywords", ["restaurant", "fintech", "luxury", "playful", "", "eco garden"]
)
def test_every_generated_palette_passes_the_contrast_gate(keywords: str) -> None:
    for seed in range(100):
        system = generate_seeded_system(seed, keywords=keywords, app_type=APP_TYPE)
        assert check_palette_contrast(system.palette) == []


def test_contrast_gate_catches_a_bad_palette() -> None:
    bad = dict(palette_for_vibe("fintech"))
    bad["text_muted"] = "#CCCCCC"  # far too light on white
    assert check_palette_contrast(bad)


# ---- the design-kit wiring -------------------------------------------------------


def test_tokens_for_app_type_defaults_are_unchanged() -> None:
    palette = palette_for_vibe("fintech")
    tokens = tokens_for_app_type("website", palette, "both")
    assert tokens["spacing"] == {
        "xs": 4,
        "sm": 8,
        "md": 16,
        "lg": 32,
        "xl": 64,
        "2xl": 96,
        "3xl": 128,
    }
    assert "typography" not in tokens
    assert "motion" not in tokens


def test_tokens_for_app_type_seeded_overrides() -> None:
    system = generate_seeded_system(3, keywords=KEYWORDS, app_type=APP_TYPE)
    tokens = tokens_for_app_type(
        "website",
        system.palette,
        "both",
        layout=system.layout,
        pairing=system.type_pairing,
        motion=system.motion,
    )
    assert tokens["spacing"] != tokens_for_app_type("website", system.palette, "both")["spacing"]
    assert tokens["radii"] == dict(system.layout.radii)
    typography = tokens["typography"]
    assert isinstance(typography, dict)
    assert typography["font_display"] == system.type_pairing.display_stack
    assert typography["font_body"] == system.type_pairing.body_stack
    assert tokens["motion"] == system.motion.tokens


def test_markdown_renders_the_chosen_variant_and_choices() -> None:
    system = generate_seeded_system(5, keywords=KEYWORDS, app_type=APP_TYPE)
    palette = palette_for_vibe("fintech")
    default = design_system_markdown("acme", "website", "Web + Tailwind", palette, "both")
    seeded = design_system_markdown(
        "acme",
        "website",
        "Web + Tailwind",
        system.palette,
        "both",
        components=list(system.components.components),
        choices=system.choices,
        pairing=system.type_pairing,
    )
    assert "## Design choices" in seeded
    assert "## Design choices" not in default
    for key in ("color_strategy", "type_pairing", "layout", "motion", "components"):
        assert f"`{key}`: {system.choices[key]}" in seeded
    for name, _guidance in system.components.components:
        assert f"**{name}**" in seeded
    assert system.type_pairing.display_stack in seeded


def test_rich_tokens_carry_the_seeded_choices() -> None:
    system = generate_seeded_system(9, keywords=KEYWORDS, app_type=APP_TYPE)
    rich = rich_tokens_for_seeded(VIBE_PROFILES["minimal"], system, "both")
    typography = rich["typography"]
    assert isinstance(typography, dict)
    assert typography["font_display"] == system.type_pairing.display_stack
    assert typography["font_body"] == system.type_pairing.body_stack
    assert rich["spacing"] == system.layout.spacing
    assert rich["radii"] == system.layout.radii
    motion = rich["motion"]
    assert isinstance(motion, dict)
    assert motion["duration_normal"] == system.motion.durations["normal"]
    assert motion["ease_standard"] == system.motion.ease_standard
    colors = rich["colors"]
    assert isinstance(colors, dict)
    light = colors["light"]
    assert isinstance(light, dict)
    assert light["primary"] == system.palette["primary"]
    assert light["primary_hover"] == system.palette["primary_hover"]
    assert light["primary_text"] == system.palette["primary_text"]
    dark = colors["dark"]
    assert isinstance(dark, dict)
    assert dark["primary"] != system.palette["primary"]


# ---- brief enforcement (issue #301) ---------------------------------------------------------


def test_force_pins_the_briefs_named_choices() -> None:
    force = {
        "harmony": "monochromatic",
        "type_pairing": "space-grotesk-plex",
        "layout": "dense-square",
        "motion_profile": "snappy",
        "layout_pattern": "table-led",
        "anchor_hue": 210.0,
    }
    system = generate_seeded_system(7, keywords="fintech", app_type="dashboard", force=force)
    assert system.color_strategy.key == "monochromatic"
    assert system.type_pairing.key == "space-grotesk-plex"
    assert system.layout.key == "dense-square"
    assert system.motion.key == "snappy"
    assert system.components.key == "table-led"
    # The anchor hue is honoured: the primary sits near 210°, not wherever
    # the keyword steer would have put it.
    assert abs(system.hue - 210.0) <= 12.0 + 1e-9


def test_force_is_deterministic_and_still_varies_the_rest() -> None:
    force = {"harmony": "monochromatic", "type_pairing": "space-grotesk-plex"}
    first = generate_seeded_system(7, keywords="fintech", app_type="dashboard", force=force)
    second = generate_seeded_system(7, keywords="fintech", app_type="dashboard", force=force)
    assert first == second
    # The unforced axes still vary by seed.
    other = generate_seeded_system(8, keywords="fintech", app_type="dashboard", force=force)
    assert other.color_strategy.key == "monochromatic"
    assert other.type_pairing.key == "space-grotesk-plex"
    assert (other.layout.key, other.motion.key) != (first.layout.key, first.motion.key)


def test_force_motion_alias_must_agree() -> None:
    system = generate_seeded_system(3, force={"motion": "gentle"})
    assert system.motion.key == "gentle"
    with pytest.raises(ValueError):
        generate_seeded_system(3, force={"motion": "snappy", "motion_profile": "brisk"})


def test_force_rejects_unknown_keys_and_values() -> None:
    with pytest.raises(ValueError):
        generate_seeded_system(3, force={"harmony": "not-a-harmony"})
    with pytest.raises(ValueError):
        generate_seeded_system(3, force={"unknown_key": "x"})
    with pytest.raises(ValueError):
        generate_seeded_system(3, force={"layout_pattern": "not-a-pattern"})
    with pytest.raises(ValueError):
        generate_seeded_system(3, force={"anchor_hue": "blue"})
    # A layout pattern for another app type is not honoured for this one.
    with pytest.raises(ValueError):
        generate_seeded_system(3, app_type="dashboard", force={"layout_pattern": "hero-led"})


def test_generate_unique_system_honours_force_on_reroll() -> None:
    force = {"harmony": "triadic", "type_pairing": "bebas-roboto"}
    first, _ = generate_unique_system(11, keywords="x", app_type="website", force=force)
    assert first.color_strategy.key == "triadic"
    assert first.type_pairing.key == "bebas-roboto"
    # Colliding with the first system re-rolls the seed, not the force.
    second, attempts = generate_unique_system(
        11,
        existing_fingerprints={first.fingerprint},
        keywords="x",
        app_type="website",
        force=force,
    )
    assert attempts == 2
    assert second.color_strategy.key == "triadic"
    assert second.type_pairing.key == "bebas-roboto"
    assert second.fingerprint != first.fingerprint
