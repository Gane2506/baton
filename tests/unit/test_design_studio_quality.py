"""Tests for the Design Studio quality elevation (issue #155):

- ``design_showcase``: vibe profiles + rich token generation
- ``design_showcase_html``: the showcase gallery renderer
- ``design_codegen``: reusable-elements code generation (HTML+CSS, React+Tailwind)
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from jev_agent.core.design_codegen import (
    CODEGEN_TARGETS,
    bundle_zip,
    codegen_summary,
    component_catalog,
    css_variables,
    generate_files,
    target_for_stack,
    validate_target,
)
from jev_agent.core.design_kit import contrast_ratio
from jev_agent.core.design_showcase import (
    VIBE_PROFILES,
    rich_tokens,
    vibe_for_keywords,
)
from jev_agent.core.design_showcase_html import render_showcase

_TOKENS: dict[str, Any] = {"design_tokens": rich_tokens(vibe_for_keywords("elegant"), "both")}


def _rich() -> dict[str, Any]:
    tokens = _TOKENS["design_tokens"]
    assert isinstance(tokens, dict)
    return tokens


# ---------------------------------------------------------------------------
# Vibe profiles
# ---------------------------------------------------------------------------


def test_vibe_profiles_cover_expected_aesthetics() -> None:
    for key in ("minimal", "bold", "playful", "elegant", "brutalist", "calm"):
        assert key in VIBE_PROFILES, f"missing vibe profile {key}"


def test_vibe_for_keywords_maps_free_text() -> None:
    assert vibe_for_keywords("brutalist, raw").key == "brutalist"
    assert vibe_for_keywords("soft pastel playful").key == "playful"
    assert vibe_for_keywords("").key == "minimal"  # deterministic fallback


# ---------------------------------------------------------------------------
# Rich tokens
# ---------------------------------------------------------------------------


def test_rich_tokens_has_full_structure() -> None:
    tokens = _rich()
    assert set(tokens) >= {"vibe", "colors", "typography", "spacing", "radii", "shadows", "motion"}
    light = tokens["colors"]["light"]
    dark = tokens["colors"]["dark"]
    for role in (
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
        "info",
    ):
        assert role in light, f"missing light role {role}"
        assert role in dark, f"missing dark role {role}"
    assert light["background"] != dark["background"], "dark mode must differ"


def test_rich_tokens_typography_hierarchy() -> None:
    typo = _rich()["typography"]
    assert set(typo) >= {
        "display_xl",
        "display_lg",
        "headline_lg",
        "headline_md",
        "body_lg",
        "body_md",
        "body_sm",
        "caption",
        "overline",
    }
    sizes = [typo[key]["size"] for key in ("display_xl", "headline_lg", "body_md", "caption")]
    assert sizes == sorted(sizes, reverse=True), "type scale must descend"


def test_rich_tokens_scales_present() -> None:
    tokens = _rich()
    assert len(tokens["spacing"]) >= 8
    assert len(tokens["radii"]) >= 5
    assert len(tokens["shadows"]) >= 4
    assert "ease_in_out" in tokens["motion"]


# ---------------------------------------------------------------------------
# Showcase HTML
# ---------------------------------------------------------------------------


def test_render_showcase_contains_all_sections() -> None:
    profile = vibe_for_keywords("bold")
    html = render_showcase("demo", profile, _rich(), "both")
    for section in ("Palette", "Typography", "Components", "Foundations"):
        assert section in html
    assert "dark" in html.lower() and "light" in html.lower()


def test_render_showcase_embeds_vibe_name() -> None:
    profile = vibe_for_keywords("brutalist")
    html = render_showcase("demo", profile, _rich(), "light")
    assert profile.name in html


def test_render_showcase_theme_toggle_only_when_both() -> None:
    profile = vibe_for_keywords("minimal")
    both = render_showcase("demo", profile, _rich(), "both")
    light = render_showcase("demo", profile, _rich(), "light")
    assert 'id="themeToggle"' in both
    assert 'id="themeToggle"' not in light


def test_render_showcase_fits_390px_viewport() -> None:
    """Issue #317: the generated showcase laid out ~640px wide at 390px.

    The topbar (brand + 5 nav links + theme toggle) never collapsed and the
    type specimens used fixed sizes, so the page overflowed horizontally.
    The nav must collapse at <=720px and every specimen size must be a
    clamp() whose ceiling is the design token, so desktop keeps the token
    size while mobile caps it.
    """
    profile = vibe_for_keywords("playful")
    html = render_showcase("audit-shots", profile, _rich(), "both")
    assert "@media(max-width:720px)" in html
    assert ".topbar nav{display:none}" in html
    for size, clamp in (
        ("display-xl", "clamp(40px,11vw,var(--tw-display-xl-size,76px))"),
        ("display-lg", "clamp(34px,9vw,var(--tw-display-lg-size,60px))"),
        ("display-md", "clamp(30px,8vw,var(--tw-display-md-size,48px))"),
        ("display-sm", "clamp(26px,7vw,var(--tw-display-sm-size,36px))"),
        ("headline-lg", "clamp(22px,6vw,var(--tw-headline-lg-size,28px))"),
        ("headline-md", "clamp(20px,5.5vw,var(--tw-headline-md-size,22px))"),
        ("headline-sm", "clamp(16px,5vw,var(--tw-headline-sm-size,18px))"),
    ):
        assert clamp in html, size
        assert f"font-size:var(--tw-{size}-size," not in html, size


# ---------------------------------------------------------------------------
# Codegen
# ---------------------------------------------------------------------------


def test_target_for_stack() -> None:
    assert target_for_stack("Web + Tailwind") == "react-tailwind"
    assert target_for_stack("Web + plain CSS") == "html-css"
    assert target_for_stack("React Native") == "react-tailwind"
    assert target_for_stack("something unknown") == "html-css"
    assert validate_target("nope") == "html-css"


def test_css_variables_emit_light_and_dark() -> None:
    css = css_variables(_TOKENS)
    assert ":root {" in css
    assert "--color-primary:" in css
    assert "--color-primary-hover:" in css
    assert "--font-display:" in css
    assert "--space-md:" in css
    assert "--radius-md:" in css
    assert "--shadow-md:" in css
    assert '[data-theme="dark"]' in css


@pytest.mark.parametrize("target", ["html-css", "react-tailwind"])
def test_generate_files_expected_set(target: str) -> None:
    files = generate_files(target, "demo", _TOKENS)
    catalog = [item["filename"] for item in component_catalog(target)]
    assert sorted(files) == sorted(catalog)
    for filename in catalog:
        assert files[filename].strip(), f"{filename} is empty"


def test_html_css_components_use_variables_not_hex() -> None:
    css = generate_files("html-css", "demo", _TOKENS)["components.css"]
    assert "var(--color-primary)" in css
    # No hard-coded hex colors in the component layer — everything via tokens.
    import re

    hexes = re.findall(r"#[0-9a-fA-F]{3,6}\b", css)
    allowed = {"#fff"}  # avatar text + toggle knob only
    assert set(hexes) <= allowed, f"hard-coded colors: {set(hexes) - allowed}"


def test_react_components_documented_and_exported() -> None:
    jsx = generate_files("react-tailwind", "demo", _TOKENS)["components.jsx"]
    for component in (
        "Button",
        "Field",
        "Badge",
        "Alert",
        "Card",
        "Avatar",
        "Modal",
        "Navbar",
        "Table",
        "Progress",
        "Toggle",
    ):
        assert f"export function {component}" in jsx
    assert "var(--color-primary)" in jsx


def test_react_tailwind_config_maps_variables() -> None:
    config = generate_files("react-tailwind", "demo", _TOKENS)["tailwind.config.js"]
    assert "var(--color-primary)" in config
    assert "theme" in config and "extend" in config


def test_bundle_zip_contains_all_files() -> None:
    data = bundle_zip("html-css", "demo", _TOKENS)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
    catalog = [f"demo-html-css/{item['filename']}" for item in component_catalog("html-css")]
    assert sorted(names) == sorted(catalog)


def test_codegen_summary_counts() -> None:
    summary = codegen_summary(_TOKENS)
    assert summary["variables"] > 20
    assert summary["components"] == 11
    assert summary["files"] == 6  # 4 base files + motion.js + scene.js (issue #302)


def test_codegen_targets_documented() -> None:
    assert set(CODEGEN_TARGETS) == {"html-css", "react-tailwind"}
    for info in CODEGEN_TARGETS.values():
        assert info["label"] and info["description"]


# ---------------------------------------------------------------------------
# Contrast gate extension (issue #358): text-safe roles in the showcase and
# the generated code
# ---------------------------------------------------------------------------


def test_rich_tokens_carries_text_roles_in_both_themes() -> None:
    # Every vibe profile's rich tokens carry the derived text roles, and
    # each clears 4.6:1 on its base in both themes.
    for key, profile in VIBE_PROFILES.items():
        tokens = rich_tokens(profile, "both")
        colors = tokens["colors"]
        assert isinstance(colors, dict)
        for section_name in ("light", "dark"):
            section = colors[section_name]
            assert isinstance(section, dict)
            assert contrast_ratio(section["primary_ink"], section["background"]) >= 4.6, (
                key,
                section_name,
            )
            assert contrast_ratio(section["primary_ink"], section["surface"]) >= 4.6, (
                key,
                section_name,
            )
            for role in ("success", "warning", "danger", "info"):
                assert contrast_ratio(section[f"{role}_text"], section[f"{role}_subtle"]) >= 4.6, (
                    key,
                    section_name,
                    role,
                )


def test_showcase_tagline_badges_and_ghost_use_text_roles() -> None:
    # The rendered showcase never puts the raw primary or raw semantic
    # colors on text: tagline/overlines/ghost use the primary ink, badges
    # use the semantic text roles (with a var() fallback so flat/legacy
    # token sets render exactly as before).
    html = render_showcase("demo", VIBE_PROFILES["minimal"], _rich(), "both")
    assert "color:var(--c-primary-ink,var(--c-primary))" in html
    assert "color:var(--c-success-text,var(--c-success))" in html
    assert "color:var(--c-warning-text,var(--c-warning))" in html
    assert "color:var(--c-danger-text,var(--c-danger))" in html
    assert "color:var(--c-info-text,var(--c-info))" in html
    # The dark hero keeps the raw primary: its background is near-black,
    # where the darkened ink would read worse (brutalist, 14.36:1).
    assert ".hero-dark .hero-tagline{color:var(--c-primary)}" in html


def test_showcase_swatch_labels_clear_45_for_flagged_colors() -> None:
    # The exact swatches the #309 scan flagged white-on at 2.68-4.33:1
    # (light pink, mid-greens, the magenta crossover) must each carry a
    # label clearing 4.5:1 in the rendered page.
    import re

    colors = {
        "background": "#FFFFFF",
        "surface": "#FFFFFF",
        "text": "#21121D",
        "text_muted": "#815F78",
        "border": "#E5D3E0",
        "primary": "#DB26AB",
        "primary_hover": "#B91F90",
        "primary_text": "#FFFFFF",
        "accent_100": "#F0D1E8",
        "accent_300": "#DF7CC5",
        "accent_500": "#DB26AB",
        "accent_700": "#DB24AA",
        "accent_900": "#5C1449",
        "neutral_400": "#7E8B7F",
        "neutral_500": "#878980",
        "success": "#16A34A",
        "warning": "#D97706",
        "danger": "#DC2626",
        "info": "#2563EB",
    }
    html = render_showcase("demo", VIBE_PROFILES["minimal"], {"colors": colors}, "light")
    spans = re.findall(
        r'<span class="swatch-color" style="background:([^;]+);color:([^"]+)">([^<]+)</span>',
        html,
    )
    assert spans, "no swatches rendered"
    for bg, fg, label in spans:
        assert contrast_ratio(fg, bg) >= 4.5, (label, fg, bg)
    by_label = {label: (fg, bg) for bg, fg, label in spans}
    # The crossover swatch takes the pure-black fallback, not white.
    assert by_label["#DB24AA"][0] == "#000000"
    assert by_label["#DF7CC5"][0] == "#101418"


def test_codegen_emits_text_roles_and_badges_use_them() -> None:
    # The generated CSS defines the text roles and the badge/ghost/error
    # rules read them (with a var() fallback for hand-written token sets).
    css = css_variables(_TOKENS)
    for role in ("primary-ink", "success-text", "warning-text", "danger-text", "info-text"):
        assert f"--color-{role}:" in css, role
    components = generate_files("html-css", "demo", _TOKENS)["components.css"]
    assert "color: var(--color-success-text, var(--color-success));" in components
    assert "color: var(--color-warning-text, var(--color-warning));" in components
    assert "color: var(--color-danger-text, var(--color-danger));" in components
    assert "color: var(--color-info-text, var(--color-info));" in components
    assert "color: var(--color-primary-ink, var(--color-primary));" in components
    assert "color: var(--color-danger-text, var(--color-danger));" in components
