"""Design-system view builders: library list/detail/code views and actions."""

from __future__ import annotations

import base64
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from starlette.datastructures import FormData

from jev_agent.app.errors import JevError
from jev_agent.core.design_business import (
    ELEMENT_CATALOG,
    BusinessContext,
    BusinessElement,
    elements_for_keys,
    enabled_element_keys,
    propose_elements,
)
from jev_agent.core.design_codegen import (
    CODEGEN_TARGETS,
    business_catalog,
    business_files,
    component_catalog,
    generate_files,
)
from jev_agent.core.design_library import (
    _flat_colors,
    library_system_path,
    list_library,
    load_library_system,
    save_library_system,
    update_library_meta,
    validate_library_name,
)
from jev_agent.core.design_showcase import VIBE_PROFILES, rich_tokens, vibe_for_keywords
from jev_agent.core.design_system import META_JSON, design_dir, save_design_system
from jev_agent.core.store import NotFoundError
from jev_agent.dashboard import views
from jev_agent.dashboard.helpers import _text
from jev_agent.dashboard.render import render_markdown


def _library_meta(name: str) -> dict[str, object]:
    """A library system's meta.json extras; {} when it cannot be read.

    Corrupt files are the library's own concern (`load_library_system` warns); the
    dashboard stays readable even when one system is broken, like `design list`.
    """
    try:
        raw = json.loads((library_system_path(name) / META_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _meta_text(meta: dict[str, object], key: str) -> str:
    value = meta.get(key)
    return str(value) if isinstance(value, str) and value else "-"


def _repo_design_source(root: Path) -> str | None:
    """The repo's installed design system's `source` (`library:<name>` after `use`)."""
    try:
        raw = json.loads((design_dir(root) / META_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    source = raw.get("source") if isinstance(raw, dict) else None
    return str(source) if isinstance(source, str) else None


def _design_systems_view(repo: views.Repo, query: str = "") -> dict[str, Any]:
    """The library's systems for the list page, with the repo's active one marked.

    `query` filters on the system name, case-insensitively (the chats list's
    search pattern)."""
    active = _repo_design_source(repo.root)
    needle = query.strip().lower()
    systems = []
    for name in list_library():
        if needle and needle not in name.lower():
            continue
        meta = _library_meta(name)
        systems.append(
            {
                "name": name,
                "source": _meta_text(meta, "source"),
                "app_type": _meta_text(meta, "app_type"),
                "stack": _meta_text(meta, "stack"),
                "version": meta.get("version", "-"),
                "updated": meta.get("updated_at", "-"),
                "active": active == f"library:{name}",
            }
        )
    return {"systems": systems}


_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Palette roles shown on a landing preview, in order: the brand colors first,
# then the semantic roles. A system with only a couple of colors still shows a
# strip; roles with no (valid) value are skipped, never rendered as text.
_PREVIEW_SWATCH_ROLES = (
    "primary",
    "primary_hover",
    "accent_500",
    "accent_300",
    "success",
    "warning",
    "danger",
    "info",
)


def _hex_color(value: object) -> str | None:
    """A token value that is safe to inline as a CSS color, or None.

    Only 6-digit hex passes: anything else (a dict repr, a name, an `rgb()`
    string, a length) would either render nothing or break the declaration,
    so it is skipped instead of inlined (the #279 dict-repr lesson).
    """
    if isinstance(value, str):
        text = value.strip()
        if _HEX_COLOR_RE.match(text):
            return text.upper()
    return None


def _on_hex(hex_color: str) -> str:
    """Readable text on a hex background: white on dark, near-black on light."""
    red = int(hex_color[1:3], 16)
    green = int(hex_color[3:5], 16)
    blue = int(hex_color[5:7], 16)
    luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
    return "#FFFFFF" if luminance < 0.5 else "#101418"


def _studio_landing_view() -> dict[str, Any]:
    """The studio landing's library previews: one miniature per system.

    Each preview carries the system's own palette (validated hex only), its
    primary/background/text colors, and its vibe tagline, so the template can
    render what a system looks like — palette strip, a big "Aa" in the
    primary, a button in the primary — before the visitor clicks in. A
    corrupt or unreadable system is skipped, never a 500; a system with no
    usable colors still renders, on the fallback palette.
    """
    systems = []
    for name in list_library():
        system = load_library_system(name)
        if system is None:
            continue
        meta = _library_meta(name)
        tokens = system.tokens if isinstance(system.tokens, dict) else {}
        light, _dark = _flat_colors(tokens)
        swatches: list[str] = []
        for role in _PREVIEW_SWATCH_ROLES:
            hex_value = _hex_color(light.get(role))
            if hex_value is not None and hex_value not in swatches:
                swatches.append(hex_value)
        primary = _hex_color(light.get("primary")) or (swatches[0] if swatches else "#2563EB")
        background = _hex_color(light.get("background")) or "#FFFFFF"
        text = _hex_color(light.get("text")) or "#101418"
        vibe_key = meta.get("vibe_profile")
        vibe = VIBE_PROFILES.get(vibe_key) if isinstance(vibe_key, str) else None
        tagline = vibe.tagline if vibe is not None else ""
        systems.append(
            {
                "name": name,
                "app_type": _meta_text(meta, "app_type"),
                "stack": _meta_text(meta, "stack"),
                "version": system.version,
                "updated": system.updated_at,
                "swatches": swatches[:6],
                "primary": primary,
                "on_primary": _on_hex(primary),
                "background": background,
                "text": text,
                "tagline": tagline,
            }
        )
    return {"systems": systems}


def _flatten_token_section(section: object) -> list[tuple[str, object]]:
    """(key, value) rows for a token section, nested dicts flattened.

    A ``{"light": {...}, "dark": {...}}`` colors section becomes
    ``light.primary`` / ``dark.primary`` rows instead of rendering the dict
    repr as the value (the template prints each row's value verbatim).
    Flat sections render exactly as before, keys sorted at each level.
    """
    rows: list[tuple[str, object]] = []

    def walk(value: object, prefix: str) -> None:
        if isinstance(value, dict):
            for key, item in sorted(value.items(), key=lambda kv: str(kv[0])):
                walk(item, f"{prefix}{key}.")
        else:
            rows.append((prefix.rstrip("."), value))

    walk(section, "")
    return rows


def _business_dict(context: BusinessContext | None) -> dict[str, str] | None:
    """A business context as plain template data (None when there is none)."""
    if context is None:
        return None
    return {
        "industry": context.industry,
        "audience": context.audience,
        "tone": context.tone,
        "offering": context.offering,
        "description": context.description,
    }


def _catalog_elements() -> list[BusinessElement]:
    """Every catalogued business element (`ELEMENT_CATALOG`, dict or sequence)."""
    if isinstance(ELEMENT_CATALOG, dict):
        return list(ELEMENT_CATALOG.values())
    return list(ELEMENT_CATALOG)


def _catalog_keys() -> set[str]:
    """The keys of every catalogued business element."""
    if isinstance(ELEMENT_CATALOG, dict):
        return {str(key) for key in ELEMENT_CATALOG}
    return {element.key for element in ELEMENT_CATALOG}


def _business_entry(element: BusinessElement, enabled: bool) -> dict[str, object]:
    """One business element as plain template data, with its enabled state."""
    return {
        "key": element.key,
        "title": element.title,
        "description": element.description,
        "why": element.why,
        "enabled": enabled,
    }


# ---------------------------------------------------------------------------
# Design Studio redesign (issue #285): the system detail page is a single-page
# reference — sidebar nav, label column + live renderings, every token and
# component visible inline, like the Claude artifact the user pointed at.
# ---------------------------------------------------------------------------

# Studio-standard scales (issue #285): Layout / Z index / Stagger / Breakpoint
# have no per-system tokens, so the studio ships one shared scale for them.
# They are ALWAYS labelled "Studio standard" in the UI — never presented as
# a system token.
_STUDIO_LAYOUT = [
    ("container-sm", "640px", "Narrow content, mobile-first layouts"),
    ("container-md", "768px", "Tablets and small laptops"),
    ("container-lg", "1024px", "Standard desktop content width"),
    ("container-xl", "1280px", "Wide desktop content width"),
    ("grid", "12 columns", "Fluid grid; gutters 16px / 24px / 32px"),
]
_STUDIO_Z_INDEX = [
    ("base", "0", "Page content"),
    ("dropdown", "100", "Menus, popovers, selects"),
    ("sticky", "200", "Sticky headers and bars"),
    ("modal", "300", "Dialogs and drawers"),
    ("toast", "400", "Toast notifications"),
    ("tooltip", "500", "Hover bubbles, always on top"),
]
_STUDIO_STAGGER = [
    ("stagger-1", "40ms", "First item in a sequence"),
    ("stagger-2", "80ms", "Second item"),
    ("stagger-3", "120ms", "Third item"),
    ("stagger-4", "160ms", "Fourth item and beyond"),
]
_STUDIO_BREAKPOINTS = [
    ("sm", "640px", "Large phones, small tablets"),
    ("md", "768px", "Tablets"),
    ("lg", "1024px", "Laptops"),
    ("xl", "1280px", "Desktops"),
    ("2xl", "1536px", "Wide desktops"),
]

_FONT_STACK_RE = re.compile(r"^[A-Za-z0-9\s,'\"\-]+$")

_LOGO_TAG_ATTRS: dict[str, frozenset[str]] = {
    "svg": frozenset({"xmlns", "viewBox", "width", "height", "role"}),
    "circle": frozenset({"cx", "cy", "r", "fill", "stroke", "stroke-width"}),
    "rect": frozenset({"x", "y", "width", "height", "rx", "fill", "stroke", "stroke-width"}),
    "path": frozenset({"d", "fill", "stroke", "stroke-width"}),
    "polygon": frozenset({"points", "fill", "stroke", "stroke-width"}),
    "polyline": frozenset({"points", "fill", "stroke", "stroke-width"}),
    "line": frozenset({"x1", "y1", "x2", "y2", "stroke", "stroke-width"}),
    "ellipse": frozenset({"cx", "cy", "rx", "ry", "fill", "stroke", "stroke-width"}),
}
_LOGO_ATTR_VALUE_RE = re.compile(r"^[#0-9A-Za-z\s,.\-+:/]+$")


def _safe_logo_svg(path: Path) -> str | None:
    """The stored `logo.svg`, sanitized — or None when it cannot be trusted.

    The wizard's mark has no text primitive and only geometric shapes colored
    from the palette, so a strict allowlist (tags, attribute names, value
    characters) is enough to embed it. Anything else — a hand-edited file, a
    `<script>` smuggled in — renders as a placeholder, never as markup.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    if root.tag.split("}")[-1] != "svg":
        return None
    for element in root.iter():
        tag = element.tag.split("}")[-1]
        allowed = _LOGO_TAG_ATTRS.get(tag)
        if allowed is None:
            return None
        for attr, value in element.attrib.items():
            if attr not in allowed or not _LOGO_ATTR_VALUE_RE.match(value):
                return None
    root.set("xmlns", "http://www.w3.org/2000/svg")
    # Serialize without a namespace prefix: ElementTree defaults to
    # `<ns0:svg>`/`<ns0:rect>`, which HTML parsers do not recognise as SVG
    # — the mark would silently render as nothing.
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    return ET.tostring(root, encoding="unicode")


def _logo_data_uri(path: Path) -> str | None:
    """The stored `logo.svg` as a `data:` URI for an `<img>` (issue #285).

    The SVG is sanitized first (`_safe_logo_svg`), then base64-encoded, so
    the template never needs `| safe` — the audit test in
    `test_issue224_minors.py` allows exactly one `| safe` in the whole
    template tree, and this is not it. The CSP's `img-src` allows `data:`.
    """
    svg = _safe_logo_svg(path)
    if svg is None:
        return None
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def _favicon_data_uri(path: Path) -> str | None:
    """A stored favicon PNG as a `data:` URI (the CSP allows `img-src data:`)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(b"\x89PNG"):
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _rich_for(meta: dict[str, object], tokens: dict[str, object]) -> dict[str, object]:
    """The system's rich tokens: stored `design_tokens`, else derived.

    The wizard stores the full set under `design_tokens`; older systems only
    have the flat keys, so the view derives the same set from the vibe
    profile (`rich_tokens`) — same source the showcase uses.
    """
    stored = tokens.get("design_tokens")
    if isinstance(stored, dict):
        return stored
    profile_key = meta.get("vibe_profile")
    profile = VIBE_PROFILES.get(profile_key) if isinstance(profile_key, str) else None
    if profile is None:
        profile = vibe_for_keywords(str(meta.get("vibe") or meta.get("product_name") or ""))
    theme = meta.get("theme")
    return rich_tokens(profile, str(theme) if isinstance(theme, str) else "both")


def _hex_case(value: object) -> str | None:
    """A token value that is a valid 6-digit hex, in its stored case.

    Unlike `_hex_color` (which normalizes to upper case for the landing
    previews), the detail page shows the token exactly as stored — the
    existing tests assert the stored `#e8590c` / `#18150C` verbatim.
    """
    if isinstance(value, str):
        text = value.strip()
        if _HEX_COLOR_RE.match(text):
            return text
    return None


def _color_rows(
    rich: dict[str, object], tokens: dict[str, object]
) -> tuple[dict[str, list[dict[str, str]]], dict[str, str]]:
    """(Color rows, flat light dict) for the Colors section.

    Systems with stored rich tokens show the full ramps. Legacy systems
    (flat `colors` only, e.g. the screenshots seed) show exactly what they
    stored — flattened `light.*` / `dark.*` rows — and nothing invented.
    The flat dict keeps UNPREFIXED names: the view derives the page's
    primary/background/text from it, and the prefixed row names
    (`light.primary`) would never match a plain lookup.
    """
    stored = tokens.get("design_tokens")
    if isinstance(stored, dict):
        colors = rich.get("colors")
        if isinstance(colors, dict):
            out: dict[str, list[dict[str, str]]] = {"light": [], "dark": []}
            flat_light: dict[str, str] = {}
            for mode in ("light", "dark"):
                section = colors.get(mode)
                if not isinstance(section, dict):
                    continue
                for name in sorted(section):
                    hex_value = _hex_case(section[name])
                    if hex_value is not None:
                        out[mode].append({"name": name, "hex": hex_value})
                        if mode == "light":
                            flat_light[name] = hex_value
            return out, flat_light
    flat = tokens.get("colors")
    rows: dict[str, list[dict[str, str]]] = {"light": [], "dark": []}
    flat_light = {}
    if isinstance(flat, dict) and ("light" in flat or "dark" in flat):
        for mode in ("light", "dark"):
            section = flat.get(mode)
            if not isinstance(section, dict):
                continue
            for name in sorted(section):
                hex_value = _hex_case(section[name])
                if hex_value is not None:
                    rows[mode].append({"name": f"{mode}.{name}", "hex": hex_value})
                    if mode == "light":
                        flat_light[name] = hex_value
    elif isinstance(flat, dict):
        for name in sorted(flat):
            hex_value = _hex_case(flat[name])
            if hex_value is not None:
                rows["light"].append({"name": name, "hex": hex_value})
                flat_light[name] = hex_value
    return rows, flat_light


def _typography_rows(rich: dict[str, object]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(font stacks, type styles) for the Typography section, from rich tokens."""
    typography = rich.get("typography")
    fonts: list[dict[str, str]] = []
    styles: list[dict[str, str]] = []
    if not isinstance(typography, dict):
        return fonts, styles
    for key in ("font_display", "font_body", "font_mono"):
        stack = typography.get(key)
        if isinstance(stack, str) and _FONT_STACK_RE.match(stack):
            fonts.append({"name": key, "stack": stack})
    for name in sorted(typography):
        if name.startswith("font_"):
            continue
        spec = typography[name]
        if not isinstance(spec, dict):
            continue
        size = spec.get("size")
        weight = spec.get("weight")
        leading = spec.get("leading")
        tracking = spec.get("tracking")
        if not isinstance(size, (int, float)) or not isinstance(weight, (int, float)):
            continue
        styles.append(
            {
                "name": name,
                "size": str(size),
                "weight": str(weight),
                "leading": str(leading) if isinstance(leading, (int, float)) else "1.5",
                "tracking": str(tracking) if isinstance(tracking, (int, float)) else "0",
            }
        )
    return fonts, styles


def _scale_rows(rich: dict[str, object], key: str) -> list[dict[str, str]]:
    """(name, value) rows for a flat numeric token scale (spacing / radii)."""
    section = rich.get(key)
    rows: list[dict[str, str]] = []
    if isinstance(section, dict):
        for name in sorted(section):
            value = section[name]
            if isinstance(value, (int, float, str)):
                rows.append({"name": str(name), "value": str(value)})
    return rows


def _shadow_rows(rich: dict[str, object]) -> list[dict[str, str]]:
    section = rich.get("shadows")
    rows: list[dict[str, str]] = []
    if isinstance(section, dict):
        for name in sorted(section):
            value = section[name]
            if isinstance(value, str):
                rows.append({"name": str(name), "value": value})
    return rows


def _motion_rows(rich: dict[str, object]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(durations, easings) from the rich motion tokens."""
    motion = rich.get("motion")
    durations: list[dict[str, str]] = []
    easings: list[dict[str, str]] = []
    if isinstance(motion, dict):
        for name in sorted(motion):
            value = motion[name]
            if not isinstance(value, str):
                continue
            if name.startswith("duration_"):
                durations.append({"name": name, "value": value})
            elif name.startswith("ease_"):
                easings.append({"name": name, "value": value})
    return durations, easings


def _type_specimens(
    styles: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Three type specimens — largest display, headline, body (issue #287).

    The full scale rendered ~12 "Aa — The quick brown fox" rows and the
    display sizes wrapped to 2-3 lines in the narrow content column. The
    full scale stays in the copied JSON; the page shows three, big. The
    display size is capped at 52px so the pangram fits on one line at
    desktop widths.
    """
    picked: list[dict[str, str]] = []
    for prefix in ("display_", "headline_", "body_"):
        group = [style for style in styles if style["name"].startswith(prefix)]
        if not group:
            continue
        best = max(group, key=lambda style: float(style["size"]))
        row = dict(best)
        row["size"] = str(min(float(best["size"]), 52))
        picked.append(row)
    return picked


def _overview_palette(flat_light: dict[str, str]) -> list[dict[str, str]]:
    """Up to 8 signature colors for the Overview palette strip (issue #287).

    Brand + surface roles first, in a fixed order; anything else fills the
    strip alphabetically. Names are the flat (unprefixed) token names.
    """
    picked: list[dict[str, str]] = []
    for name in (
        "primary",
        "primary_hover",
        "accent_500",
        "background",
        "surface",
        "text",
        "success",
        "warning",
        "danger",
        "info",
    ):
        hex_value = _hex_case(flat_light.get(name))
        if hex_value is not None:
            picked.append({"name": name, "hex": hex_value})
        if len(picked) == 8:
            return picked
    for name in sorted(flat_light):
        if len(picked) == 8:
            break
        if name in {row["name"] for row in picked}:
            continue
        hex_value = _hex_case(flat_light.get(name))
        if hex_value is not None:
            picked.append({"name": name, "hex": hex_value})
    return picked


def _preview_palette(flat: dict[str, str], fallback: dict[str, str]) -> dict[str, str]:
    """(background, surface, text, muted, border) for a preview theme.

    `flat` is one mode's flat color dict (unprefixed names); a missing role
    falls back to that theme's defaults, never to the other mode's colors.
    """
    return {
        "background": _hex_color(flat.get("background")) or fallback["background"],
        "surface": _hex_color(flat.get("surface")) or fallback["surface"],
        "text": _hex_color(flat.get("text")) or fallback["text"],
        "muted": _hex_color(flat.get("text_muted")) or fallback["muted"],
        "border": _hex_color(flat.get("border")) or fallback["border"],
    }


def _design_system_view(repo: views.Repo, name: str) -> dict[str, Any]:
    """One library system for the detail page: the single-page reference view.

    Every token category and component renders inline on the page (issue
    #285): Colors / Typography / Spacing / Corner radius / Shadows / Duration
    / Easing come from the rich tokens; Layout / Z index / Stagger /
    Breakpoint are the studio's standard scale, labelled as such in the UI.
    """
    system = load_library_system(name)
    if system is None:
        raise NotFoundError(f'no design system named "{name}" in the library')
    meta = _library_meta(name)
    tokens = system.tokens if isinstance(system.tokens, dict) else {}
    sections = (
        ("Colors", tokens.get("colors")),
        ("Type scale", tokens.get("type_scale")),
        ("Spacing", tokens.get("spacing")),
        ("Radii", tokens.get("radii")),
    )
    token_sections = [
        {"title": title, "items": _flatten_token_section(section)} for title, section in sections
    ]
    rich = _rich_for(meta, tokens)
    colors, light_colors = _color_rows(rich, tokens)
    fonts, type_styles = _typography_rows(rich)
    spacing = _scale_rows(rich, "spacing")
    radii = _scale_rows(rich, "radii")
    shadows = _shadow_rows(rich)
    durations, easings = _motion_rows(rich)
    primary = _hex_color(light_colors.get("primary")) or "#2563EB"
    background = _hex_color(light_colors.get("background")) or "#FFFFFF"
    text = _hex_color(light_colors.get("text")) or "#101418"
    theme = str(meta.get("theme") or rich.get("theme") or "both")
    # Issue #287: the light/dark preview toggle needs both modes' surfaces.
    # Dark rows carry prefixed names for legacy flat tokens ("dark.primary"),
    # so strip the prefix; rich rows are already unprefixed.
    dark_flat = {row["name"].removeprefix("dark."): row["hex"] for row in colors["dark"]}
    preview_light = _preview_palette(
        light_colors,
        {
            "background": "#FFFFFF",
            "surface": "#F7F7F8",
            "text": "#101418",
            "muted": "#5F5F58",
            "border": "#E2E2DF",
        },
    )
    preview_dark = _preview_palette(
        dark_flat,
        {
            "background": "#10100F",
            "surface": "#1B1A18",
            "text": "#EEEEEC",
            "muted": "#9D9D95",
            "border": "#282825",
        },
    )
    # The preview defaults to the system as it actually looks: a light or
    # both-theme system opens light, a dark-only system opens dark.
    preview_theme = "light" if theme in ("light", "both") else "dark"
    system_path = library_system_path(name)
    files = (
        sorted(entry.name for entry in system_path.iterdir() if entry.is_file())
        if system_path.is_dir()
        else []
    )
    context = BusinessContext.from_meta(meta)
    proposed: list[dict[str, object]] = []
    enabled: list[dict[str, object]] = []
    if context is not None and not context.is_empty():
        enabled_set = set(enabled_element_keys(meta, context))
        proposed = [
            _business_entry(element, element.key in enabled_set)
            for element in propose_elements(context)
        ]
        enabled = [
            _business_entry(element, True)
            for element in elements_for_keys(enabled_element_keys(meta, context))
        ]
    vibe_key = meta.get("vibe_profile")
    vibe = VIBE_PROFILES.get(vibe_key) if isinstance(vibe_key, str) else None
    return {
        "name": name,
        "source": _meta_text(meta, "source"),
        "app_type": _meta_text(meta, "app_type"),
        "stack": _meta_text(meta, "stack"),
        "version": system.version,
        "updated": system.updated_at,
        "token_sections": token_sections,
        "markdown_html": render_markdown(system.markdown),
        "active": _repo_design_source(repo.root) == f"library:{name}",
        "business": _business_dict(context),
        "vibe_name": vibe.name if vibe is not None else str(rich.get("vibe_name") or "-"),
        "vibe_tagline": str(rich.get("vibe_tagline") or ""),
        "theme": theme,
        "preview_theme": preview_theme,
        "preview_light": preview_light,
        "preview_dark": preview_dark,
        "overview_palette": _overview_palette(light_colors),
        "type_specimens": _type_specimens(type_styles),
        "colors_light": colors["light"],
        "colors_dark": colors["dark"],
        "colors_json": json.dumps(
            {"light": light_colors, "dark": {row["name"]: row["hex"] for row in colors["dark"]}},
            indent=2,
        ),
        "fonts": fonts,
        "type_styles": type_styles,
        "typography_json": json.dumps(rich.get("typography") or {}, indent=2, default=str),
        "spacing": spacing,
        "spacing_json": json.dumps(rich.get("spacing") or {}, indent=2, default=str),
        "radii": radii,
        "radii_json": json.dumps(rich.get("radii") or {}, indent=2, default=str),
        "shadows": shadows,
        "shadows_json": json.dumps(rich.get("shadows") or {}, indent=2, default=str),
        "durations": durations,
        "easings": easings,
        "motion_json": json.dumps(rich.get("motion") or {}, indent=2, default=str),
        "layout_standard": [
            {"name": item[0], "value": item[1], "note": item[2]} for item in _STUDIO_LAYOUT
        ],
        "z_index_standard": [
            {"name": item[0], "value": item[1], "note": item[2]} for item in _STUDIO_Z_INDEX
        ],
        "stagger_standard": [
            {"name": item[0], "value": item[1], "note": item[2]} for item in _STUDIO_STAGGER
        ],
        "breakpoints_standard": [
            {"name": item[0], "value": item[1], "note": item[2]} for item in _STUDIO_BREAKPOINTS
        ],
        "files": files,
        "file_count": len(files),
        "business_proposed": proposed,
        "business_enabled": enabled,
        "primary": primary,
        "on_primary": _on_hex(primary),
        "background": background,
        "text": text,
        "logo_data_uri": _logo_data_uri(system_path / "logo.svg"),
        "logo_template": _meta_text(meta, "logo_template"),
        "favicon_16": _favicon_data_uri(system_path / "favicon-16.png"),
        "favicon_32": _favicon_data_uri(system_path / "favicon-32.png"),
        "favicon_180": _favicon_data_uri(system_path / "favicon-180.png"),
    }


def _design_system_code_view(repo: views.Repo, name: str, target: str) -> dict[str, Any]:
    """The "Get reusable elements" page: generated component files with copy buttons."""
    system = load_library_system(name)
    if system is None:
        raise NotFoundError(f'no design system named "{name}" in the library')
    tokens = system.tokens if isinstance(system.tokens, dict) else {}
    files = generate_files(target, name, tokens)
    catalog = component_catalog(target)
    entries = [
        {
            "filename": item["filename"],
            "title": item["title"],
            "description": item["description"],
            "code": files.get(item["filename"], ""),
            "lines": files.get(item["filename"], "").count("\n") + 1,
        }
        for item in catalog
    ]
    # Business elements (issue #283): the enabled ones generate extra files,
    # merged into the same entry list after the base components.
    meta = _library_meta(name)
    context = BusinessContext.from_meta(meta)
    business: dict[str, object] = {"context": None, "proposed": [], "catalog": []}
    if context is not None and not context.is_empty():
        proposed = propose_elements(context)
        enabled = enabled_element_keys(meta, context)
        enabled_set = set(enabled)
        business_elements = elements_for_keys(enabled)
        business_files_map = business_files(target, name, tokens, business_elements)
        entries.extend(
            {
                "filename": item["filename"],
                "title": item["title"],
                "description": item["description"],
                "code": business_files_map.get(item["filename"], ""),
                "lines": business_files_map.get(item["filename"], "").count("\n") + 1,
            }
            for item in business_catalog(target, business_elements)
        )
        business = {
            "context": context.to_meta(),
            "proposed": [
                _business_entry(element, element.key in enabled_set) for element in proposed
            ],
            "catalog": [
                _business_entry(element, element.key in enabled_set)
                for element in _catalog_elements()
            ],
        }
    return {
        "name": name,
        "target": target,
        "targets": [
            {"key": key, "label": info["label"], "description": info["description"]}
            for key, info in CODEGEN_TARGETS.items()
        ],
        "entries": entries,
        "stack": _meta_text(meta, "stack"),
        "business": business,
    }


def _design_system_extras(name: str) -> dict[str, object]:
    """The library meta extras a `use`/`clone` carries over (mirrors `design_cmds`)."""
    meta = _library_meta(name)
    return {key: meta[key] for key in ("app_type", "stack") if isinstance(meta.get(key), str)}


def _reverse_evidence_summary(evidence: dict[str, object]) -> dict[str, Any]:
    """The reverse evidence dict shaped for `_design_studio_reverse_evidence.html`.

    Only plain data crosses into the template: strings, counts and (hex,
    coverage) pairs. Unknown shapes read as empty rather than raising, so a
    future evidence format cannot break the status page.
    """
    if evidence.get("kind") == "url":
        raw = evidence.get("url_evidence")
        url_evidence = raw if isinstance(raw, dict) else {}
        colors = url_evidence.get("colors")
        top_colors = (
            [(str(color), count) for color, count in list(colors.items())[:12]]
            if isinstance(colors, dict)
            else []
        )
        fonts = url_evidence.get("fonts")
        title = url_evidence.get("title")
        theme_color = url_evidence.get("theme_color")
        styles = url_evidence.get("styles")
        return {
            "kind": "url",
            "title": title if isinstance(title, str) else None,
            "theme_color": theme_color if isinstance(theme_color, str) else None,
            "fonts": [str(font) for font in fonts] if isinstance(fonts, list) else [],
            "top_colors": top_colors,
            "styles": str(styles)[:600] if isinstance(styles, str) else "",
        }
    shots = evidence.get("screenshots")
    summaries = []
    if isinstance(shots, list):
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            palette = shot.get("palette")
            entries = []
            if isinstance(palette, list):
                for entry in palette:
                    if (
                        isinstance(entry, (list, tuple))
                        and len(entry) == 2
                        and isinstance(entry[0], str)
                    ):
                        entries.append((entry[0], f"{float(entry[1]):.0%}"))
            path = shot.get("path")
            summaries.append(
                {
                    "name": Path(str(path)).name if isinstance(path, str) else "screenshot",
                    "palette": entries,
                }
            )
    return {"kind": "screenshots", "shots": summaries}


def _design_system_use(repo: views.Repo, form: FormData) -> str:
    """`jev design use <name>` (Tier 1): install the library system into the repo."""
    name = validate_library_name((_text(form, "name") or "").strip())
    system = load_library_system(name)
    if system is None:
        raise JevError(f'no design system named "{name}" in the library')
    version = save_design_system(
        repo.root,
        system.markdown,
        dict(system.tokens),
        meta_extra={"source": f"library:{name}", **_design_system_extras(name)},
    )
    return f'installed "{name}" as the repo design system v{version}'


def _design_system_clone(repo: views.Repo, form: FormData) -> str:
    """`jev design clone <name> <new_name>` (Tier 1): copy a library system."""
    name = validate_library_name((_text(form, "name") or "").strip())
    new_name = validate_library_name((_text(form, "new_name") or "").strip())
    system = load_library_system(name)
    if system is None:
        raise JevError(f'no design system named "{name}" in the library')
    if library_system_path(new_name).exists():
        raise JevError(f'a design system named "{new_name}" already exists in the library')
    version = save_library_system(
        new_name,
        system.markdown,
        dict(system.tokens),
        meta_extra={
            "name": new_name,
            "source": f"clone:{name}",
            **_design_system_extras(name),
        },
    )
    return f'cloned "{name}" to "{new_name}" (library v{version})'


def _design_system_business_context(repo: views.Repo, form: FormData) -> str:
    """Update a library system's business context (issue #283, meta-only).

    The edit never bumps the system version: `update_library_meta` rewrites
    `meta.json` extras without touching the versioned markdown/tokens. An
    emptied context clears both stored keys. A changed, non-empty context
    resets `business_elements` to the newly proposed keys only when no stored
    list exists — a user who customized the enabled list keeps their list,
    and the code page proposes the new elements as addable instead.
    """
    name = validate_library_name((_text(form, "name") or "").strip())
    context = BusinessContext.from_form(form)
    meta = _library_meta(name)
    if context.is_empty():
        patch: dict[str, object | None] = {"business_context": None, "business_elements": None}
    else:
        patch = {"business_context": context.to_meta()}
        if "business_elements" not in meta:
            patch["business_elements"] = [element.key for element in propose_elements(context)]
    if update_library_meta(name, patch) is None:
        raise JevError(f'no design system named "{name}" in the library')
    return f'business context updated for "{name}"'


def _design_system_business_elements(repo: views.Repo, form: FormData) -> str:
    """Toggle one business element on/off for a library system (issue #283).

    The form carries `name`, `element` (a catalog key) and `action`
    (`add`/`remove`). Adds keep the stored order and append the key; removes
    drop it. Meta-only, like the context edit — no version bump.
    """
    name = validate_library_name((_text(form, "name") or "").strip())
    key = (_text(form, "element") or "").strip()
    action = (_text(form, "action") or "").strip()
    if key not in _catalog_keys():
        raise JevError(f'unknown business element "{key}"')
    if action not in ("add", "remove"):
        raise JevError(f'unknown business element action "{action}": choose add or remove')
    meta = _library_meta(name)
    stored = meta.get("business_elements")
    if isinstance(stored, list):
        enabled = [str(item) for item in stored if isinstance(item, str)]
    else:
        context = BusinessContext.from_meta(meta)
        enabled = (
            enabled_element_keys(meta, context)
            if context is not None and not context.is_empty()
            else []
        )
    if action == "add":
        if key not in enabled:
            enabled.append(key)
        verb = "added"
    else:
        enabled = [item for item in enabled if item != key]
        verb = "removed"
    if update_library_meta(name, {"business_elements": enabled}) is None:
        raise JevError(f'no design system named "{name}" in the library')
    return f'{verb} business element "{key}" for "{name}"'
