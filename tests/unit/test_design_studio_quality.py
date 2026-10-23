"""Tests for the Design Studio quality elevation (issue #155):

- ``design_showcase``: vibe profiles + rich token generation
- ``design_showcase_html``: the showcase gallery renderer
- ``design_codegen``: reusable-elements code generation (HTML+CSS, React+Tailwind)
"""

from __future__ import annotations

import io
import zipfile

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
from jev_agent.core.design_showcase import (
    VIBE_PROFILES,
    rich_tokens,
    vibe_for_keywords,
)
from jev_agent.core.design_showcase_html import render_showcase

_TOKENS = {"design_tokens": rich_tokens(vibe_for_keywords("elegant"), "both")}


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
    tokens = _TOKENS["design_tokens"]
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
    typo = _TOKENS["design_tokens"]["typography"]
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
    tokens = _TOKENS["design_tokens"]
    assert len(tokens["spacing"]) >= 8
    assert len(tokens["radii"]) >= 5
    assert len(tokens["shadows"]) >= 4
    assert "ease_in_out" in tokens["motion"]


# ---------------------------------------------------------------------------
# Showcase HTML
# ---------------------------------------------------------------------------


def test_render_showcase_contains_all_sections() -> None:
    profile = vibe_for_keywords("bold")
    html = render_showcase("demo", profile, _TOKENS["design_tokens"], "both")
    for section in ("Palette", "Typography", "Components", "Foundations"):
        assert section in html
    assert "dark" in html.lower() and "light" in html.lower()


def test_render_showcase_embeds_vibe_name() -> None:
    profile = vibe_for_keywords("brutalist")
    html = render_showcase("demo", profile, _TOKENS["design_tokens"], "light")
    assert profile.name in html


def test_render_showcase_theme_toggle_only_when_both() -> None:
    profile = vibe_for_keywords("minimal")
    both = render_showcase("demo", profile, _TOKENS["design_tokens"], "both")
    light = render_showcase("demo", profile, _TOKENS["design_tokens"], "light")
    assert 'id="themeToggle"' in both
    assert 'id="themeToggle"' not in light


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
    assert summary["files"] == 4


def test_codegen_targets_documented() -> None:
    assert set(CODEGEN_TARGETS) == {"html-css", "react-tailwind"}
    for info in CODEGEN_TARGETS.values():
        assert info["label"] and info["description"]
