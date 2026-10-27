"""Tests for the Design Studio quality gates (issue #303):

- ``contrast_gate``: WCAG AA in both themes
- ``reduced_motion_gate``: prefers-reduced-motion in CSS/JS
- ``generic_detector`` / ``is_generic``: the AI-default markers
- ``ensure_passing_gates``: the bounded re-roll helper
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from jev_agent.core.design_gates import (
    contrast_gate,
    ensure_passing_gates,
    generic_detector,
    is_generic,
    reduced_motion_gate,
)
from jev_agent.core.design_kit import _palette_from_hue, palette_for_vibe

# A light palette that passes in the light theme (blue primary, dark text,
# muted text dark enough for 4.5:1 on white and on the surface).
_PASSING_LIGHT: dict[str, str] = {
    "primary": "#1D4ED8",
    "primary_hover": "#1E40AF",
    "primary_text": "#FFFFFF",
    "background": "#FFFFFF",
    "surface": "#F1F5F9",
    "text": "#0F172A",
    "text_muted": "#475569",
    "border": "#CBD5E1",
    "success": "#16A34A",
    "warning": "#D97706",
    "danger": "#DC2626",
}

# The same light palette, but the dark theme fails: the muted text is the
# light-theme value, far too dim on a dark background.
_FAILING_DARK: dict[str, str] = {
    "primary": "#1D4ED8",
    "primary_hover": "#3B82F6",
    "primary_text": "#FFFFFF",
    "background": "#0F172A",
    "surface": "#1E293B",
    "text": "#F1F5F9",
    "text_muted": "#475569",
    "border": "#334155",
    "success": "#22C55E",
    "warning": "#F59E0B",
    "danger": "#F87171",
}

_RATIO = re.compile(r"\d+\.\d\d:1")


def _violations_matching(violations: list[str], fragment: str) -> list[str]:
    return [v for v in violations if fragment in v]


# ---------------------------------------------------------------------------
# Contrast gate
# ---------------------------------------------------------------------------


def test_contrast_gate_passing_palette_both_themes() -> None:
    assert contrast_gate(_PASSING_LIGHT) == []
    assert contrast_gate(_PASSING_LIGHT, theme="light") == []
    assert contrast_gate(_PASSING_LIGHT, theme="dark") == []


def test_contrast_gate_kit_palettes_pass_both_themes() -> None:
    # The design kit's own palettes must pass the gate that guards them.
    for keywords in (
        "fintech",
        "playful",
        "health",
        "luxury",
        "food",
        "travel",
        "music",
        "education",
        "eco",
        "social",
        "productivity",
    ):
        assert contrast_gate(palette_for_vibe(keywords)) == [], keywords


def test_contrast_gate_kit_muted_text_gap_fixed() -> None:
    # Issue #309: `_palette_from_hue` set muted text at lightness 0.44,
    # too light for green/cyan hues — health 4.32:1, eco 4.36:1,
    # travel-on-surface 4.18:1. The builder now darkens muted text until
    # it clears 4.6:1 on both bases; the gate must pass for all three.
    for keywords in ("health", "eco", "travel"):
        assert contrast_gate(palette_for_vibe(keywords)) == [], keywords


def test_kit_muted_text_darkened_only_where_it_failed() -> None:
    # The muted derivation now clears 4.6:1 on background, surface, AND
    # the raised surface badges render on (issue #359) — the third base
    # moves even vibes that passed on the first two, slightly, toward the
    # hue's own dark. The hues that failed before move further.
    assert palette_for_vibe("fintech")["text_muted"] == "#5B677B"
    assert palette_for_vibe("playful")["text_muted"] == "#786159"
    assert palette_for_vibe("music")["text_muted"] == "#6E5D7E"
    assert palette_for_vibe("health")["text_muted"] == "#506D5F"
    assert palette_for_vibe("eco")["text_muted"] == "#506D55"
    assert palette_for_vibe("travel")["text_muted"] == "#526B6F"


def test_kit_muted_text_passes_every_hue() -> None:
    # The hash fallback in `palette_for_vibe` can land on any hue, so the
    # builder must clear the gate for all of them, not just the families.
    for hue in range(360):
        palette = _palette_from_hue(float(hue))
        assert contrast_gate(palette) == [], f"hue {hue}"


def test_seeded_playful_seed_6_passes_both_themes() -> None:
    # Issue #360 regression: `generate_seeded_system(6, keywords='playful')`
    # failed the gate — dark `primary_text` on dark `primary_hover` was
    # 2.85:1, because the dark hover was lightened unconditionally to
    # lightness 0.62, past the shared-text limit for its primary. The dark
    # hover is now derived so a shared pick clears 4.5:1 on both states.
    from jev_agent.core.design_skills import generate_seeded_system

    system = generate_seeded_system(6, keywords="playful", app_type="website")
    assert contrast_gate(system.palette) == []


def test_dark_hover_walks_both_directions() -> None:
    # The dark hover cannot only step down: for blue hues the shared pick
    # needs the hover stepped UP (hue 240: dark-on-0.62 is 3.38:1, so the
    # hover must lighten until dark text clears 4.6:1 on both states),
    # while for the seed-6 brown it steps down. Both directions must pass
    # the gate at the 4.5:1 bar.
    from jev_agent.core.design_kit import _dark_variant, contrast_ratio

    for hue in (240.0, 322.0):
        palette = _palette_from_hue(hue)
        dark = _dark_variant(palette)
        text = dark["primary_text"]
        assert contrast_ratio(text, dark["primary"]) >= 4.5, hue
        assert contrast_ratio(text, dark["primary_hover"]) >= 4.5, hue


def test_contrast_gate_muted_text_on_raised_surface() -> None:
    # Issue #359: muted text that clears 4.5:1 on the background (5.41:1)
    # and on the surface (4.87:1) but not on the slightly darker raised
    # surface badges render on (4.33:1) still fails — the gate derives the
    # raised surface the same way the builders do and checks muted on it.
    palette = dict(_SCANNED_HEALTH, text_muted="#806174")
    violations = contrast_gate(palette, theme="light")
    assert _violations_matching(violations, "text_muted on surface_raised")
    assert not _violations_matching(violations, "text_muted on background")
    assert not _violations_matching(violations, "text_muted on surface is")


def test_contrast_gate_fails_dark_only() -> None:
    violations = contrast_gate(_PASSING_LIGHT, dark_palette=_FAILING_DARK)
    assert violations, "expected the dim muted text to fail on dark"
    assert all(v.startswith("dark:") for v in violations)
    muted = _violations_matching(violations, "text_muted on background")
    assert len(muted) == 1
    assert _RATIO.search(muted[0]) is not None
    assert "4.5:1" in muted[0]
    # The same palette passes on its own in the light theme.
    assert contrast_gate(_PASSING_LIGHT, theme="light") == []


def test_contrast_gate_dark_only_theme() -> None:
    violations = contrast_gate(_PASSING_LIGHT, theme="dark", dark_palette=_FAILING_DARK)
    assert violations
    assert all(v.startswith("dark:") for v in violations)


def test_contrast_gate_muted_text_on_surface() -> None:
    # Muted text that clears 4.5:1 on the background but not on the
    # (slightly darker) surface still fails — the gate checks both.
    palette = dict(_PASSING_LIGHT, surface="#475569", text_muted="#5B6B80")
    violations = contrast_gate(palette, theme="light")
    assert _violations_matching(violations, "text_muted on surface")
    assert not _violations_matching(violations, "text_muted on background")


def test_contrast_gate_button_text_below_4_5_to_1() -> None:
    # Mid-grey primary with white button text: ~2.3:1, below the 4.5:1
    # floor button text now carries (issues #359/#360: button labels are
    # text, so the WCAG AA normal-text floor applies, not the 3:1
    # UI-component minimum).
    palette = dict(
        _PASSING_LIGHT,
        primary="#AAAAAA",
        primary_hover="#9CA3AF",
        primary_text="#FFFFFF",
    )
    violations = contrast_gate(palette, theme="light")
    assert _violations_matching(violations, "primary_text on primary")
    assert _violations_matching(violations, "primary_text on primary_hover")
    assert all(_RATIO.search(v) is not None for v in violations)


def test_contrast_gate_body_text_below_4_5_to_1() -> None:
    palette = dict(_PASSING_LIGHT, text="#9CA3AF")
    violations = contrast_gate(palette, theme="light")
    assert _violations_matching(violations, "text on background")
    assert _violations_matching(violations, "text on surface")


def test_contrast_gate_unknown_theme_raises() -> None:
    with pytest.raises(ValueError, match="unknown theme"):
        contrast_gate(_PASSING_LIGHT, theme="sepia")


def test_contrast_gate_palette_without_primary_skips_dark() -> None:
    # No primary means no derivable dark variant; the light check still runs.
    palette = {"text": "#111111", "background": "#FFFFFF"}
    assert contrast_gate(palette) == []


# ---------------------------------------------------------------------------
# Reduced-motion gate
# ---------------------------------------------------------------------------

_CSS_WITH_MOTION = """
.card { transition: transform 200ms ease, opacity 200ms ease; }
.fade { animation: fade-in 300ms ease; }
@keyframes fade-in { from { opacity: 0; } to { opacity: 1; } }
"""

_PASSING_CSS = (
    _CSS_WITH_MOTION
    + """
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
"""
)

_JS_TIMELINE = "function tick() { requestAnimationFrame(tick); }"
_JS_BRANCHED = (
    "const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;\n"
    "function tick() { if (!reduce) requestAnimationFrame(tick); }"
)
_JS_NO_MOTION = "document.querySelector('.btn').addEventListener('click', submit);"


def test_reduced_motion_gate_passing_css() -> None:
    assert reduced_motion_gate(_PASSING_CSS) == []


def test_reduced_motion_gate_missing_block() -> None:
    violations = reduced_motion_gate(_CSS_WITH_MOTION)
    assert len(violations) == 1
    assert "no @media (prefers-reduced-motion: reduce) block" in violations[0]


def test_reduced_motion_gate_commented_out_block_does_not_pass() -> None:
    css = (
        _CSS_WITH_MOTION
        + """
    /* @media (prefers-reduced-motion: reduce) {
      * { animation-duration: 0.01ms !important; }
    } */
    """
    )
    assert reduced_motion_gate(css) == ["CSS has no @media (prefers-reduced-motion: reduce) block"]


def test_reduced_motion_gate_block_without_neutraliser() -> None:
    css = (
        _CSS_WITH_MOTION
        + """
    @media (prefers-reduced-motion: reduce) {
      .card { color: inherit; }
    }
    """
    )
    violations = reduced_motion_gate(css)
    assert _violations_matching(violations, "does not neutralise animation or transition durations")


def test_reduced_motion_gate_animation_only_block() -> None:
    css = (
        _CSS_WITH_MOTION
        + """
    @media (prefers-reduced-motion: reduce) {
      * { animation-duration: 0.01ms !important; }
    }
    """
    )
    violations = reduced_motion_gate(css)
    assert _violations_matching(violations, "transitions have no reduced-motion path")


def test_reduced_motion_gate_transition_only_block() -> None:
    css = (
        _CSS_WITH_MOTION
        + """
    @media (prefers-reduced-motion: reduce) {
      * { transition-duration: 0.01ms !important; }
    }
    """
    )
    violations = reduced_motion_gate(css)
    assert _violations_matching(violations, "animations have no reduced-motion path")


def test_reduced_motion_gate_uncovered_keyframes_flagged() -> None:
    # A duration neutraliser scoped to one selector does not cover other
    # animations: both keyframes are named.
    css = """
    .a { animation: spin 2s linear infinite; }
    .b { animation: pulse 1s ease infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    @keyframes pulse { 50% { opacity: 0.5; } }
    @media (prefers-reduced-motion: reduce) {
      .a { animation-duration: 0.01ms; transition-duration: 0.01ms; }
    }
    """
    violations = reduced_motion_gate(css)
    assert "animation 'spin' has no reduced-motion path" in violations
    assert "animation 'pulse' has no reduced-motion path" in violations


def test_reduced_motion_gate_keyframe_named_in_block_is_covered() -> None:
    css = """
    .fade { animation: fade-in 300ms ease; }
    @keyframes fade-in { from { opacity: 0; } to { opacity: 1; } }
    @media (prefers-reduced-motion: reduce) {
      .fade-in { animation-duration: 0.01ms; transition-duration: 0.01ms; }
    }
    """
    assert reduced_motion_gate(css) == []


def test_reduced_motion_gate_animation_none_covers_keyframes() -> None:
    css = """
    .ticker { animation: scroll 28s linear infinite; }
    @keyframes scroll { to { transform: translateX(-50%); } }
    @media (prefers-reduced-motion: reduce) {
      .ticker { animation: none; transition: none; }
    }
    """
    assert reduced_motion_gate(css) == []


def test_reduced_motion_gate_js_timeline_without_branch() -> None:
    violations = reduced_motion_gate(_PASSING_CSS, js=_JS_TIMELINE)
    assert _violations_matching(violations, "JS motion does not branch")


def test_reduced_motion_gate_js_timeline_with_branch() -> None:
    assert reduced_motion_gate(_PASSING_CSS, js=_JS_BRANCHED) == []


def test_reduced_motion_gate_js_timeline_with_spec_variant() -> None:
    motion: dict[str, object] = {
        "reduced_motion": {"duration_fast": "0ms", "duration_normal": "0ms"}
    }
    assert reduced_motion_gate(_PASSING_CSS, js=_JS_TIMELINE, motion=motion) == []


def test_reduced_motion_gate_js_timeline_spec_without_variant() -> None:
    motion: dict[str, object] = {"duration_fast": "160ms", "duration_normal": "260ms"}
    violations = reduced_motion_gate(_PASSING_CSS, js=_JS_TIMELINE, motion=motion)
    assert _violations_matching(violations, "JS motion does not branch")


def test_reduced_motion_gate_js_without_motion_needs_no_branch() -> None:
    assert reduced_motion_gate(_PASSING_CSS, js=_JS_NO_MOTION) == []


def test_reduced_motion_gate_js_animate_call_is_motion() -> None:
    js = "element.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 300 });"
    assert _violations_matching(
        reduced_motion_gate(_PASSING_CSS, js=js), "JS motion does not branch"
    )


# ---------------------------------------------------------------------------
# Generic detector
# ---------------------------------------------------------------------------

_DISTINCTIVE: dict[str, Any] = {
    "primary": "#C2410C",
    "display_stack": '"Fraunces", Georgia, serif',
    "body_stack": '"Newsreader", Georgia, serif',
    "radii": {"sm": 4, "md": 10, "lg": 20, "full": 9999},
    "layout_pattern": "editorial single column",
    "gradient_text": False,
    "components": ["hero", "chapters", "pull-quote"],
}


def _with(**overrides: Any) -> dict[str, Any]:
    system = dict(_DISTINCTIVE)
    system.update(overrides)
    return system


def test_generic_detector_distinctive_system_scores_zero() -> None:
    score, flags = generic_detector(_DISTINCTIVE)
    assert score == 0
    assert flags == []
    assert not is_generic(score)


def test_generic_detector_default_type_only() -> None:
    score, flags = generic_detector(
        _with(
            display_stack='"Inter", system-ui, sans-serif',
            body_stack='"Inter", system-ui, sans-serif',
        )
    )
    assert score == 1
    assert any("Inter/system-ui" in flag for flag in flags)


def test_generic_detector_characterful_display_face_clears_type_flag() -> None:
    # Inter body is fine when the display face has character.
    score, _flags = generic_detector(_with(body_stack='"Inter", system-ui, sans-serif'))
    assert score == 0


def test_generic_detector_purple_primary_only() -> None:
    score, flags = generic_detector(_with(primary="#6366F1"))
    assert score == 1
    assert any("purple/indigo" in flag for flag in flags)
    assert any("#6366F1" in flag for flag in flags)


def test_generic_detector_muted_purple_does_not_flag() -> None:
    # Same hue band, but desaturated: not the indigo-500 default.
    score, _flags = generic_detector(_with(primary="#8B8FA8"))
    assert score == 0


def test_generic_detector_all_radii_eight_only() -> None:
    score, flags = generic_detector(_with(radii={"sm": 8, "md": 8, "lg": 8, "xl": 8, "full": 9999}))
    assert score == 1
    assert any("0.5rem" in flag for flag in flags)


def test_generic_detector_radii_as_rem_strings() -> None:
    score, _flags = generic_detector(_with(radii={"sm": "0.5rem", "md": "0.5rem", "lg": "0.5rem"}))
    assert score == 1


def test_generic_detector_varied_radii_do_not_flag() -> None:
    score, _flags = generic_detector(_with(radii={"sm": 4, "md": 8, "lg": 16}))
    assert score == 0


def test_generic_detector_three_column_icon_grid_only() -> None:
    score, flags = generic_detector(_with(layout_pattern="3-column icon grid"))
    assert score == 1
    assert any("3-column icon grid" in flag for flag in flags)


def test_generic_detector_three_column_wording_variants() -> None:
    for pattern in ("three-column icon grid", "3 col icon grid"):
        score, _flags = generic_detector(_with(layout_pattern=pattern))
        assert score == 1, pattern


def test_generic_detector_other_grids_do_not_flag() -> None:
    score, _flags = generic_detector(_with(layout_pattern="2-column editorial grid"))
    assert score == 0


def test_generic_detector_gradient_text_only() -> None:
    score, flags = generic_detector(_with(gradient_text=True))
    assert score == 1
    assert any("gradient text" in flag for flag in flags)


def test_generic_detector_all_markers_combined() -> None:
    score, flags = generic_detector(
        _with(
            primary="#6366F1",
            display_stack='"Inter", system-ui, sans-serif',
            body_stack='"Inter", system-ui, sans-serif',
            radii={"sm": 8, "md": 8, "lg": 8},
            layout_pattern="3-column icon grid",
            gradient_text=True,
        )
    )
    assert score == 5
    assert len(flags) == 5
    assert is_generic(score)


def test_is_generic_threshold() -> None:
    assert not is_generic(0)
    assert not is_generic(1)
    assert is_generic(2)
    assert is_generic(5)


# ---------------------------------------------------------------------------
# Re-roll helper
# ---------------------------------------------------------------------------

_SYSTEM: dict[str, object] = {"primary": "#C2410C"}


def test_ensure_passing_gates_succeeds_first_try() -> None:
    system, attempts = ensure_passing_gates(lambda: (_SYSTEM, []))
    assert system == _SYSTEM
    assert attempts == 1


def test_ensure_passing_gates_succeeds_on_second_attempt() -> None:
    calls = {"count": 0}

    def generate() -> tuple[dict[str, object], list[str]]:
        calls["count"] += 1
        if calls["count"] == 1:
            return _SYSTEM, ["light: text on background is 2.10:1 (minimum 4.5:1)"]
        return _SYSTEM, []

    system, attempts = ensure_passing_gates(generate)
    assert system == _SYSTEM
    assert attempts == 2
    assert calls["count"] == 2


def test_ensure_passing_gates_raises_after_exhausting_attempts() -> None:
    calls = {"count": 0}

    def generate() -> tuple[dict[str, object], list[str]]:
        calls["count"] += 1
        return _SYSTEM, ["always failing"]

    with pytest.raises(ValueError, match="in 3 attempts"):
        ensure_passing_gates(generate, max_attempts=3)
    assert calls["count"] == 3


def test_ensure_passing_gates_terminates_at_default_max() -> None:
    calls = {"count": 0}

    def generate() -> tuple[dict[str, object], list[str]]:
        calls["count"] += 1
        return _SYSTEM, ["always failing"]

    with pytest.raises(ValueError, match="in 10 attempts"):
        ensure_passing_gates(generate)
    assert calls["count"] == 10


def test_ensure_passing_gates_rejects_zero_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        ensure_passing_gates(lambda: (_SYSTEM, []), max_attempts=0)


# ---------------------------------------------------------------------------
# Contrast gate, issue #358 extension: primary-as-text, semantic text on
# tint, swatch labels
# ---------------------------------------------------------------------------

# The scanned health system (issue #309 verification report), after the
# #359/#360 walk: the raw primary #DB26AB read 4.31:1 on white, so the
# builder walks the primary to #BD1F83 (white 5.69:1, hover #9A196A 7.72:1)
# and the muted text to #7B5B6F (5.89:1 on white, 4.71:1 on the raised
# surface) — the derived ink is the walked primary itself, 5.69:1 / 5.13:1.
_SCANNED_HEALTH: dict[str, str] = {
    **_PASSING_LIGHT,
    "primary": "#BD1F83",
    "primary_hover": "#9A196A",
    "primary_text": "#FFFFFF",
    "surface": "#F8F1F6",
    "text": "#21121D",
    "text_muted": "#7B5B6F",
    "border": "#E5D3E0",
    "primary_ink": "#BD1F83",
    "success_subtle": "#DCFCE7",
    "success_text": "#117E39",
    "warning_subtle": "#FEF3C7",
    "warning_text": "#A45A05",
    "danger_subtle": "#FEE2E2",
    "danger_text": "#C62020",
    "info": "#2563EB",
    "info_subtle": "#DBEAFE",
    "info_text": "#1A5BEA",
}


def test_contrast_gate_scanned_health_palette_passes() -> None:
    # The exact palette the Mac scan measured, with the derived text roles
    # the fix adds: the gate must pass it in both themes.
    assert contrast_gate(_SCANNED_HEALTH) == []


def test_contrast_gate_primary_ink_too_light_is_a_violation() -> None:
    bad = dict(_SCANNED_HEALTH, primary_ink="#DB26AB")  # the raw primary as text
    violations = contrast_gate(bad, theme="light")
    assert _violations_matching(violations, "primary_ink on background")
    assert _violations_matching(violations, "primary_ink on surface")


def test_contrast_gate_semantic_text_on_subtle() -> None:
    # The raw semantic colors as badge text: warning 2.86, success 3.00,
    # danger 3.95, info 4.24 on their tints — all violations.
    for role, base in (
        ("success", "#16A34A"),
        ("warning", "#D97706"),
        ("danger", "#DC2626"),
        ("info", "#2563EB"),
    ):
        bad = dict(_SCANNED_HEALTH, **{f"{role}_text": base})
        assert _violations_matching(
            contrast_gate(bad, theme="light"), f"{role}_text on {role}_subtle"
        ), role
    # The derived text roles pass.
    assert contrast_gate(_SCANNED_HEALTH, theme="light") == []


def test_contrast_gate_swatch_label_crossover_needs_the_black_fallback() -> None:
    # #DB26AB sits at the white/near-black crossover: white reads 4.31:1,
    # near-black 4.30:1 — neither clears 4.5. The label rule's pure-black
    # fallback (4.88:1) is what makes the gate pass; a white-only pick
    # would fail. The gate pins the rule against the real palette.
    from jev_agent.core.design_kit import label_on_color

    assert label_on_color("#DB26AB") == "#000000"
    assert label_on_color("#DB24AA") == "#000000"
    assert contrast_gate(_SCANNED_HEALTH, theme="light") == []


def test_label_on_color_clears_45_for_every_ramp_color() -> None:
    # The swatch label rule must clear 4.5:1 for every color any ramp can
    # produce — accent steps and neutral steps at every hue — plus the
    # fixed semantic bases. The worst case of max(white, black) is 4.58:1,
    # so the rule always has a passing pick.
    from jev_agent.core.design_kit import _hsl, contrast_ratio, label_on_color

    for hue in range(360):
        for lightness in (0.88, 0.68, 0.50, 0.36, 0.22, 0.97, 0.04):
            color = _hsl(float(hue), 0.72, lightness)
            assert contrast_ratio(label_on_color(color), color) >= 4.5, (hue, lightness)
        for lightness in (0.97, 0.93, 0.88, 0.80, 0.68, 0.52, 0.36, 0.24, 0.14, 0.08, 0.04):
            color = _hsl(float(hue), 0.05, lightness)
            assert contrast_ratio(label_on_color(color), color) >= 4.5, (hue, lightness)
    for base in (
        "#16A34A",
        "#D97706",
        "#DC2626",
        "#2563EB",
        "#22C55E",
        "#F59E0B",
        "#F87171",
        "#60A5FA",
    ):
        assert contrast_ratio(label_on_color(base), base) >= 4.5, base


def test_contrast_gate_kit_palettes_pass_extended_checks_every_hue() -> None:
    # The extended checks ride on the same builders the #309 sweep covers:
    # every hue must pass in both themes with the derived roles included.
    for hue in range(360):
        palette = _palette_from_hue(float(hue))
        assert contrast_gate(palette) == [], f"hue {hue}"
