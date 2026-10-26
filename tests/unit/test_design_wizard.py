"""Tests for `jev design new`: the design_kit content and the CLI wizard flow.

These run against the real design library (`jev_agent.core.design_library`,
built by a parallel workstream) with `Path.home` redirected to a tmp dir, so
nothing touches the real `~/.jev`.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import typer

from jev_agent.cli import design_cmds
from jev_agent.core import design_library
from jev_agent.core.design_kit import (
    APP_TYPES,
    PALETTE_KEYS,
    STACKS,
    contrast_ratio,
    design_system_markdown,
    palette_for_vibe,
    tokens_for_app_type,
)
from jev_agent.core.design_skills import COMPONENT_VARIANTS


@pytest.fixture
def home_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect `~/.jev` (the design library root) into a tmp dir."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _mapping(value: object) -> dict[str, Any]:
    assert isinstance(value, dict)
    return value


# ---- design_kit -------------------------------------------------------------------------------


def test_app_types_have_descriptions() -> None:
    assert set(APP_TYPES) == {"mobile", "website", "pwa", "dashboard"}
    for description in APP_TYPES.values():
        assert isinstance(description, str) and len(description) > 10


def test_stacks_cover_every_type() -> None:
    assert set(STACKS) == set(APP_TYPES)
    for stacks in STACKS.values():
        assert len(stacks) >= 2  # a recommended default plus at least one alternative


def test_palette_is_deterministic() -> None:
    assert palette_for_vibe("fintech") == palette_for_vibe("fintech")
    assert palette_for_vibe("") == palette_for_vibe("")
    assert palette_for_vibe("  FINTECH ") == palette_for_vibe("fintech")
    assert set(palette_for_vibe("anything").keys()) == set(PALETTE_KEYS)


def test_palette_families_differ() -> None:
    fintech = palette_for_vibe("a fintech banking app")
    playful = palette_for_vibe("a playful kids game")
    health = palette_for_vibe("fitness and wellness")
    luxury = palette_for_vibe("luxury watches")
    primaries = {fintech["primary"], playful["primary"], health["primary"]}
    assert len(primaries) == 3
    # luxury is the bespoke near-black + gold palette
    assert luxury["background"] == "#0C0B09"
    assert luxury["primary"] == "#C9A227"


def test_palette_hash_fallback_is_stable_but_varied() -> None:
    first = palette_for_vibe("quantum sprocket logistics")
    assert palette_for_vibe("quantum sprocket logistics") == first
    assert palette_for_vibe("a different odd phrase") != first


def test_tokens_mobile() -> None:
    tokens = tokens_for_app_type("mobile", palette_for_vibe("x"), "light")
    assert _mapping(tokens["touch"])["min_target"] == 44
    assert "safe_area_top" in _mapping(tokens["spacing"])
    assert _mapping(tokens["type_scale"])["base"] == 16


def test_tokens_dashboard_dark_first() -> None:
    tokens = tokens_for_app_type("dashboard", palette_for_vibe("x"), "light")
    assert tokens["color_scheme"] == "dark-first"
    dark_colors = _mapping(tokens["dark_colors"])
    assert dark_colors["background"] != "#FFFFFF"
    chart = tokens["chart_palette"]
    assert isinstance(chart, list) and len(chart) == 5
    assert len(set(chart)) == 5  # five distinct categorical colors
    assert _mapping(tokens["spacing"])["md"] == 8  # density scale, not the default 16
    notes = tokens["notes"]
    assert isinstance(notes, list)
    assert any("tabular" in str(note) for note in notes)


def test_tokens_website_type_forward() -> None:
    tokens = tokens_for_app_type("website", palette_for_vibe("x"), "both")
    type_scale = _mapping(tokens["type_scale"])
    assert "display_lg" in type_scale and "display_xl" in type_scale
    assert "line_heights" in tokens
    assert "platform" not in tokens  # install notes are pwa-only


def test_tokens_pwa_adds_install_notes() -> None:
    tokens = tokens_for_app_type("pwa", palette_for_vibe("x"), "dark")
    assert "display_lg" in _mapping(tokens["type_scale"])  # website-like foundation
    platform = _mapping(tokens["platform"])
    assert "install_prompt" in platform and "splash_note" in platform


def test_tokens_theme_shapes_colors() -> None:
    palette = palette_for_vibe("x")
    light = _mapping(tokens_for_app_type("mobile", palette, "light")["colors"])
    dark = _mapping(tokens_for_app_type("mobile", palette, "dark")["colors"])
    assert light["background"] == "#FFFFFF"
    assert dark["background"] != "#FFFFFF"
    both = _mapping(tokens_for_app_type("mobile", palette, "both")["colors"])
    assert set(both) == {"light", "dark"}


def test_tokens_reject_bad_input() -> None:
    palette = palette_for_vibe("x")
    with pytest.raises(ValueError):
        tokens_for_app_type("smartwatch", palette, "light")
    with pytest.raises(ValueError):
        tokens_for_app_type("mobile", palette, "sepia")
    with pytest.raises(ValueError):
        design_system_markdown("n", "smartwatch", "s", palette, "light")


def test_markdown_has_type_component_set() -> None:
    palette = palette_for_vibe("x")
    cases = {
        "mobile": "Tab bar",
        "dashboard": "Data table",
        "website": "Hero",
        "pwa": "Install prompt",
    }
    for app_type, component in cases.items():
        md = design_system_markdown("Acme", app_type, "Web + Tailwind", palette, "both")
        assert "# Acme" in md
        assert component in md
        assert "Font stack" in md  # wordmark guidance as styled text, never in the mark


# ---- the `design new` CLI ---------------------------------------------------------------------


def test_new_refuses_without_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(typer.Exit):
        design_cmds.new()


def test_new_never_prompts_without_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-TTY refusal happens before any prompt is attempted: with piped
    stdin the wizard must fail fast, never hang waiting for input."""

    def _boom(*args: object, **kwargs: object) -> str:
        raise AssertionError("a prompt was attempted without a TTY")

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("jev_agent.cli.design_cmds.Prompt.ask", _boom)
    with pytest.raises(typer.Exit):
        design_cmds.new()


