"""Motion & 3D as first-class Design Studio output (issue #302).

Covers the motion spec (research bands), the GSAP timeline, the seven
Three.js scene builders (palette-matched, each with a static fallback),
the showcase's live motion/3D section, and the codegen hook (both targets).
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from jev_agent.core.design_business import BusinessContext
from jev_agent.core.design_codegen import (
    bundle_zip,
    codegen_summary,
    component_catalog,
    generate_files,
    motion_files,
)
from jev_agent.core.design_motion3d import (
    SCENE_TYPES,
    build_motion_js,
    build_motion_spec,
    build_scene_js,
    build_scene_spec,
    motion_tokens_from_tokens,
    reduced_motion_css,
    scene_type_for_context,
    scene_type_for_industry,
    showcase_motion3d_script,
)
from jev_agent.core.design_showcase import VIBE_PROFILES, rich_tokens
from jev_agent.core.design_showcase_html import render_showcase

_PALETTE = {
    "primary": "#2563EB",
    "accent_500": "#7C3AED",
    "accent_300": "#A78BFA",
    "accent_100": "#EDE9FE",
    "background": "#FFFFFF",
    "surface": "#F1F5F9",
    "text": "#0F172A",
}

# The skills-module motion token shape (design_skills.py MotionProfile.tokens).
_SKILLS_TOKENS: dict[str, object] = {
    "duration_instant": "90ms",
    "duration_fast": "160ms",
    "duration_normal": "260ms",
    "duration_slow": "420ms",
    "duration_slower": "700ms",
    "duration_stagger": "40ms",
    "ease_standard": "cubic-bezier(0.65, 0, 0.35, 1)",
    "ease_out": "cubic-bezier(0.16, 1, 0.3, 1)",
    "ease_in_out": "cubic-bezier(0.65, 0, 0.35, 1)",
    "ease_spring": "cubic-bezier(0.34, 1.56, 0.64, 1)",
}


def _balanced(source: str) -> bool:
    """Braces/parens/brackets balance — a cheap structural check that the
    generated JS is not truncated mid-template.

    Comments are stripped first: an apostrophe in a comment ("system's")
    is not a string delimiter, and braces in comments are not code.
    """
    import re

    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    pairs = {"{": "}", "(": ")", "[": "]"}
    stack: list[str] = []
    in_string: str | None = None
    escaped = False
    for char in code:
        if in_string is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char in ("'", '"', "`"):
            in_string = char
        elif char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values() and (not stack or stack.pop() != char):
            return False
    return not stack and in_string is None


# ---------------------------------------------------------------------------
# Motion spec — research bands (report §6.2/§6.3/§6.4)
# ---------------------------------------------------------------------------


def _durations(spec: dict[str, object]) -> dict[str, int]:
    """The spec's durations as typed ints (the spec dict itself is untyped)."""
    raw = spec.get("durations")
    assert isinstance(raw, dict)
    return {str(key): int(value) for key, value in raw.items()}


def _section(spec: dict[str, object], key: str) -> dict[str, object]:
    raw = spec.get(key)
    assert isinstance(raw, dict)
    return raw


def _objects(spec: dict[str, object]) -> list[dict[str, object]]:
    raw = spec.get("objects")
    assert isinstance(raw, list)
    return [item for item in raw if isinstance(item, dict)]


def _assert_in_bands(durations: dict[str, int]) -> None:
    assert 50 <= durations["instant"] <= 200  # short
    assert 50 <= durations["fast"] <= 200  # short
    assert 100 <= durations["normal"] <= 500  # most UI 100-500ms
    assert 250 <= durations["slow"] <= 600  # long
    assert 450 <= durations["slower"] <= 1000  # extra-long
    assert durations["hover"] <= 150  # hover completes within ~150ms
    assert 50 <= durations["stagger"] <= 100  # stagger 50-100ms
    assert durations["exit"] < durations["enter"]  # exit quicker than entering


