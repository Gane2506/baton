"""Motion & 3D as first-class Design Studio output (issue #302).

Two generated artifacts, both palette-matched and driven by one motion spec:

- ``motion.js`` — a GSAP timeline (hero entrance, scroll reveals, page
  transitions) built from the research-backed motion tokens (report §6).
- ``scene.js`` — a Three.js scene (one of seven industry-matched types)
  with drag-to-rotate orbit, a static CSS fallback for no-WebGL or
  reduced-motion, and the same scene spec the showcase's inline renderer
  consumes, so the download and the live preview never disagree.

The motion spec is the single source of truth for durations/easings:

- Durations follow the Material ladder from the research (§6.2): short
  50-200ms, medium 250-400ms, long 450-600ms, extra-long 700-1000ms;
  most UI motion stays 100-500ms and hover completes within ~150ms.
- Easing is one curve per job (§6.3): ease-out for entering, ease-in for
  exiting, and **exits quicker than entrances**. Standard is
  ``cubic-bezier(0.2, 0, 0, 1)``.
- Stagger is 50-100ms per item (§6.4); only ``transform`` and ``opacity``
  are ever animated (GPU-accelerated, 60fps).
- ``prefers-reduced-motion`` is honoured everywhere (§6.5): the reduced
  CSS block kills decorative motion while state changes still happen,
  instantly, and every scene ships a static fallback.

Builders accept either a motion-tokens dict (the shape the skills module
``design_skills.py`` produces — ``duration_instant/fast/normal/slow/slower``,
``duration_stagger``, ``ease_standard/ease_out/ease_in_out/ease_spring``)
or a temperament name, so they work with or without that module. Values
from a tokens dict are clamped into the research bands, never trusted raw.

Pure functions, stdlib-only, like ``design_kit.py``. The generated JS is
plain strings — validate with ``node --check`` after editing the templates.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jev_agent.core.design_business import BusinessContext

# ---------------------------------------------------------------------------
# Research bands (report §6.2/§6.4) — every duration the builders emit lands
# inside one of these, whatever the caller passes in.
# ---------------------------------------------------------------------------

SHORT_MS: tuple[int, ...] = (50, 100, 150, 200)
MEDIUM_MS: tuple[int, ...] = (250, 300, 350, 400)
LONG_MS: tuple[int, ...] = (450, 500, 550, 600)
EXTRA_LONG_MS: tuple[int, ...] = (700, 800, 900, 1000)
HOVER_MAX_MS = 150  # hover must complete within ~150ms or it feels laggy
STAGGER_MIN_MS = 50  # stagger 50-100ms per item (§6.4)
STAGGER_MAX_MS = 100
UI_CEILING_MS = 500  # most UI motion stays under this; slower is page-level

EASE_STANDARD = "cubic-bezier(0.2, 0, 0, 1)"  # M3 standard (§6.3)
EASE_ACCELERATE = "cubic-bezier(0.3, 0, 1, 1)"  # exits
EASE_DECELERATE = "cubic-bezier(0, 0, 0, 1)"  # entrances
EASE_OUT = "cubic-bezier(0.16, 1, 0.3, 1)"  # entering / hero
EASE_IN = "cubic-bezier(0.3, 0, 1, 1)"  # exiting (same curve as accelerate)
EASE_IN_OUT = "cubic-bezier(0.65, 0, 0.35, 1)"
EASE_SPRING = "cubic-bezier(0.34, 1.56, 0.64, 1)"  # overshoot — one moment only

_BEZIER_RE = re.compile(
    r"^cubic-bezier\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)$"
)
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# (scale, stagger_ms) per temperament — the same names the skills module
# uses, so a temperament chosen there means the same thing here.
_TEMPERAMENTS: dict[str, tuple[float, int]] = {
    "snappy": (0.8, 50),
    "smooth": (1.0, 60),
    "springy": (1.0, 50),
    "stately": (1.3, 80),
    "gentle": (1.15, 70),
    "brisk": (0.7, 50),
}
DEFAULT_TEMPERAMENT = "smooth"

# The default ladder: instant/fast/normal/slow/slower + the two derived
# timings every timeline needs (enter/exit) and the hover budget.
_BASE_DURATIONS: dict[str, int] = {
    "instant": 100,
    "fast": 150,
    "normal": 250,
    "slow": 450,
    "slower": 700,
    "hover": 120,
    "enter": 300,
    "exit": 240,  # exits quicker than entrances (§6.3)
}

# (low, high) clamp band per duration key — the research bands above.
_DURATION_BANDS: dict[str, tuple[int, int]] = {
    "instant": (50, 200),
    "fast": (50, 200),
    "normal": (100, 500),
    "slow": (250, 600),
    "slower": (450, 1000),
    "hover": (50, HOVER_MAX_MS),
    "enter": (100, 500),
    "exit": (50, 500),
}


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _parse_ms(value: object, default: int) -> int:
    """A motion-token value as integer milliseconds.

    Accepts ``160``, ``"160ms"`` and ``"0.16s"``; anything else falls back
    to `default` instead of raising — a bad token must never break the build.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip().lower()
        try:
            if text.endswith("ms"):
                return int(float(text[:-2]))
            if text.endswith("s"):
                return int(float(text[:-1]) * 1000)
            return int(float(text))
        except ValueError:
            return default
    return default


def _parse_ease(value: object, default: str) -> str:
    """A ``cubic-bezier(...)`` string, or `default` when malformed."""
    if isinstance(value, str) and _BEZIER_RE.match(value.strip()):
        return value.strip()
    return default


def build_motion_spec(
    motion_tokens: dict[str, object] | None = None,
    temperament: str | None = None,
) -> dict[str, object]:
    """The motion spec: durations, easings, stagger — all inside the
    research bands.

    `motion_tokens` is the skills-module shape (``duration_*`` / ``ease_*``
    keys); `temperament` is one of ``snappy/smooth/springy/stately/gentle/
    brisk``. A tokens dict wins over the temperament for the keys it sets;
    anything missing falls back to the temperament-scaled ladder, then the
    defaults. Every duration is clamped into its band, and the exit is
    forced quicker than the entrance.
    """
    name = (temperament or "").strip().lower()
    if name not in _TEMPERAMENTS:
        name = DEFAULT_TEMPERAMENT
    scale, stagger = _TEMPERAMENTS[name]
    durations = {
        key: _clamp(round(value * scale), *_DURATION_BANDS[key])
        for key, value in _BASE_DURATIONS.items()
    }
    durations["stagger"] = stagger
    easings = {
        "standard": EASE_STANDARD,
        "accelerate": EASE_ACCELERATE,
        "decelerate": EASE_DECELERATE,
        "ease_out": EASE_OUT,
        "ease_in": EASE_IN,
        "ease_in_out": EASE_IN_OUT,
        "spring": EASE_SPRING,
    }
    if name == "springy":
        easings["ease_out"] = EASE_SPRING
    if motion_tokens:
        for key in ("instant", "fast", "normal", "slow", "slower"):
            raw = motion_tokens.get(f"duration_{key}")
            if raw is not None:
                low, high = _DURATION_BANDS[key]
                durations[key] = _clamp(_parse_ms(raw, durations[key]), low, high)
        raw_stagger = motion_tokens.get("duration_stagger")
        if raw_stagger is not None:
            durations["stagger"] = _clamp(
                _parse_ms(raw_stagger, stagger), STAGGER_MIN_MS, STAGGER_MAX_MS
            )
        easings["standard"] = _parse_ease(motion_tokens.get("ease_standard"), easings["standard"])
        easings["ease_out"] = _parse_ease(motion_tokens.get("ease_out"), easings["ease_out"])
        easings["ease_in_out"] = _parse_ease(
            motion_tokens.get("ease_in_out"), easings["ease_in_out"]
        )
        easings["spring"] = _parse_ease(motion_tokens.get("ease_spring"), easings["spring"])
    # Exit quicker than entering, always (§6.3) — even a caller-supplied
    # pair that got it backwards.
    if durations["exit"] >= durations["enter"]:
        durations["exit"] = max(50, round(durations["enter"] * 0.8))
    return {
        "temperament": name,
        "durations": durations,
        "easings": easings,
        "stagger_ms": durations["stagger"],
    }


