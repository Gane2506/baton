"""The shared brief -> generation -> gates pipeline (issue #301)."""

from __future__ import annotations

from typing import cast

import pytest

from jev_agent.core.design_brief import DesignBrief, compile_brief
from jev_agent.core.design_business import BusinessContext
from jev_agent.core.design_pipeline import (
    brief_meta,
    force_from_brief,
    gate_violations_for,
    generate_briefed_system,
)
from jev_agent.core.design_skills import generate_seeded_system


def _fintech_brief() -> DesignBrief:
    context = BusinessContext(
        industry="fintech",
        audience="investors",
        tone="professional",
        offering="portfolio analytics",
        description="A fintech dashboard",
    )
    return compile_brief(context, "precise, trustworthy", "dashboard")


def test_fintech_brief_is_honoured_end_to_end() -> None:
    brief = _fintech_brief()
    seeded, gates = generate_briefed_system(brief)
    assert seeded.color_strategy.key == brief.harmony
    assert seeded.type_pairing.key == brief.type_pairing
    assert seeded.layout.key == brief.layout
    assert seeded.motion.key == brief.motion
    assert seeded.components.key == brief.layout_pattern
    assert gates == {"passed": True, "attempts": 1, "violations": []}
    # The same brief twice is the same system (seeded determinism).
    again, _ = generate_briefed_system(brief)
    assert again.fingerprint == seeded.fingerprint
    assert again.palette == seeded.palette


def test_force_from_brief_carries_every_decided_choice() -> None:
    brief = _fintech_brief()
    force = force_from_brief(brief)
    assert force == {
        "harmony": brief.harmony,
        "type_pairing": brief.type_pairing,
        "layout": brief.layout,
        "motion_profile": brief.motion,
        "layout_pattern": brief.layout_pattern,
        "anchor_hue": brief.anchor_hue,
    }


def test_gates_reroll_on_failure_then_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jev_agent.core.design_pipeline as pipeline

    real = pipeline.gate_violations_for
    calls = {"n": 0}

    def _flaky(seeded: object, brief: object, **kwargs: object) -> list[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return ["generic design: every radius is 8px (0.5rem everywhere)"]
        return real(seeded, brief, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pipeline, "gate_violations_for", _flaky)
    brief = _fintech_brief()
    seeded, gates = generate_briefed_system(brief)
    assert calls["n"] == 2
    assert gates == {"passed": True, "attempts": 2, "violations": []}
    # The re-roll moved the seed — and still honoured the brief.
    assert seeded.seed == brief.seed + 1
    assert seeded.color_strategy.key == brief.harmony


def test_gates_reroll_on_fingerprint_collision() -> None:
    brief = _fintech_brief()
    first, _ = generate_briefed_system(brief)
    second, gates = generate_briefed_system(brief, existing_fingerprints={first.fingerprint})
    assert gates["attempts"] == 2
    assert second.seed == brief.seed + 1
    assert second.fingerprint != first.fingerprint
    assert second.color_strategy.key == brief.harmony


def test_gates_exhaustion_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import jev_agent.core.design_pipeline as pipeline

    monkeypatch.setattr(pipeline, "gate_violations_for", lambda *args, **kwargs: ["always failing"])
    with pytest.raises(ValueError, match="quality gates"):
        generate_briefed_system(_fintech_brief(), max_attempts=3)


def test_gate_violations_cover_a_generic_candidate() -> None:
    brief = _fintech_brief()
    seeded = generate_seeded_system(brief.seed, keywords=brief.keywords, app_type="dashboard")
    # A hand-built generic system flags: Inter-only type + purple primary
    # + 8px radii is already two flags, so the detector fires.
    from jev_agent.core.design_gates import generic_detector, is_generic

    score, _flags = generic_detector(
        {
            "primary": "#6366F1",
            "display_stack": '"Inter", system-ui, sans-serif',
            "body_stack": '"Inter", system-ui, sans-serif',
            "radii": {"xs": 8, "sm": 8, "md": 8, "lg": 8},
            "layout_pattern": "hero-led",
        }
    )
    assert is_generic(score)
    # And a real generated candidate passes all three gates.
    assert gate_violations_for(seeded, brief, system_name="test") == []


def test_brief_meta_is_json_safe_and_complete() -> None:
    import json

    meta = brief_meta(_fintech_brief())
    json.dumps(meta)  # must serialise
    assert meta["harmony"] and meta["type_pairing"] and meta["layout"]
    assert meta["scene"] == "data-geometry"
    assert len(cast("list[object]", meta["palette_ramp"])) == 11
    assert set(cast("dict[str, object]", meta["axes"])) == {
        "warmth",
        "contrast",
        "density",
        "geometry",
        "formality",
        "motion",
        "type_personality",
    }
    assert meta["brief_fingerprint"]