def test_motion_spec_default_within_research_bands() -> None:
    spec = build_motion_spec()
    durations = _durations(spec)
    _assert_in_bands(durations)
    easings = _section(spec, "easings")
    assert easings["standard"] == "cubic-bezier(0.2, 0, 0, 1)"
    assert easings["ease_out"] == "cubic-bezier(0.16, 1, 0.3, 1)"
    assert easings["ease_in"] == "cubic-bezier(0.3, 0, 1, 1)"
    assert spec["temperament"] == "smooth"


@pytest.mark.parametrize(
    "temperament",
    ["snappy", "smooth", "springy", "stately", "gentle", "brisk"],
)
def test_motion_spec_temperaments_within_bands(temperament: str) -> None:
    spec = build_motion_spec(temperament=temperament)
    assert spec["temperament"] == temperament
    _assert_in_bands(_durations(spec))


def test_motion_spec_unknown_temperament_falls_back_to_smooth() -> None:
    assert build_motion_spec(temperament="wobbly")["temperament"] == "smooth"


def test_motion_spec_accepts_skills_token_shape() -> None:
    spec = build_motion_spec(_SKILLS_TOKENS)
    durations = _durations(spec)
    assert durations["instant"] == 90
    assert durations["fast"] == 160
    assert durations["normal"] == 260
    assert durations["slow"] == 420
    assert durations["slower"] == 700
    # The skills module's 40ms stagger is below the research band — clamped up.
    assert durations["stagger"] == 50
    assert _section(spec, "easings")["standard"] == "cubic-bezier(0.65, 0, 0.35, 1)"
    _assert_in_bands(durations)


def test_motion_spec_clamps_out_of_band_tokens() -> None:
    spec = build_motion_spec(
        {
            "duration_normal": "5ms",  # below the 100ms floor
            "duration_slower": "5000ms",  # above the 1000ms ceiling
            "duration_stagger": "250ms",  # above the 100ms stagger ceiling
            "ease_standard": "not-a-bezier",  # malformed: falls back
        }
    )
    durations = _durations(spec)
    assert durations["normal"] == 100
    assert durations["slower"] == 1000
    assert durations["stagger"] == 100
    assert _section(spec, "easings")["standard"] == "cubic-bezier(0.2, 0, 0, 1)"


def test_reduced_motion_css_covers_both_triggers() -> None:
    css = reduced_motion_css()
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert 'html[data-motion="reduced"]' in css
    assert "animation-duration: 0.01ms" in css
    assert "transition-duration: 0.01ms" in css


def test_motion_tokens_from_rich_tokens() -> None:
    tokens = rich_tokens(VIBE_PROFILES["minimal"], "both")
    motion = motion_tokens_from_tokens(tokens)
    assert motion["duration_fast"] == "160ms"
    assert motion["ease_standard"] == "cubic-bezier(0.16, 1, 0.3, 1)"
    assert motion_tokens_from_tokens({}) == {}


# ---------------------------------------------------------------------------
# GSAP timeline — motion.js
# ---------------------------------------------------------------------------


def test_motion_js_structure() -> None:
    js = build_motion_js(build_motion_spec(_SKILLS_TOKENS), system_name="Acme")
    assert "Acme" in js
    # Hero entrance, scroll reveals, page transitions — the three hooks.
    assert "[data-hero]" in js and "[data-hero-item]" in js
    assert "[data-reveal]" in js and "ScrollTrigger" in js
    assert "a[data-transition]" in js
    # Reduced motion + the no-GSAP fallback (never a blank page).
    assert "prefers-reduced-motion" in js
    assert 'data-motion") === "reduced"' in js
    assert "GSAP not loaded" in js
    assert "window.DesignMotion" in js and "replayHero" in js
    # The spec is embedded as JSON, not a second drifting copy.
    assert '"fast": 160' in js
    assert _balanced(js)


def test_motion_js_temperament_and_tokens_agree() -> None:
    snappy = build_motion_js(temperament="brisk", system_name="Acme")
    stately = build_motion_js(temperament="stately", system_name="Acme")
    assert json.loads(
        snappy[snappy.index("var MOTION = ") + len("var MOTION = ") :].split(";", 1)[0]
    )
    assert '"normal": 175' in snappy  # 250 * 0.7
    assert '"normal": 325' in stately  # 250 * 1.3