def reduced_motion_css() -> str:
    """The ``prefers-reduced-motion`` block (§6.5), plus the matching
    ``html[data-motion="reduced"]`` rules the reduced-motion toggle drives.

    Decorative motion collapses to ~instant; state changes still happen —
    functional feedback is preserved, only the movement goes away.
    """
    rules = (
        "  *, *::before, *::after {\n"
        "    animation-duration: 0.01ms !important;\n"
        "    animation-delay: 0s !important;\n"
        "    animation-iteration-count: 1 !important;\n"
        "    transition-duration: 0.01ms !important;\n"
        "    transition-delay: 0s !important;\n"
        "    scroll-behavior: auto !important;\n"
        "  }"
    )
    return (
        "@media (prefers-reduced-motion: reduce) {\n" + rules + "\n}\n"
        'html[data-motion="reduced"] {\n' + rules + "\n}\n"
    )


def motion_tokens_from_tokens(tokens: dict[str, object]) -> dict[str, object]:
    """The motion-token dict from a token set, or {} when absent.

    Rich tokens carry it under ``design_tokens.motion``; flat sets under
    ``motion``. Either way the shape is the skills-module one
    (``duration_*`` / ``ease_*``), so `build_motion_spec` accepts it as-is.
    """
    rich = tokens.get("design_tokens")
    if isinstance(rich, dict) and isinstance(rich.get("motion"), dict):
        return dict(rich["motion"])
    flat = tokens.get("motion")
    return dict(flat) if isinstance(flat, dict) else {}


# ---------------------------------------------------------------------------
# GSAP timeline builder — motion.js
# ---------------------------------------------------------------------------

_MOTION_JS_TEMPLATE = """\
/* Motion for __NAME__ — generated by Baton Design Studio (issue #302).
   GSAP timeline: hero entrance, scroll reveals, page transitions.
   Durations/easings come from the system's motion tokens (Material ladder:
   short 50-200ms, medium 250-400ms, long 450-600ms, extra-long 700-1000ms;
   hover < 150ms; stagger 50-100ms; transform/opacity only).

   Requires GSAP 3.12+ and ScrollTrigger for the scroll reveals:
     <script src="https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js"><\\/script>
     <script src="https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/ScrollTrigger.min.js"><\\/script>
     <script src="motion.js" defer><\\/script>
   Self-host the two GSAP files or swap the CDN for your own bundle — the
   loader above is the only CDN dependency. Without GSAP this file no-ops
   and content renders in its final state (never a blank page).

   Hooks (add them to your markup):
     [data-hero]        hero container — its [data-hero-item] children enter
                        staggered, opacity + a 10px rise, ease-out.
     [data-reveal]      any element that fades/slides in when scrolled to.
     a[data-transition] same-site links that fade the page out (quicker than
                        the entrance) before navigating.
   Reduced motion: honoured via prefers-reduced-motion and via
   <html data-motion="reduced"> — both skip the timeline and show final states.
   window.DesignMotion.replayHero() re-runs the hero entrance (e.g. from a
   "Replay" button). */
(function () {
  "use strict";
  var MOTION = __MOTION_JSON__;
  var D = MOTION.durations;
  var E = MOTION.easings;

  function reducedMotion() {
    return (
      window.matchMedia("(prefers-reduced-motion: reduce)").matches ||
      document.documentElement.getAttribute("data-motion") === "reduced"
    );
  }

  function heroTimeline() {
    var hero = document.querySelector("[data-hero]");
    if (!hero) return null;
    var items = hero.querySelectorAll("[data-hero-item]");
    if (!items.length) return null;
    var tl = window.gsap.timeline({
      defaults: { ease: E.ease_out, duration: D.enter / 1000 },
    });
    tl.from(items, {
      y: 10,
      opacity: 0,
      duration: D.enter / 1000,
      stagger: D.stagger / 1000,
      clearProps: "transform,opacity",
    });
    return tl;
  }

  function scrollReveals() {
    var targets = window.gsap.utils.toArray("[data-reveal]");
    if (!targets.length) return;
    if (window.ScrollTrigger) {
      window.gsap.registerPlugin(window.ScrollTrigger);
      targets.forEach(function (el) {
        window.gsap.from(el, {
          y: 10,
          opacity: 0,
          duration: D.normal / 1000,
          ease: E.ease_out,
          clearProps: "transform,opacity",
          scrollTrigger: { trigger: el, start: "top 85%", once: true },
        });
      });
    } else if ("IntersectionObserver" in window) {
      // No ScrollTrigger loaded: same reveal, driven by the observer.
      var observer = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (entry) {
            if (!entry.isIntersecting) return;
            window.gsap.to(entry.target, {
              y: 0,
              opacity: 1,
              duration: D.normal / 1000,
              ease: E.ease_out,
              clearProps: "transform,opacity",
            });
            observer.unobserve(entry.target);
          });
        },
        { threshold: 0.15 }
      );
      targets.forEach(function (el) {
        window.gsap.set(el, { y: 10, opacity: 0 });
        observer.observe(el);
      });
    }
  }

  function pageTransitions() {
    document.addEventListener("click", function (event) {
      var link = event.target.closest("a[data-transition]");
      if (!link || link.target === "_blank") return;
      var href = link.getAttribute("href");
      if (!href || href.charAt(0) === "#") return;
      if (reducedMotion()) return; // navigate immediately, no fade
      event.preventDefault();
      window.gsap.to("body", {
        opacity: 0,
        duration: D.exit / 1000,
        ease: E.ease_in,
        onComplete: function () {
          window.location.href = href;
        },
      });
    });
  }

  function init() {
    if (reducedMotion() || typeof window.gsap === "undefined") {
      if (typeof window.gsap === "undefined" && !reducedMotion()) {
        console.warn("[design-motion] GSAP not loaded — motion skipped, content shown as-is.");
      }
      return;
    }
    heroTimeline();
    scrollReveals();
    pageTransitions();
  }

  window.DesignMotion = {
    spec: MOTION,
    replayHero: function () {
      if (reducedMotion() || typeof window.gsap === "undefined") return;
      var tl = heroTimeline();
      if (tl) tl.restart();
    },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
"""