def test_new_wizard_end_to_end(monkeypatch: pytest.MonkeyPatch, home_tmp: Path) -> None:
    """Scripted answers: dashboard, default stack, an invalid name (re-prompted),
    product, vibe, theme — then every file the wizard promises must exist."""
    answers = iter(["4", "1", "not a valid name!", "acme-dash", "Acme", "fintech", "dark"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        "jev_agent.cli.design_cmds.Prompt.ask", lambda *args, **kwargs: next(answers)
    )
    design_cmds.new()

    system_dir = home_tmp / ".jev" / "design-systems" / "acme-dash"
    expected = [
        "design-system.md",
        "tokens.json",
        "meta.json",
        "logo.svg",
        "favicon.svg",
        "favicon-16.png",
        "favicon-32.png",
        "favicon-180.png",
        "favicon-192.png",
        "favicon-512.png",
    ]
    for filename in expected:
        assert (system_dir / filename).exists(), filename

    meta = json.loads((system_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["name"] == "acme-dash"
    assert meta["app_type"] == "dashboard"
    assert meta["stack"] == "Web + Tailwind"
    assert meta["source"] == "wizard"
    assert meta["version"] == 1

    tokens = json.loads((system_dir / "tokens.json").read_text(encoding="utf-8"))
    assert len(tokens["chart_palette"]) == 5
    assert _mapping(tokens["colors"])["background"] != "#FFFFFF"  # dark theme

    markdown = (system_dir / "design-system.md").read_text(encoding="utf-8")
    assert "acme-dash" in markdown
    # The component set is the seeded variant recorded in meta (issue #299):
    # the markdown lists exactly that variant's components.
    assert "design_seed" in meta and "design_fingerprint" in meta
    choices = meta["design_choices"]
    assert isinstance(choices, dict)
    variant_key = choices["components"]
    variant = next(item for item in COMPONENT_VARIANTS["dashboard"] if item.key == variant_key)
    for component_name, _guidance in variant.components:
        assert f"**{component_name}**" in markdown

    assert "acme-dash" in design_library.list_library()

    # logo.svg parses as XML and carries no text; the 512 favicon is a real PNG.
    ET.fromstring((system_dir / "logo.svg").read_text(encoding="utf-8"))
    png = (system_dir / "favicon-512.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# ---- per-type token differences, JSON safety, contrast ------------------------------------------


def test_tokens_are_json_serializable() -> None:
    palette = palette_for_vibe("fintech")
    for app_type in APP_TYPES:
        for theme in ("light", "dark", "both"):
            json.dumps(tokens_for_app_type(app_type, palette, theme))


def test_token_sets_are_type_specific_not_relabels() -> None:
    """The four app types get genuinely different token shapes, not the same
    dict with a different label."""
    palette = palette_for_vibe("x")
    mobile = tokens_for_app_type("mobile", palette, "both")
    dashboard = tokens_for_app_type("dashboard", palette, "both")
    website = tokens_for_app_type("website", palette, "both")
    pwa = tokens_for_app_type("pwa", palette, "both")

    # mobile-only: touch targets and safe areas
    assert "touch" in mobile and "touch" not in dashboard
    assert "safe_area_top" in _mapping(mobile["spacing"])

    # dashboard-only: dark-first scheme, categorical chart ramp, density spacing
    assert dashboard["color_scheme"] == "dark-first"
    assert "dark_colors" in dashboard and "dark_colors" not in mobile
    assert "chart_palette" in dashboard and "chart_palette" not in website
    assert _mapping(dashboard["spacing"])["md"] < _mapping(mobile["spacing"])["md"]
    assert _mapping(dashboard["type_scale"])["base"] < _mapping(mobile["type_scale"])["base"]

    # website-only: type-forward scale with display sizes and line heights
    assert "line_heights" in website and "line_heights" not in mobile
    assert "display_xl" in _mapping(website["type_scale"])
    assert "display_xl" not in _mapping(mobile["type_scale"])

    # pwa-only: install/splash/offline guidance on top of the website foundation
    assert "platform" in pwa and "platform" not in website


def _vibe_samples() -> list[str]:
    return [
        "fintech banking app",
        "playful kids game",
        "fitness wellness",
        "luxury watches",
        "food truck",
        "travel blog",
        "music podcast",
        "education course",
        "eco garden",
        "social chat",
        "productivity app",
        "quantum sprocket logistics",
        "a different odd phrase",
        "",
    ]


def _palettes_for(vibe: str) -> list[dict[str, Any]]:
    """The light palette plus its dark and both-theme resolutions."""
    light = palette_for_vibe(vibe)
    dark = _mapping(tokens_for_app_type("mobile", light, "dark")["colors"])
    both = _mapping(tokens_for_app_type("mobile", light, "both")["colors"])
    assert set(both) == {"light", "dark"}
    return [light, dark, _mapping(both["light"]), _mapping(both["dark"])]


def test_body_text_contrast_is_aa() -> None:
    for vibe in _vibe_samples():
        for palette in _palettes_for(vibe):
            assert contrast_ratio(palette["text"], palette["background"]) >= 4.5, vibe
            assert contrast_ratio(palette["text_muted"], palette["background"]) >= 3.0, vibe


def test_primary_text_is_readable_on_primary_buttons() -> None:
    """`primary_text` must stay legible on the primary button and its hover
    state: >= 3:1 (WCAG AA for UI components) always, 4.5:1 wherever white
    qualifies."""
    for vibe in _vibe_samples():
        for palette in _palettes_for(vibe):
            on = palette["primary_text"]
            assert contrast_ratio(on, palette["primary"]) >= 3.0, vibe
            assert contrast_ratio(on, palette["primary_hover"]) >= 3.0, vibe


def test_primary_text_prefers_white_when_readable() -> None:
    assert palette_for_vibe("fintech")["primary_text"] == "#FFFFFF"
    # luxury gold cannot carry white text; it gets the near-black fallback
    assert palette_for_vibe("luxury watches")["primary_text"] == "#101418"


def test_markdown_dark_theme_shows_dark_palette() -> None:
    palette = palette_for_vibe("fintech")
    dark_bg = _mapping(tokens_for_app_type("mobile", palette, "dark")["colors"])["background"]
    assert dark_bg != "#FFFFFF"
    md = design_system_markdown("Acme", "mobile", "React Native", palette, "dark")
    assert f"- `background`: {dark_bg}" in md
    assert "primary_text" in md