# ---------------------------------------------------------------------------
# Scenes — one builder per type, palette-matched, with a static fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("industry", "scene_type"),
    [
        ("restaurant", "warm-ambient"),
        ("fintech", "data-geometry"),
        ("saas", "product-orbit"),
        ("ecommerce", "storefront"),
        ("health", "calm-growth"),
        ("education", "campus"),
        ("travel", "journey"),
        ("aerospace", "product-orbit"),
        ("", "product-orbit"),
    ],
)
def test_scene_type_for_industry(industry: str, scene_type: str) -> None:
    assert scene_type_for_industry(industry) == scene_type


def test_scene_type_for_context() -> None:
    assert scene_type_for_context(BusinessContext(industry="fintech")) == "data-geometry"
    assert scene_type_for_context(BusinessContext(industry="restaurant")) == "warm-ambient"
    assert scene_type_for_context(BusinessContext()) == "product-orbit"
    assert scene_type_for_context(None) == "product-orbit"


@pytest.mark.parametrize("scene_type", sorted(SCENE_TYPES))
def test_scene_spec_palette_matched_with_fallback(scene_type: str) -> None:
    spec = build_scene_spec(scene_type, _PALETTE)
    assert spec["type"] == scene_type
    assert spec["label"] and spec["description"]
    colors = _section(spec, "colors")
    assert colors["primary"] == "#2563EB"
    assert colors["accent"] == "#7C3AED"
    objects = _objects(spec)
    assert len(objects) >= 7
    # The primary color is actually used by the composition, not just listed.
    assert any(obj.get("color") == "#2563EB" for obj in objects)
    assert all(obj["kind"] in ("box", "sphere", "cone", "plane") for obj in objects)
    fallback = _section(spec, "fallback")
    assert "#2563EB" in str(fallback["css"])  # primary in the fallback
    assert "#7C3AED" in str(fallback["css"])  # accent in the fallback
    assert fallback["caption"]


def test_scene_specs_differ_per_type() -> None:
    kinds = {
        scene_type: tuple(
            sorted(
                {str(obj.get("kind")) for obj in _objects(build_scene_spec(scene_type, _PALETTE))}
            )
        )
        for scene_type in SCENE_TYPES
    }
    # Not one composition recolored seven times: the kind signatures differ.
    assert len(set(kinds.values())) >= 4
    # And the fallback compositions differ too.
    fallbacks = {
        str(_section(build_scene_spec(scene_type, _PALETTE), "fallback")["css"])
        for scene_type in SCENE_TYPES
    }
    assert len(fallbacks) == len(SCENE_TYPES)


@pytest.mark.parametrize("scene_type", sorted(SCENE_TYPES))
def test_scene_js_structure(scene_type: str) -> None:
    js = build_scene_js(scene_type, _PALETTE, system_name="Acme")
    assert "Acme" in js
    assert SCENE_TYPES[scene_type]["label"] in js
    assert "THREE.WebGLRenderer" in js
    assert "pointerdown" in js  # drag-to-rotate orbit
    assert "prefers-reduced-motion" in js
    assert "WebGL unavailable" in js  # the no-WebGL path shows the fallback
    assert "scene-3d-fallback" in js
    assert "#2563EB" in js  # palette-matched: the primary is in the file
    assert "#7C3AED" in js
    assert _balanced(js)


def test_scene_js_unknown_type_falls_back_to_default() -> None:
    js = build_scene_js("not-a-scene", _PALETTE, system_name="Acme")
    assert "Product orbit" in js


def test_scene_spec_from_tokens() -> None:
    tokens = rich_tokens(VIBE_PROFILES["bold"], "both")
    spec = build_scene_spec(tokens=tokens)
    assert _section(spec, "colors")["primary"]
    assert _objects(spec)


# ---------------------------------------------------------------------------
# Showcase — live motion/3D section
# ---------------------------------------------------------------------------


