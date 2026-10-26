"""Design-system view builders: library list/detail/code views and actions."""

from __future__ import annotations

import json
import re
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
from jev_agent.core.design_showcase import VIBE_PROFILES
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


def _design_system_view(repo: views.Repo, name: str) -> dict[str, Any]:
    """One library system for the detail page: tokens and the system markdown."""
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
        "business": _business_dict(BusinessContext.from_meta(meta)),
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