def build_motion_js(
    spec: dict[str, object] | None = None,
    *,
    system_name: str = "",
    motion_tokens: dict[str, object] | None = None,
    temperament: str | None = None,
) -> str:
    """The real ``motion.js``: a GSAP timeline for the hero entrance,
    scroll reveals, and page transitions.

    `spec` is a `build_motion_spec` result; otherwise it is built from
    `motion_tokens` / `temperament`. The spec is embedded as JSON, so the
    durations and easings in the file are exactly the spec's — never a
    second, drifting copy.
    """
    if spec is None:
        spec = build_motion_spec(motion_tokens, temperament)
    name = re.sub(r"\s+", " ", str(system_name)).replace("*/", "* /").strip() or "design system"
    return _MOTION_JS_TEMPLATE.replace("__NAME__", name).replace(
        "__MOTION_JSON__", json.dumps(spec, sort_keys=True)
    )


# ---------------------------------------------------------------------------
# Three.js scene builder — scene.js
# ---------------------------------------------------------------------------

# (label, description, industry profile key it illustrates)
SCENE_TYPES: dict[str, dict[str, str]] = {
    "warm-ambient": {
        "label": "Warm ambient",
        "description": (
            "Floating warm glows over a table plane — the room, before the first plate arrives."
        ),
    },
    "data-geometry": {
        "label": "Data geometry",
        "description": (
            "A bar chart in the round, with a trend line arcing over it — "
            "the numbers, made physical."
        ),
    },
    "product-orbit": {
        "label": "Product orbit",
        "description": (
            "The product at the centre, its satellites in orbit — one object, "
            "everything revolving around it."
        ),
    },
    "storefront": {
        "label": "Storefront",
        "description": ("Shelves of products under a warm light — the shop floor, miniature."),
    },
    "calm-growth": {
        "label": "Calm growth",
        "description": (
            "Slow-growing forms and drifting light — growth you can watch without hurry."
        ),
    },
    "campus": {
        "label": "Campus",
        "description": ("A miniature campus: blocks of different heights around a shared path."),
    },
    "journey": {
        "label": "Journey",
        "description": (
            "Terrain, a route arcing across it, and markers along the way — "
            "the trip, before it starts."
        ),
    },
}

# Industry profile key (design_business._PROFILES) -> scene type.
_PROFILE_SCENES: dict[str, str] = {
    "restaurant": "warm-ambient",
    "fintech": "data-geometry",
    "saas": "product-orbit",
    "ecommerce": "storefront",
    "health": "calm-growth",
    "education": "campus",
    "travel": "journey",
}
DEFAULT_SCENE_TYPE = "product-orbit"

# Fallback hexes when a palette role is missing or malformed.
_SCENE_DEFAULTS: dict[str, str] = {
    "primary": "#2563EB",
    "accent": "#7C3AED",
    "accent_soft": "#BFDBFE",
    "background": "#FFFFFF",
    "surface": "#F1F5F9",
    "ink": "#0F172A",
}


def validate_scene_type(value: str | None) -> str:
    """A known scene type, or the default when unknown/empty."""
    lowered = (value or "").strip().lower()
    return lowered if lowered in SCENE_TYPES else DEFAULT_SCENE_TYPE


def scene_type_for_industry(industry: str | None) -> str:
    """The scene type for a free-text industry, via the business profiles.

    Matches the same way `design_business.match_profile` does (substring
    against the profile keywords); unknown industries get the default.
    """
    lowered = (industry or "").strip().lower()
    if not lowered:
        return DEFAULT_SCENE_TYPE
    for key, keywords in _PROFILE_KEYWORDS:
        if any(word in lowered for word in keywords):
            return _PROFILE_SCENES[key]
    return DEFAULT_SCENE_TYPE


# The profile keywords, mirrored from design_business._PROFILES so this
# module can match an industry without importing the element catalog.
_PROFILE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fintech", ("finance", "bank", "payment", "crypto", "trading", "invest", "fintech")),
    (
        "restaurant",
        ("restaurant", "food", "cafe", "coffee", "bakery", "dining", "menu"),
    ),
    ("saas", ("saas", "software", "b2b", "platform", "subscription")),
    ("ecommerce", ("shop", "store", "ecommerce", "retail", "marketplace", "product")),
    ("health", ("health", "fitness", "wellness", "medical", "clinic", "gym")),
    ("education", ("education", "school", "course", "learn", "student", "teach")),
    ("travel", ("travel", "hotel", "flight", "trip", "tour", "booking")),
)


def scene_type_for_context(context: BusinessContext | None) -> str:
    """The scene type for a business context (industry + offering +
    description), or the default when the context is empty/unknown."""
    if context is None or context.is_empty():
        return DEFAULT_SCENE_TYPE
    from jev_agent.core.design_business import match_profile

    profile = match_profile(context)
    return _PROFILE_SCENES.get(profile or "", DEFAULT_SCENE_TYPE)


