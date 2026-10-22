"""The dashboard's selectable UI styles (workstream UI-02, issue #116).

Sixteen named visual treatments, Bento Grid the default. The registry is the
single source of truth: the sidebar picker, the `data-ui-style` attribute on
`<html>`, and `static/ui-styles.css` all key off these slugs.

Persistence: the choice lives in the user's own config file
(`~/.config/jev/config.toml`, `[dashboard] ui_style`), written with the same
atomic `settings.write_value` the settings editor uses, so it survives
restarts. Reading never raises: a missing, unreadable or corrupt file falls
back to the default, because a cosmetic preference must never take down page
rendering (the settings page still surfaces a corrupt config through its own
read path).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from jev_agent.app import settings
from jev_agent.config.loader import default_user_config_path

_CONFIG_DOTTED = "dashboard.ui_style"


@dataclass(frozen=True)
class UIStyle:
    """One selectable style: the CSS slug, the picker's label, a short blurb."""

    slug: str
    label: str
    blurb: str


UI_STYLES: tuple[UIStyle, ...] = (
    UIStyle("minimalism", "Minimalism", "Stripped back: hairlines, air, quiet type."),
    UIStyle("flat", "Flat Design 2.0", "Bold flat color, no shadows, crisp edges."),
    UIStyle("skeuomorphism", "Skeuomorphism", "Glossy depth: gradients, insets, highlights."),
    UIStyle("neumorphism", "Neumorphism", "Soft extruded plastic: dual diffuse shadows."),
    UIStyle("glassmorphism", "Glassmorphism", "Frosted glass: blur, translucency, light borders."),
    UIStyle("claymorphism", "Claymorphism", "Chunky clay: thick soft rims, puffy shadows."),
    UIStyle("bento", "Bento Grid", "Tiled mosaic: varied tiles, soft lift, airy gaps."),
    UIStyle("cards", "Cards-Based UI", "Everything floats: strong cards, deep gaps."),
    UIStyle("split-screen", "Split-Screen Design", "Hard sidebar/content divide, two-tone zones."),
    UIStyle("brutalism", "Brutalism", "Raw concrete: square corners, heavy black rules."),
    UIStyle("neo-brutalism", "Neo-Brutalism", "Brutalist bones, accent offset shadows."),
    UIStyle("bauhaus", "Bauhaus Style", "Geometric, uppercase, primary-color accents."),
    UIStyle("hand-drawn", "Hand-Drawn", "Sketchbook: wobbly borders, casual type."),
    UIStyle("cyberpunk", "Cyberpunk", "Neon terminal: glow, scanlines, mono caps."),
    UIStyle("y2k", "Y2K / Vaporwave", "Chrome gloss: gradients, shine, playful shine."),
    UIStyle("pixel-art", "Pixel Art UI", "Stepped pixels: chunky borders, mono, no blur."),
)

DEFAULT_UI_STYLE = "bento"

_SLUGS = frozenset(style.slug for style in UI_STYLES)
_BY_SLUG = {style.slug: style for style in UI_STYLES}


def label_for(slug: str) -> str:
    """The picker's human label for a slug (the slug itself when unknown)."""
    style = _BY_SLUG.get(slug)
    return style.label if style is not None else slug


def normalize_ui_style(value: object) -> str:
    """The slug for `value`, or the default for anything unknown/empty."""
    return value if isinstance(value, str) and value in _SLUGS else DEFAULT_UI_STYLE


def read_ui_style(path: Path | None = None) -> str:
    """The persisted style slug; the default when nothing usable is stored."""
    config = path if path is not None else default_user_config_path()
    try:
        raw = config.read_bytes()
    except OSError:
        return DEFAULT_UI_STYLE
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return DEFAULT_UI_STYLE
    dashboard = data.get("dashboard")
    value = dashboard.get("ui_style") if isinstance(dashboard, dict) else None
    return normalize_ui_style(value)


def write_ui_style(slug: str, path: Path | None = None) -> str:
    """Persist `slug` (ValueError for an unknown slug); returns the slug written."""
    if slug not in _SLUGS:
        raise ValueError(f"unknown UI style: {slug!r}")
    config = path if path is not None else default_user_config_path()
    settings.write_value(config, _CONFIG_DOTTED, slug)
    return slug