def _showcase_html(business: BusinessContext | None = None, scene_type: str | None = None) -> str:
    tokens = rich_tokens(VIBE_PROFILES["minimal"], "both")
    return render_showcase(
        "Acme",
        VIBE_PROFILES["minimal"],
        tokens,
        business=business,
        scene_type=scene_type,
    )


def test_render_showcase_motion_section() -> None:
    page = _showcase_html()
    assert 'id="motion-3d"' in page
    assert 'id="scene3d"' in page
    assert 'id="sceneFallback"' in page
    assert 'id="motionToggle"' in page
    assert 'id="replayMotion"' in page
    assert "aria-pressed" in page
    assert "data-motion-hero" in page
    assert "data-reveal" in page
    assert "prefers-reduced-motion" in page
    assert 'data-motion="reduced"' in page
    # The scene container + the toggle + the scripts are all present.
    assert page.count("<script>") >= 2
    assert 'getContext("webgl"' in page
    # Default scene (no business context) is the generic one.
    assert "Product orbit" in page


def test_render_showcase_scene_matches_industry() -> None:
    page = _showcase_html(business=BusinessContext(industry="restaurant"))
    assert "Warm ambient" in page
    page = _showcase_html(business=BusinessContext(industry="fintech"))
    assert "Data geometry" in page
    page = _showcase_html(business=BusinessContext(industry="fintech"), scene_type="campus")
    assert "Campus" in page  # explicit override wins over the industry match


def test_showcase_motion_script_structure() -> None:
    script = showcase_motion3d_script(build_motion_spec(), build_scene_spec("journey", _PALETTE))
    assert "motionToggle" in script
    assert "scene3d" in script
    assert 'getContext("webgl"' in script
    assert "prefers-reduced-motion" in script
    assert "IntersectionObserver" in script
    assert "mat4Perspective" in script
    assert '"normal": 250' in script
    assert '"type": "journey"' in script
    assert _balanced(script)


def test_render_showcase_flat_tokens_still_render_motion() -> None:
    page = render_showcase("Acme", VIBE_PROFILES["minimal"], {"colors": {}})
    assert 'id="motion-3d"' in page
    assert 'id="scene3d"' in page


# ---------------------------------------------------------------------------
# Codegen hook — motion.js + scene.js in both targets
# ---------------------------------------------------------------------------


_TOKENS = rich_tokens(VIBE_PROFILES["minimal"], "both")


@pytest.mark.parametrize("target", ["html-css", "react-tailwind"])
def test_generate_files_includes_motion_and_scene(target: str) -> None:
    files = generate_files(target, "demo", _TOKENS)
    assert "motion.js" in files
    assert "scene.js" in files
    assert "gsap" in files["motion.js"].lower()
    assert "THREE" in files["scene.js"]
    assert files["motion.js"].strip() and files["scene.js"].strip()
    catalog = [item["filename"] for item in component_catalog(target)]
    assert "motion.js" in catalog and "scene.js" in catalog
    assert sorted(files) == sorted(catalog)


@pytest.mark.parametrize("target", ["html-css", "react-tailwind"])
def test_bundle_zip_includes_motion_and_scene(target: str) -> None:
    data = bundle_zip(target, "demo", _TOKENS)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
    assert f"demo-{target}/motion.js" in names
    assert f"demo-{target}/scene.js" in names


def test_codegen_summary_counts_motion_files() -> None:
    assert codegen_summary(_TOKENS)["files"] == 6


def test_motion_files_scene_from_context() -> None:
    files = motion_files("Acme", _TOKENS, context=BusinessContext(industry="restaurant"))
    assert "Warm ambient" in files["scene.js"]
    assert "motion.js" in files and "scene.js" in files


def test_html_demo_page_wires_motion_and_scene() -> None:
    page = generate_files("html-css", "demo", _TOKENS)["index.html"]
    assert 'id="scene-3d"' in page
    assert 'id="scene-3d-fallback"' in page
    assert "gsap@3.12.5" in page
    assert "three@0.160.0" in page
    assert 'src="motion.js"' in page
    assert 'src="scene.js"' in page
    assert "data-hero" in page