def _scene_hex(value: object, fallback: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text.upper() if _HEX_RE.match(text) else fallback


def palette_from_tokens(tokens: dict[str, object]) -> dict[str, str]:
    """The flat light palette a scene is built from.

    Rich tokens carry it under ``design_tokens.colors.light``; flat sets
    under ``colors``. Missing roles fall back to neutral defaults — a scene
    is never unbuildable for lack of a token.
    """
    palette: dict[str, str] = {}
    rich = tokens.get("design_tokens")
    if isinstance(rich, dict):
        colors = rich.get("colors")
        if isinstance(colors, dict):
            light = colors.get("light")
            if isinstance(light, dict):
                palette = {str(k): str(v) for k, v in light.items()}
    if not palette:
        colors = tokens.get("colors")
        if isinstance(colors, dict):
            nested = colors.get("light")
            if isinstance(nested, dict):
                palette = {str(k): str(v) for k, v in nested.items()}
            else:
                palette = {str(k): str(v) for k, v in colors.items()}
    return palette


def _scene_colors(palette: dict[str, str]) -> dict[str, str]:
    """The six roles a scene uses, sanitized to ``#RRGGBB``."""
    accent = palette.get("accent_500") or palette.get("accent") or palette.get("primary")
    accent_soft = palette.get("accent_100") or palette.get("accent_300") or palette.get("accent")
    return {
        "primary": _scene_hex(palette.get("primary"), _SCENE_DEFAULTS["primary"]),
        "accent": _scene_hex(accent, _SCENE_DEFAULTS["accent"]),
        "accent_soft": _scene_hex(accent_soft, _SCENE_DEFAULTS["accent_soft"]),
        "background": _scene_hex(palette.get("background"), _SCENE_DEFAULTS["background"]),
        "surface": _scene_hex(palette.get("surface"), _SCENE_DEFAULTS["surface"]),
        "ink": _scene_hex(palette.get("text"), _SCENE_DEFAULTS["ink"]),
    }


def _obj(
    kind: str,
    pos: tuple[float, float, float],
    size: float | tuple[float, float, float],
    color: str,
    *,
    spin: bool = False,
    bob: bool = False,
    orbit: dict[str, float] | None = None,
) -> dict[str, object]:
    obj: dict[str, object] = {
        "kind": kind,
        "pos": list(pos),
        "size": list(size) if isinstance(size, tuple) else size,
        "color": color,
    }
    if spin:
        obj["spin"] = True
    if bob:
        obj["bob"] = True
    if orbit is not None:
        obj["orbit"] = orbit
    return obj


def _scene_objects(scene_type: str, colors: dict[str, str]) -> list[dict[str, object]]:
    """The primitive composition for one scene type — the part that makes
    each scene genuinely different, not a recolor."""
    primary, accent, soft = colors["primary"], colors["accent"], colors["accent_soft"]
    surface, ink = colors["surface"], colors["ink"]
    if scene_type == "warm-ambient":
        return [
            _obj("plane", (0, -1.6, 0), (9, 0.15, 5.5), surface),
            _obj("sphere", (-2.2, -0.4, -0.6), 0.9, primary, bob=True),
            _obj("sphere", (0.2, 0.1, 0.4), 1.15, accent, bob=True, spin=True),
            _obj("sphere", (2.3, -0.6, -0.4), 0.7, soft, bob=True),
            _obj("sphere", (-1.0, 0.9, -1.2), 0.45, soft, bob=True),
            _obj("sphere", (1.4, 1.1, -1.0), 0.4, primary, bob=True),
            _obj("sphere", (-2.8, 0.6, 0.8), 0.3, accent, bob=True),
            _obj("sphere", (2.9, 0.5, 0.9), 0.32, soft, bob=True),
        ]
    if scene_type == "data-geometry":
        heights = [0.8, 1.5, 1.1, 2.2, 1.7, 2.8, 2.3]
        objects: list[dict[str, object]] = [
            _obj("plane", (0, -1.6, 0), (9, 0.15, 5.5), surface),
        ]
        for i, height in enumerate(heights):
            x = -3.0 + i * 1.0
            color = primary if i % 2 == 0 else accent
            objects.append(_obj("box", (x, -1.6 + height / 2, 0), (0.7, height, 0.7), color))
        for i, height in enumerate(heights[:5]):
            x = -3.0 + i * 1.0
            objects.append(_obj("sphere", (x, -1.6 + height + 0.55, 0), 0.16, soft, bob=True))
        return objects
    if scene_type == "product-orbit":
        return [
            _obj("plane", (0, -1.6, 0), (9, 0.15, 5.5), surface),
            _obj("box", (0, -0.3, 0), (1.3, 1.3, 1.3), primary, spin=True),
            _obj(
                "sphere",
                (2.2, 0.3, 0),
                0.42,
                accent,
                orbit={"radius": 2.2, "speed": 0.5, "height": 0.3, "tilt": 0.35},
            ),
            _obj(
                "sphere",
                (-1.9, 0.8, 0.6),
                0.3,
                soft,
                orbit={"radius": 1.9, "speed": -0.7, "height": 0.8, "tilt": -0.3},
            ),
            _obj(
                "box",
                (0.4, -0.9, 1.6),
                0.5,
                ink,
                orbit={"radius": 1.7, "speed": 0.9, "height": -0.9, "tilt": 0.2},
            ),
            _obj("sphere", (-2.6, -1.0, -1.0), 0.22, accent, bob=True),
            _obj("sphere", (2.7, -1.1, -0.8), 0.2, soft, bob=True),
        ]
    if scene_type == "storefront":
        return [
            _obj("plane", (0, -1.6, 0), (9, 0.15, 5.5), surface),
            _obj("box", (-1.4, -0.6, -1.2), (4.6, 0.12, 1.0), ink),
            _obj("box", (-1.4, 0.7, -1.2), (4.6, 0.12, 1.0), ink),
            _obj("box", (-3.0, -0.05, -1.2), (0.7, 0.95, 0.7), primary),
            _obj("box", (-1.9, -0.1, -1.2), (0.7, 0.85, 0.7), accent),
            _obj("box", (-0.8, 0.0, -1.2), (0.7, 1.05, 0.7), soft),
            _obj("box", (-2.6, 1.25, -1.2), (0.7, 0.85, 0.7), accent),
            _obj("box", (-1.5, 1.2, -1.2), (0.7, 0.95, 0.7), primary),
            _obj("box", (2.2, -0.9, 0.6), (1.6, 1.3, 1.0), primary, spin=True),
            _obj("sphere", (2.2, 0.1, 0.6), 0.35, soft, bob=True),
        ]
    if scene_type == "calm-growth":
        return [
            _obj("plane", (0, -1.6, 0), (9, 0.15, 5.5), surface),
            _obj("cone", (-2.4, -0.7, -0.5), (0.9, 1.9, 0.9), primary, bob=True),
            _obj("cone", (-0.9, -0.45, 0.3), (0.8, 2.3, 0.8), accent, bob=True),
            _obj("cone", (0.7, -0.6, -0.3), (0.85, 2.0, 0.85), primary, bob=True),
            _obj("cone", (2.3, -0.75, 0.4), (0.75, 1.7, 0.75), soft, bob=True),
            _obj("sphere", (-0.9, 1.15, 0.3), 0.3, soft, bob=True),
            _obj("sphere", (0.7, 0.95, -0.3), 0.26, accent, bob=True),
            _obj("sphere", (-2.4, 0.75, -0.5), 0.24, primary, bob=True),
            _obj("sphere", (1.8, 1.3, -1.2), 0.18, soft, bob=True),
            _obj("sphere", (-1.8, 1.5, -1.4), 0.16, accent, bob=True),
        ]
    if scene_type == "campus":
        return [
            _obj("plane", (0, -1.6, 0), (9, 0.15, 5.5), surface),
            _obj("box", (-2.6, -0.5, -0.8), (1.4, 2.2, 1.4), primary),
            _obj("box", (-0.9, -0.85, -0.8), (1.4, 1.5, 1.4), accent),
            _obj("box", (0.8, -0.35, -0.8), (1.4, 2.5, 1.4), primary),
            _obj("box", (2.5, -0.7, -0.8), (1.4, 1.9, 1.4), soft),
            _obj("box", (0, -1.45, 1.4), (7.0, 0.1, 0.9), soft),
            _obj("cone", (-2.6, 1.0, -0.8), (0.5, 0.8, 0.5), accent, spin=True),
            _obj("sphere", (0.8, 1.35, -0.8), 0.3, soft, bob=True),
            _obj("sphere", (2.5, 0.75, -0.8), 0.24, primary, bob=True),
        ]
    # journey
    return [
        _obj("plane", (0, -1.6, 0), (9, 0.15, 5.5), surface),
        _obj("box", (-2.8, -1.3, -1.0), (1.2, 0.6, 1.2), soft),
        _obj("box", (-1.2, -1.2, 0.6), (1.4, 0.8, 1.4), surface),
        _obj("box", (0.6, -1.35, -0.8), (1.3, 0.5, 1.3), soft),
        _obj("box", (2.4, -1.25, 0.4), (1.5, 0.7, 1.5), surface),
        _obj("sphere", (-2.9, -0.6, 0.2), 0.22, accent, bob=True),
        _obj("sphere", (-1.5, -0.3, 0.9), 0.22, accent, bob=True),
        _obj("sphere", (0.0, -0.1, 0.2), 0.22, accent, bob=True),
        _obj("sphere", (1.5, 0.1, 0.8), 0.22, accent, bob=True),
        _obj("sphere", (2.8, -0.4, 0.0), 0.22, accent, bob=True),
        _obj("cone", (-2.9, 0.1, 0.2), (0.45, 0.9, 0.45), primary),
        _obj("cone", (2.8, 0.3, 0.0), (0.45, 0.9, 0.45), primary),
        _obj("sphere", (2.6, 1.6, -1.6), 0.55, soft, bob=True),
    ]


def static_fallback_css(scene_type: str, colors: dict[str, str]) -> str:
    """The static fallback: a CSS gradient/pattern composition in the
    scene's own palette, shown when WebGL is unavailable or motion is
    reduced. Every scene gets its own composition — not one shared wash."""
    primary, accent, soft = colors["primary"], colors["accent"], colors["accent_soft"]
    background, surface = colors["background"], colors["surface"]
    compositions = {
        "warm-ambient": (
            f"radial-gradient(180px 120px at 30% 35%, {accent}59, transparent 70%), "
            f"radial-gradient(220px 160px at 70% 60%, {primary}4D, transparent 70%), "
            f"radial-gradient(120px 90px at 55% 20%, {soft}66, transparent 70%), "
            f"linear-gradient(160deg, {background}, {surface})"
        ),
        "data-geometry": (
            f"linear-gradient(0deg, {primary} 0 18%, transparent 18%), "
            f"linear-gradient(0deg, {accent} 0 32%, transparent 32%), "
            f"linear-gradient(0deg, {primary} 0 46%, transparent 46%), "
            f"linear-gradient(0deg, {accent} 0 60%, transparent 60%), "
            f"linear-gradient(180deg, {background}, {surface})"
        ),
        "product-orbit": (
            f"radial-gradient(circle at 50% 50%, {primary} 0 62px, transparent 63px), "
            f"radial-gradient(circle at 50% 50%, transparent 0 110px, {accent}40 111px, "
            f"transparent 112px), "
            f"radial-gradient(circle at 50% 50%, transparent 0 150px, {soft}66 151px, "
            f"transparent 152px), "
            f"linear-gradient(160deg, {background}, {surface})"
        ),
        "storefront": (
            f"linear-gradient(0deg, {surface} 0 26%, transparent 26%), "
            f"linear-gradient(0deg, {surface} 0 52%, transparent 52%), "
            f"repeating-linear-gradient(90deg, {primary}33 0 46px, {accent}33 46px 92px, "
            f"transparent 92px 138px), "
            f"linear-gradient(180deg, {background}, {surface})"
        ),
        "calm-growth": (
            f"radial-gradient(200px 140px at 25% 70%, {primary}40, transparent 70%), "
            f"radial-gradient(180px 130px at 60% 65%, {accent}38, transparent 70%), "
            f"radial-gradient(140px 100px at 82% 60%, {soft}66, transparent 70%), "
            f"linear-gradient(180deg, {background}, {surface})"
        ),
        "campus": (
            f"linear-gradient(90deg, {primary}59 0 18%, transparent 18%), "
            f"linear-gradient(90deg, transparent 22%, {accent}59 22% 40%, transparent 40%), "
            f"linear-gradient(90deg, transparent 44%, {primary}4D 44% 62%, transparent 62%), "
            f"linear-gradient(90deg, transparent 66%, {soft}99 66% 84%, transparent 84%), "
            f"linear-gradient(180deg, {background}, {surface})"
        ),
        "journey": (
            f"radial-gradient(150px 110px at 82% 18%, {soft}CC, transparent 70%), "
            f"radial-gradient(90px 70px at 45% 45%, {accent}40, transparent 70%), "
            f"linear-gradient(100deg, transparent 0 48%, {primary}59 48% 52%, transparent 52%), "
            f"linear-gradient(180deg, {background} 0 62%, {surface} 62%)"
        ),
    }
    return compositions.get(
        scene_type,
        f"linear-gradient(160deg, {background}, {surface})",
    )


def build_scene_spec(
    scene_type: str | None = None,
    palette: dict[str, str] | None = None,
    *,
    tokens: dict[str, object] | None = None,
) -> dict[str, object]:
    """One scene spec: type, label, description, palette-matched colors,
    the primitive composition, and the static fallback.

    The spec is the shared input to both renderers — the Three.js
    ``scene.js`` and the showcase's inline WebGL engine — so the download
    and the live preview render the same scene. `palette` is a flat color
    dict; pass `tokens` instead to pull the light palette from a token set.
    """
    kind = validate_scene_type(scene_type)
    if palette is None and tokens is not None:
        palette = palette_from_tokens(tokens)
    colors = _scene_colors(palette or {})
    return {
        "type": kind,
        "label": SCENE_TYPES[kind]["label"],
        "description": SCENE_TYPES[kind]["description"],
        "colors": colors,
        "objects": _scene_objects(kind, colors),
        "fallback": {
            "css": static_fallback_css(kind, colors),
            "caption": (
                f"{SCENE_TYPES[kind]['label']} — static preview. "
                "Enable motion (or a WebGL-capable browser) for the live 3D scene."
            ),
        },
    }


_SCENE_JS_TEMPLATE = """\
/* 3D scene for __NAME__ — __SCENE_LABEL__ (generated by Baton Design Studio, issue #302).
   __SCENE_DESC__
   Requires three.js r150+ (global THREE build):
     <script src="https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js"><\\/script>
     <script src="scene.js" defer><\\/script>
   Self-host three.min.js or swap the CDN for your own bundle — that one
   script tag is the only CDN dependency. Drag to orbit; the scene pauses
   when the tab is hidden. Without WebGL or three.js (or with reduced
   motion) the static fallback stays visible — never a blank box.
   Mount points (add them to your markup):
     <div id="scene-3d-wrap" class="scene-wrap">
       <div id="scene-3d"></div>
       <div id="scene-3d-fallback" class="scene-fallback" style="background: ..."></div>
     </div> */
(function () {
  "use strict";
  var SPEC = __SCENE_JSON__;

  function reducedMotion() {
    return (
      window.matchMedia("(prefers-reduced-motion: reduce)").matches ||
      document.documentElement.getAttribute("data-motion") === "reduced"
    );
  }

  function init() {
    var mount = document.getElementById("scene-3d");
    var fallback = document.getElementById("scene-3d-fallback");
    if (!mount) return;
    if (reducedMotion()) return; // the static fallback stays visible
    if (typeof window.THREE === "undefined") {
      console.warn("[design-scene] three.js not loaded — showing the static fallback.");
      return;
    }
    var renderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch (err) {
      console.warn("[design-scene] WebGL unavailable — showing the static fallback.", err);
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(mount.clientWidth || 640, mount.clientHeight || 360);
    mount.appendChild(renderer.domElement);

    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(
      42,
      (mount.clientWidth || 640) / (mount.clientHeight || 360),
      0.1,
      100
    );
    scene.add(new THREE.HemisphereLight(0xffffff, 0x8899aa, 0.9));
    var sun = new THREE.DirectionalLight(0xffffff, 1.1);
    sun.position.set(4, 6, 5);
    scene.add(sun);

    function geometryFor(obj) {
      var s = obj.size;
      if (obj.kind === "box") return new THREE.BoxGeometry(s[0], s[1], s[2]);
      if (obj.kind === "sphere") return new THREE.SphereGeometry(s / 2, 32, 24);
      if (obj.kind === "cone") return new THREE.ConeGeometry(s[0] / 2, s[1], 24);
      return new THREE.PlaneGeometry(s[0], s[2]);
    }

    var meshes = SPEC.objects.map(function (obj) {
      var mesh = new THREE.Mesh(
        geometryFor(obj),
        new THREE.MeshStandardMaterial({ color: obj.color, roughness: 0.55, metalness: 0.08 })
      );
      if (obj.kind === "plane") mesh.rotation.x = -Math.PI / 2;
      mesh.position.set(obj.pos[0], obj.pos[1], obj.pos[2]);
      mesh.userData.spec = obj;
      scene.add(mesh);
      return mesh;
    });

    var yaw = 0.5;
    var pitch = 0.32;
    var targetYaw = yaw;
    var targetPitch = pitch;
    var dragging = false;
    var lastX = 0;
    var lastY = 0;
    var idleTimer = null;
    function armIdle() {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(function () {
        targetYaw += 0.004; // gentle auto-orbit once the user stops dragging
      }, 4000);
    }
    mount.addEventListener("pointerdown", function (event) {
      dragging = true;
      lastX = event.clientX;
      lastY = event.clientY;
      mount.setPointerCapture(event.pointerId);
      clearTimeout(idleTimer);
    });
    mount.addEventListener("pointermove", function (event) {
      if (!dragging) return;
      targetYaw += (event.clientX - lastX) * 0.008;
      targetPitch = Math.max(
        -0.2,
        Math.min(1.1, targetPitch + (event.clientY - lastY) * 0.005)
      );
      lastX = event.clientX;
      lastY = event.clientY;
    });
    function endDrag() {
      dragging = false;
      armIdle();
    }
    mount.addEventListener("pointerup", endDrag);
    mount.addEventListener("pointercancel", endDrag);
    armIdle();

    function onResize() {
      var w = mount.clientWidth || 640;
      var h = mount.clientHeight || 360;
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
    }
    window.addEventListener("resize", onResize);

    var clock = new THREE.Clock();
    var running = true;
    document.addEventListener("visibilitychange", function () {
      running = !document.hidden;
      if (running) renderer.setAnimationLoop(tick);
      else renderer.setAnimationLoop(null);
    });

    function tick() {
      var dt = Math.min(clock.getDelta(), 0.05);
      var t = clock.elapsedTime;
      yaw += (targetYaw - yaw) * 0.08;
      pitch += (targetPitch - pitch) * 0.08;
      var radius = 8.2;
      camera.position.set(
        radius * Math.sin(yaw) * Math.cos(pitch),
        1.2 + radius * Math.sin(pitch),
        radius * Math.cos(yaw) * Math.cos(pitch)
      );
      camera.lookAt(0, -0.3, 0);
      meshes.forEach(function (mesh, i) {
        var obj = mesh.userData.spec;
        if (obj.spin) mesh.rotation.y += dt * 0.4;
        if (obj.bob) mesh.position.y = obj.pos[1] + Math.sin(t * 0.9 + i * 0.7) * 0.12;
        if (obj.orbit) {
          var angle = t * obj.orbit.speed + i;
          mesh.position.set(
            Math.cos(angle) * obj.orbit.radius,
            obj.orbit.height,
            Math.sin(angle) * obj.orbit.radius * Math.cos(obj.orbit.tilt)
          );
        }
      });
      renderer.render(scene, camera);
    }
    renderer.setAnimationLoop(tick);
    if (fallback) fallback.classList.add("scene-hidden");
    mount.classList.add("scene-live");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
"""


def build_scene_js(
    scene_type: str | None = None,
    palette: dict[str, str] | None = None,
    *,
    system_name: str = "",
    tokens: dict[str, object] | None = None,
) -> str:
    """The real ``scene.js``: a Three.js scene from the shared scene spec.

    The spec (type, colors, objects, fallback) is embedded as JSON, so the
    file renders exactly the scene `build_scene_spec` describes — the same
    spec the showcase's inline renderer consumes.
    """
    spec = build_scene_spec(scene_type, palette, tokens=tokens)
    name = re.sub(r"\s+", " ", str(system_name)).replace("*/", "* /").strip() or "design system"
    return (
        _SCENE_JS_TEMPLATE.replace("__NAME__", name)
        .replace("__SCENE_LABEL__", str(spec["label"]))
        .replace("__SCENE_DESC__", str(spec["description"]))
        .replace("__SCENE_JSON__", json.dumps(spec, sort_keys=True))
    )


# ---------------------------------------------------------------------------
# Showcase inline runtime — the live motion/3D section
# ---------------------------------------------------------------------------
#
# The showcase is served as a single self-contained file under a strict CSP
# (``SHOWCASE_CONTENT_SECURITY_POLICY``: no third-party origins — see
# ``dashboard/security.py``, issue #200), so GSAP/Three.js cannot be loaded
# from a CDN there, and inlining ~700KB of vendored Three.js into every
# saved ``showcase.html`` was rejected as bloat. The showcase therefore runs
# the same motion spec and the same scene spec through a dependency-free
# inline runtime: Web Animations API + IntersectionObserver for motion, and
# a compact raw-WebGL renderer for the scene. The downloadable
# ``motion.js``/``scene.js`` still use real GSAP/Three.js (see above) — the
# specs are shared, so the preview and the download never disagree.

_SHOWCASE_SCRIPT_TEMPLATE = """\
(function () {
  "use strict";
  var MOTION = __MOTION_JSON__;
  var SPEC = __SCENE_JSON__;
  var D = MOTION.durations;
  var E = MOTION.easings;
  var root = document.documentElement;
  var toggle = document.getElementById("motionToggle");

  function systemReduced() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }
  function reduced() {
    return root.getAttribute("data-motion") === "reduced";
  }
  function applyToggle() {
    if (!toggle) return;
    var isReduced = reduced();
    toggle.setAttribute("aria-pressed", isReduced ? "true" : "false");
    toggle.textContent = isReduced ? "Restore motion" : "Reduce motion";
  }

  /* ---- hero entrance + scroll reveals (Web Animations API) ---- */
  function animateIn(el, delay) {
    el.animate(
      [
        { opacity: 0, transform: "translateY(10px)" },
        { opacity: 1, transform: "translateY(0)" },
      ],
      {
        duration: D.enter,
        delay: delay || 0,
        easing: E.ease_out,
        fill: "both",
      }
    );
  }
  function heroEntrance() {
    if (reduced()) return;
    var items = document.querySelectorAll("[data-motion-hero]");
    items.forEach(function (el, i) {
      animateIn(el, i * D.stagger);
    });
  }
  function scrollReveals() {
    if (reduced() || !("IntersectionObserver" in window)) return;
    var targets = document.querySelectorAll("[data-reveal]");
    targets.forEach(function (el) {
      el.style.opacity = "0";
    });
    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          var el = entry.target;
          el.style.opacity = "";
          animateIn(el, 0);
          observer.unobserve(el);
        });
      },
      { threshold: 0.12 }
    );
    targets.forEach(function (el) {
      observer.observe(el);
    });
  }

  /* ---- compact WebGL scene renderer (no dependencies) ---- */
  var glState = null;
  function hexToRgb(hex) {
    var n = parseInt(hex.slice(1), 16);
    return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  }
  function mat4Perspective(fovy, aspect, near, far) {
    var f = 1 / Math.tan(fovy / 2);
    var nf = 1 / (near - far);
    return [
      f / aspect, 0, 0, 0,
      0, f, 0, 0,
      0, 0, (far + near) * nf, -1,
      0, 0, 2 * far * near * nf, 0,
    ];
  }
  function mat4LookAt(eye, center, up) {
    function sub(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }
    function norm(v) {
      var l = Math.hypot(v[0], v[1], v[2]) || 1;
      return [v[0] / l, v[1] / l, v[2] / l];
    }
    function cross(a, b) {
      return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
      ];
    }
    function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
    var z = norm(sub(eye, center));
    var x = norm(cross(up, z));
    var y = cross(z, x);
    return [
      x[0], y[0], z[0], 0,
      x[1], y[1], z[1], 0,
      x[2], y[2], z[2], 0,
      -dot(x, eye), -dot(y, eye), -dot(z, eye), 1,
    ];
  }
  function mat4Multiply(a, b) {
    var out = new Array(16);
    for (var c = 0; c < 4; c++) {
      for (var r = 0; r < 4; r++) {
        out[c * 4 + r] =
          a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] +
          a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
      }
    }
    return out;
  }
  function mat4Translate(x, y, z) {
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1];
  }
  function mat4RotateY(a) {
    var c = Math.cos(a);
    var s = Math.sin(a);
    return [c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, 0, 0, 0, 1];
  }
  function pushTri(out, a, b, c, normals, color) {
    var pts = [a, b, c];
    for (var i = 0; i < 3; i++) {
      out.positions.push(pts[i][0], pts[i][1], pts[i][2]);
      out.normals.push(normals[i][0], normals[i][1], normals[i][2]);
      out.colors.push(color[0], color[1], color[2]);
    }
  }
  function pushQuad(out, a, b, c, d, normal, color) {
    var n = [normal, normal, normal];
    pushTri(out, a, b, c, n, color);
    pushTri(out, a, c, d, n, color);
  }
  function buildGeometry(obj) {
    var out = { positions: [], normals: [], colors: [] };
    var color = hexToRgb(obj.color);
    var s = obj.size;
    if (obj.kind === "box") {
      var hx = s[0] / 2, hy = s[1] / 2, hz = s[2] / 2;
      pushQuad(out, [-hx,-hy,hz],[hx,-hy,hz],[hx,hy,hz],[-hx,hy,hz], [0,0,1], color);
      pushQuad(out, [hx,-hy,-hz],[-hx,-hy,-hz],[-hx,hy,-hz],[hx,hy,-hz], [0,0,-1], color);
      pushQuad(out, [-hx,hy,hz],[hx,hy,hz],[hx,hy,-hz],[-hx,hy,-hz], [0,1,0], color);
      pushQuad(out, [-hx,-hy,-hz],[hx,-hy,-hz],[hx,-hy,hz],[-hx,-hy,hz], [0,-1,0], color);
      pushQuad(out, [hx,-hy,hz],[hx,-hy,-hz],[hx,hy,-hz],[hx,hy,hz], [1,0,0], color);
      pushQuad(out, [-hx,-hy,-hz],[-hx,-hy,hz],[-hx,hy,hz],[-hx,hy,-hz], [-1,0,0], color);
    } else if (obj.kind === "sphere") {
      var r = s / 2;
      var segU = 18, segV = 12;
      for (var v = 0; v < segV; v++) {
        var t0 = (v / segV) * Math.PI;
        var t1 = ((v + 1) / segV) * Math.PI;
        for (var u = 0; u < segU; u++) {
          var p0 = (u / segU) * Math.PI * 2;
          var p1 = ((u + 1) / segU) * Math.PI * 2;
          var a = [
            r * Math.sin(t0) * Math.cos(p0), r * Math.cos(t0),
            r * Math.sin(t0) * Math.sin(p0),
          ];
          var b = [
            r * Math.sin(t0) * Math.cos(p1), r * Math.cos(t0),
            r * Math.sin(t0) * Math.sin(p1),
          ];
          var c = [
            r * Math.sin(t1) * Math.cos(p1), r * Math.cos(t1),
            r * Math.sin(t1) * Math.sin(p1),
          ];
          var d = [
            r * Math.sin(t1) * Math.cos(p0), r * Math.cos(t1),
            r * Math.sin(t1) * Math.sin(p0),
          ];
          var na = [a[0] / r, a[1] / r, a[2] / r];
          var nb = [b[0] / r, b[1] / r, b[2] / r];
          var nc = [c[0] / r, c[1] / r, c[2] / r];
          var nd = [d[0] / r, d[1] / r, d[2] / r];
          pushTri(out, a, c, b, [na, nc, nb], color);
          pushTri(out, a, d, c, [na, nd, nc], color);
        }
      }
    } else if (obj.kind === "cone") {
      var cr = s[0] / 2, ch = s[1];
      var seg = 20;
      var apex = [0, ch / 2, 0];
      for (var i = 0; i < seg; i++) {
        var a0 = (i / seg) * Math.PI * 2;
        var a1 = ((i + 1) / seg) * Math.PI * 2;
        var pA = [cr * Math.cos(a0), -ch / 2, cr * Math.sin(a0)];
        var pB = [cr * Math.cos(a1), -ch / 2, cr * Math.sin(a1)];
        var n = [Math.cos(a0 + Math.PI / seg), ch / (2 * cr), Math.sin(a0 + Math.PI / seg)];
        var nl = Math.hypot(n[0], n[1], n[2]) || 1;
        var sideNormal = [n[0] / nl, n[1] / nl, n[2] / nl];
        pushTri(out, apex, pA, pB, [sideNormal, sideNormal, sideNormal], color);
        pushTri(out, [0, -ch / 2, 0], pB, pA, [[0, -1, 0], [0, -1, 0], [0, -1, 0]], color);
      }
    } else {
      // plane: a flat slab in the XZ plane, normal +Y
      var hw = s[0] / 2, hd = s[2] / 2;
      pushQuad(out,
        [-hw, 0, -hd], [hw, 0, -hd], [hw, 0, hd], [-hw, 0, hd],
        [0, 1, 0], color);
    }
    return out;
  }
  function initScene() {
    var canvas = document.getElementById("scene3d");
    var fallback = document.getElementById("sceneFallback");
    if (!canvas || reduced()) return false;
    var gl = null;
    try {
      gl = canvas.getContext("webgl", { antialias: true, alpha: true });
    } catch (err) {
      gl = null;
    }
    if (!gl) return false; // the static fallback stays visible
    var vsrc =
      "attribute vec3 aPos; attribute vec3 aNormal; attribute vec3 aColor;" +
      "uniform mat4 uMVP; uniform mat3 uNormal;" +
      "varying vec3 vNormal; varying vec3 vColor;" +
      "void main(){ vNormal = uNormal * aNormal; vColor = aColor;" +
      " gl_Position = uMVP * vec4(aPos, 1.0); }";
    var fsrc =
      "precision mediump float; varying vec3 vNormal; varying vec3 vColor;" +
      "void main(){ vec3 n = normalize(vNormal);" +
      " float diff = max(dot(n, normalize(vec3(0.5, 0.8, 0.6))), 0.0);" +
      " gl_FragColor = vec4(vColor * (0.55 + 0.45 * diff), 1.0); }";
    function compile(type, src) {
      var sh = gl.createShader(type);
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
        console.warn("[design-scene] shader failed", gl.getShaderInfoLog(sh));
        return null;
      }
      return sh;
    }
    var vs = compile(gl.VERTEX_SHADER, vsrc);
    var fs = compile(gl.FRAGMENT_SHADER, fsrc);
    if (!vs || !fs) return false;
    var prog = gl.createProgram();
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return false;
    gl.useProgram(prog);
    gl.enable(gl.DEPTH_TEST);
    var locPos = gl.getAttribLocation(prog, "aPos");
    var locNormal = gl.getAttribLocation(prog, "aNormal");
    var locColor = gl.getAttribLocation(prog, "aColor");
    var locMVP = gl.getUniformLocation(prog, "uMVP");
    var locNormalMat = gl.getUniformLocation(prog, "uNormal");
    var meshes = SPEC.objects.map(function (obj) {
      var geo = buildGeometry(obj);
      var buf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      var stride = 9;
      var data = new Float32Array((geo.positions.length / 3) * stride);
      for (var i = 0; i < geo.positions.length / 3; i++) {
        data[i * stride] = geo.positions[i * 3];
        data[i * stride + 1] = geo.positions[i * 3 + 1];
        data[i * stride + 2] = geo.positions[i * 3 + 2];
        data[i * stride + 3] = geo.normals[i * 3];
        data[i * stride + 4] = geo.normals[i * 3 + 1];
        data[i * stride + 5] = geo.normals[i * 3 + 2];
        data[i * stride + 6] = geo.colors[i * 3];
        data[i * stride + 7] = geo.colors[i * 3 + 1];
        data[i * stride + 8] = geo.colors[i * 3 + 2];
      }
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
      return { obj: obj, buffer: buf, count: geo.positions.length / 3 };
    });
    var yaw = 0.5, pitch = 0.32, targetYaw = 0.5, targetPitch = 0.32;
    var dragging = false, lastX = 0, lastY = 0, idleTimer = null;
    function armIdle() {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(function () { targetYaw += 0.004; }, 4000);
    }
    canvas.addEventListener("pointerdown", function (event) {
      dragging = true; lastX = event.clientX; lastY = event.clientY;
      canvas.setPointerCapture(event.pointerId);
      clearTimeout(idleTimer);
    });
    canvas.addEventListener("pointermove", function (event) {
      if (!dragging) return;
      targetYaw += (event.clientX - lastX) * 0.008;
      targetPitch = Math.max(-0.2, Math.min(1.1, targetPitch + (event.clientY - lastY) * 0.005));
      lastX = event.clientX; lastY = event.clientY;
    });
    function endDrag() { dragging = false; armIdle(); }
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);
    armIdle();
    var start = performance.now();
    var raf = 0;
    var running = true;
    function resize() {
      var w = canvas.clientWidth || 640;
      var h = canvas.clientHeight || 360;
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
      }
      gl.viewport(0, 0, canvas.width, canvas.height);
    }
    function frame(now) {
      raf = requestAnimationFrame(frame);
      if (!running) return;
      resize();
      var t = (now - start) / 1000;
      yaw += (targetYaw - yaw) * 0.08;
      pitch += (targetPitch - pitch) * 0.08;
      var radius = 8.2;
      var eye = [
        radius * Math.sin(yaw) * Math.cos(pitch),
        1.2 + radius * Math.sin(pitch),
        radius * Math.cos(yaw) * Math.cos(pitch),
      ];
      var view = mat4LookAt(eye, [0, -0.3, 0], [0, 1, 0]);
      var proj = mat4Perspective(
        (42 * Math.PI) / 180,
        (canvas.clientWidth || 640) / (canvas.clientHeight || 360),
        0.1, 100
      );
      var vp = mat4Multiply(proj, view);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      meshes.forEach(function (mesh, i) {
        var obj = mesh.obj;
        var angle = obj.spin ? t * 0.4 : 0;
        var pos = obj.pos.slice();
        if (obj.bob) pos[1] = obj.pos[1] + Math.sin(t * 0.9 + i * 0.7) * 0.12;
        if (obj.orbit) {
          var a = t * obj.orbit.speed + i;
          pos = [
            Math.cos(a) * obj.orbit.radius,
            obj.orbit.height,
            Math.sin(a) * obj.orbit.radius * Math.cos(obj.orbit.tilt),
          ];
        }
        var model = mat4Multiply(mat4Translate(pos[0], pos[1], pos[2]), mat4RotateY(angle));
        var mvp = mat4Multiply(vp, model);
        gl.uniformMatrix4fv(locMVP, false, new Float32Array(mvp));
        var c = Math.cos(angle), s = Math.sin(angle);
        gl.uniformMatrix3fv(locNormalMat, false, new Float32Array([c, 0, -s, 0, 1, 0, s, 0, c]));
        gl.bindBuffer(gl.ARRAY_BUFFER, mesh.buffer);
        gl.enableVertexAttribArray(locPos);
        gl.enableVertexAttribArray(locNormal);
        gl.enableVertexAttribArray(locColor);
        gl.vertexAttribPointer(locPos, 3, gl.FLOAT, false, 36, 0);
        gl.vertexAttribPointer(locNormal, 3, gl.FLOAT, false, 36, 12);
        gl.vertexAttribPointer(locColor, 3, gl.FLOAT, false, 36, 24);
        gl.drawArrays(gl.TRIANGLES, 0, mesh.count);
      });
    }
    document.addEventListener("visibilitychange", function () {
      running = !document.hidden;
    });
    raf = requestAnimationFrame(frame);
    canvas.classList.add("scene-live");
    if (fallback) fallback.classList.add("scene-hidden");
    glState = {
      stop: function () {
        cancelAnimationFrame(raf);
        clearTimeout(idleTimer);
        canvas.classList.remove("scene-live");
        if (fallback) fallback.classList.remove("scene-hidden");
      },
    };
    return true;
  }

  /* ---- reduced-motion toggle ---- */
  function setReduced(on) {
    root.setAttribute("data-motion", on ? "reduced" : "full");
    applyToggle();
    if (on) {
      if (glState) { glState.stop(); glState = null; }
    } else if (!glState) {
      initScene();
      heroEntrance();
    }
  }
  if (toggle) {
    toggle.addEventListener("click", function () {
      setReduced(!reduced());
    });
  }
  var replay = document.getElementById("replayMotion");
  if (replay) {
    replay.addEventListener("click", function () {
      if (!reduced()) heroEntrance();
    });
  }

  if (systemReduced()) {
    root.setAttribute("data-motion", "reduced");
  }
  applyToggle();
  heroEntrance();
  scrollReveals();
  initScene();
})();
"""


def showcase_motion3d_script(
    spec: dict[str, object] | None = None,
    scene_spec: dict[str, object] | None = None,
    *,
    motion_tokens: dict[str, object] | None = None,
    temperament: str | None = None,
    scene_type: str | None = None,
    palette: dict[str, str] | None = None,
    tokens: dict[str, object] | None = None,
) -> str:
    """The inline `<script>` body for the showcase's live motion/3D section.

    Same motion spec and scene spec as the downloadable files — the
    dependency-free runtime just executes them without GSAP/Three.js (the
    showcase CSP forbids third-party origins; see the module docstring).
    """
    if spec is None:
        spec = build_motion_spec(motion_tokens, temperament)
    if scene_spec is None:
        scene_spec = build_scene_spec(scene_type, palette, tokens=tokens)
    return _SHOWCASE_SCRIPT_TEMPLATE.replace(
        "__MOTION_JSON__", json.dumps(spec, sort_keys=True)
    ).replace("__SCENE_JSON__", json.dumps(scene_spec, sort_keys=True))
