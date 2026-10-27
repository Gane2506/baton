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
    # Vibes that already passed keep their exact muted color — the fix
    # moves only the hues that failed the gate.
    assert palette_for_vibe("fintech")["text_muted"] == "#5F6C81"
    assert palette_for_vibe("playful")["text_muted"] == "#81685F"
    assert palette_for_vibe("music")["text_muted"] == "#705F81"
    # The three that failed are darkened, not re-hued.
    assert palette_for_vibe("health")["text_muted"] == "#577567"
    assert palette_for_vibe("eco")["text_muted"] == "#57755C"
    assert palette_for_vibe("travel")["text_muted"] == "#597378"


def test_kit_muted_text_passes_every_hue() -> None:
    # The hash fallback in `palette_for_vibe` can land on any hue, so the
    # builder must clear the gate for all of them, not just the families.
    for hue in range(360):
        palette = _palette_from_hue(float(hue))
        assert contrast_gate(palette) == [], f"hue {hue}"


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


def test_contrast_gate_button_text_below_3_to_1() -> None:
    # Mid-grey primary with white button text: ~2.3:1, below the 3:1
    # minimum for UI components.
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
