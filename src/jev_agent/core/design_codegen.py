"""Reusable component code generation for Design Studio systems (issue #155).

"Get reusable elements" turns a library design system into production-ready,
drop-in component code — clean, commented, and actually usable, not demo
scaffolding. Two targets:

- ``html-css``: framework-free. ``tokens.css`` (CSS custom properties from the
  design tokens, light + dark), ``components.css`` (the full component
  library), ``index.html`` (a demo page wiring it together).
- ``react-tailwind``: ``tokens.css`` (the same variables),
  ``tailwind.config.js`` (theme extension mapped onto the variables),
  ``components.jsx`` (documented React components built on Tailwind utilities
  + the variables), ``README.md`` (setup notes).

``target_for_stack()`` maps the wizard's stack choice to the closest target.
Everything is stdlib-only; the dashboard serves a per-component copy page and
a "download all" zip built by ``bundle_zip()``.
"""

from __future__ import annotations

import html
import io
import re
import zipfile

from jev_agent.core.design_business import (
    BusinessElement,
    _coerce_int,
    _coerce_str,
    _data_dicts,
    _data_strs,
)

CODEGEN_TARGETS: dict[str, dict[str, str]] = {
    "html-css": {
        "label": "HTML + CSS",
        "description": "Framework-free: tokens.css + components.css + a demo page.",
    },
    "react-tailwind": {
        "label": "React + Tailwind",
        "description": "components.jsx + tailwind.config.js theme + tokens.css.",
    },
}

_STACK_TARGETS: tuple[tuple[str, str], ...] = (
    ("tailwind", "react-tailwind"),
    ("react", "react-tailwind"),
    ("css", "html-css"),
)


def target_for_stack(stack: str) -> str:
    """The closest codegen target for a wizard stack choice."""
    lowered = (stack or "").lower()
    for needle, target in _STACK_TARGETS:
        if needle in lowered:
            return target
    return "html-css"


def validate_target(target: str) -> str:
    """The target key, or the default when unknown."""
    return target if target in CODEGEN_TARGETS else "html-css"


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _palettes(tokens: dict[str, object]) -> tuple[dict[str, str], dict[str, str]]:
    """(light, dark) flat color dicts from rich ``design_tokens``.

    Falls back to the legacy flat ``colors`` dict when rich tokens are absent
    (older library systems), using it for both themes. A legacy ``colors``
    dict that itself nests ``{"light": {...}, "dark": {...}}`` (the cxm
    shape) unwraps the same way — passing the nested dicts through
    ``_str_dict`` used to stringify them into Python reprs as CSS values.
    """
    rich = tokens.get("design_tokens")
    if isinstance(rich, dict):
        colors = rich.get("colors")
        if isinstance(colors, dict):
            light = colors.get("light")
            dark = colors.get("dark")
            if isinstance(light, dict) and isinstance(dark, dict):
                return _str_dict(light), _str_dict(dark)
            if isinstance(light, dict):
                flat = _str_dict(light)
                return flat, flat
    legacy = tokens.get("colors")
    if isinstance(legacy, dict):
        light_raw = legacy.get("light")
        dark_raw = legacy.get("dark")
        if isinstance(light_raw, dict) or isinstance(dark_raw, dict):
            light = _str_dict(light_raw) if isinstance(light_raw, dict) else {}
            dark = _str_dict(dark_raw) if isinstance(dark_raw, dict) else light
            return light, dark
        flat = _str_dict(legacy)
        return flat, flat
    return {}, {}


def _str_dict(value: object) -> dict[str, str]:
    """A dict flattened to string keys/values.

    Nested dicts flatten with ``_``-joined keys (``primary: {base, hover}``
    becomes ``primary_base`` / ``primary_hover``) instead of stringifying
    into a Python repr — a repr is not a CSS value, and ``_css_value``
    stripping its braces used to leave ``'primary': '#...'`` as a
    declaration value.
    """
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict):
                for sub_key, sub_value in _str_dict(item).items():
                    out[f"{key}_{sub_key}"] = sub_value
            else:
                out[str(key)] = str(item)
    return out


def _section(tokens: dict[str, object], key: str) -> dict[str, object]:
    rich = tokens.get("design_tokens")
    if isinstance(rich, dict) and isinstance(section := rich.get(key), dict):
        return dict(section)
    value = tokens.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _kebab(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", name.replace("_", "-").lower()).strip("-")


def _css_value(value: object) -> str:
    """A token value safe inside a CSS declaration (terminators stripped)."""
    return re.sub(r"[\x00-\x1f\x7f;{}]", "", str(value)).strip()


def _html_name(name: str) -> str:
    """A system name safe inside generated HTML text/attributes."""
    return html.escape(str(name), quote=True)


def _comment_name(name: str) -> str:
    """A system name safe inside a `/* */` or `//` comment: newlines and
    comment terminators neutralized so the name cannot break out of it."""
    return re.sub(r"\s+", " ", str(name)).replace("*/", "* /").replace("/*", "/ *").strip()


def _text_name(name: str) -> str:
    """A system name safe on one Markdown line (newlines collapsed)."""
    return re.sub(r"\s+", " ", str(name)).strip()


def _zip_safe_name(name: str) -> str:
    """A system name safe as a zip path prefix: one segment, no `..`, no
    separators — a traversal-style name becomes a flat, harmless prefix."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip(".-") or "design-system"
    return safe


def _hex_rgb(value: str) -> tuple[int, int, int] | None:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _mix(first: str, second: str, first_weight: float) -> str:
    """Blend two `#rrggbb` colors; falls back to `first` when either is not hex."""
    a = _hex_rgb(first)
    b = _hex_rgb(second)
    if a is None or b is None:
        return first
    mixed = tuple(
        round(x * first_weight + y * (1.0 - first_weight)) for x, y in zip(a, b, strict=True)
    )
    return f"#{mixed[0]:02X}{mixed[1]:02X}{mixed[2]:02X}"


def _on_color(bg: str) -> str:
    """White or near-black, whichever reads better on `bg` (WCAG luminance)."""
    rgb = _hex_rgb(bg)
    if rgb is None:
        return "#FFFFFF"

    def lin(c: int) -> float:
        v = c / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    luminance = 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2])
    return "#FFFFFF" if (1.05 / (luminance + 0.05)) >= ((luminance + 0.05) / 0.05) else "#101418"


def _derived_colors(palette: dict[str, str], *, dark: bool = False) -> dict[str, str]:
    """Fill every color role the component templates reference.

    Rich tokens already carry these; legacy/flat sets (e.g. STARTER_TOKENS)
    do not, which used to leave badges/alerts on transparent backgrounds.
    Derivations mix from the palette that *is* present (a subtle is the
    semantic color mixed 12% into the background), so a legacy system keeps
    its own hues instead of silently swapping to another palette. Constants
    cover roles with no derivable source (`info` when absent).

    `dark` switches the fallback constants to dark-appropriate values and
    lightens (rather than darkens) the primary hover: a dark palette derived
    with light constants would put near-white subtles on dark surfaces.
    """
    defaults = _DARK_ROLE_DEFAULTS if dark else _LIGHT_ROLE_DEFAULTS
    out = dict(palette)
    bg = out.get("background") or defaults["background"]
    surface = out.get("surface") or bg
    text = out.get("text") or defaults["text"]
    primary = out.get("primary") or defaults["primary"]
    border = out.get("border") or defaults["border"]
    hover_toward = "#FFFFFF" if dark else "#000000"
    out.setdefault("background", bg)
    out.setdefault("surface", surface)
    out.setdefault("text", text)
    out.setdefault("primary", primary)
    out.setdefault("primary_hover", _mix(primary, hover_toward, 0.88))
    out.setdefault("primary_text", _on_color(primary))
    out.setdefault("primary_subtle", _mix(primary, bg, 0.12))
    out.setdefault("surface_raised", _mix(surface, text, 0.94))
    out.setdefault("text_muted", _mix(text, bg, 0.55))
    out.setdefault("text_faint", _mix(text, bg, 0.35))
    out.setdefault("border", border)
    out.setdefault("border_strong", _mix(border, text, 0.70))
    for role in ("success", "warning", "danger", "info"):
        out.setdefault(role, defaults[role])
        out.setdefault(f"{role}_subtle", _mix(out[role], bg, 0.12))
    return out


# Fallback constants for roles with no derivable source, per theme. The light
# set is the original hard-coded one; the dark set mirrors the dark semantic
# colors `rich_tokens` generates (brightened semantics, dark neutrals).
_LIGHT_ROLE_DEFAULTS: dict[str, str] = {
    "background": "#FFFFFF",
    "text": "#0F172A",
    "primary": "#2563EB",
    "border": "#E2E8F0",
    "success": "#16A34A",
    "warning": "#D97706",
    "danger": "#DC2626",
    "info": "#0284C7",
}
_DARK_ROLE_DEFAULTS: dict[str, str] = {
    "background": "#0F172A",
    "text": "#F8FAFC",
    "primary": "#60A5FA",
    "border": "#334155",
    "success": "#22C55E",
    "warning": "#F59E0B",
    "danger": "#F87171",
    "info": "#60A5FA",
}


_DEFAULT_FONTS: dict[str, str] = {
    "font_display": 'system-ui, -apple-system, "Segoe UI", sans-serif',
    "font_body": 'system-ui, -apple-system, "Segoe UI", sans-serif',
    "font_mono": 'ui-monospace, "SF Mono", Menlo, monospace',
}

_DEFAULT_RADII: dict[str, object] = {"sm": 6, "md": 10, "lg": 14, "xl": 20, "full": 9999}

_DEFAULT_SHADOWS: dict[str, str] = {
    "sm": "0 1px 2px rgba(0, 0, 0, 0.05)",
    "md": "0 4px 6px -1px rgba(0, 0, 0, 0.1)",
    "lg": "0 10px 15px -3px rgba(0, 0, 0, 0.1)",
    "xl": "0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1)",
}

_DEFAULT_MOTION: dict[str, str] = {
    "duration_fast": "160ms",
    "duration_normal": "250ms",
    "ease_standard": "cubic-bezier(0.4, 0, 0.2, 1)",
}


def css_variables(tokens: dict[str, object]) -> str:
    """``:root`` custom properties for the light palette plus a
    ``[data-theme="dark"]`` override block. Spacing/radii/shadows/motion and
    font stacks are included so components never hard-code values.

    Roles the component templates reference but the token set lacks are
    derived (see `_derived_colors` and the `_DEFAULT_*` tables), so the
    emitted file defines every variable the components use — including for
    legacy flat token sets.
    """
    light, dark = _palettes(tokens)
    light = _derived_colors(light)
    dark = _derived_colors(dark, dark=True)
    typo = _section(tokens, "typography")
    spacing = _section(tokens, "spacing")
    radii = _section(tokens, "radii")
    shadows = _section(tokens, "shadows")
    motion = _section(tokens, "motion")

    def px(value: object) -> str:
        if isinstance(value, bool):
            return str(value)
        return f"{value}px" if isinstance(value, (int, float)) else _css_value(value)

    lines = [":root {"]
    for key in sorted(light):
        css_key = _kebab(key)
        if css_key:
            lines.append(f"  --color-{css_key}: {_css_value(light[key])};")
    for key in ("font_display", "font_body", "font_mono"):
        value = typo.get(key) or _DEFAULT_FONTS[key]
        lines.append(f"  --font-{key[5:]}: {_css_value(value)};")
    for key, value in spacing.items():
        css_key = _kebab(key)
        if css_key:
            lines.append(f"  --space-{css_key}: {px(value)};")
    for key, value in {**_DEFAULT_RADII, **radii}.items():
        css_key = _kebab(key)
        if css_key:
            lines.append(f"  --radius-{css_key}: {px(value)};")
    for key, value in {**_DEFAULT_SHADOWS, **shadows}.items():
        css_key = _kebab(key)
        if css_key:
            lines.append(f"  --shadow-{css_key}: {_css_value(value)};")
    for key, value in {**_DEFAULT_MOTION, **motion}.items():
        css_key = _kebab(key)
        if css_key:
            lines.append(f"  --motion-{css_key}: {_css_value(value)};")
    lines.append("}")
    if dark and dark != light:
        lines.append("")
        lines.append('[data-theme="dark"] {')
        for key in sorted(dark):
            css_key = _kebab(key)
            if css_key:
                lines.append(f"  --color-{css_key}: {_css_value(dark[key])};")
        lines.append("}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# HTML + CSS target
# ---------------------------------------------------------------------------

_COMPONENTS_CSS_TEMPLATE = """\
/* Components for __NAME__ — built on tokens.css variables.
   Usage: <link rel="stylesheet" href="tokens.css">
          <link rel="stylesheet" href="components.css">
   Dark mode: set data-theme="dark" on <html>. */

/* ---------- Button ---------- */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-family: var(--font-body, inherit);
  font-weight: 600;
  font-size: 15px;
  line-height: 1;
  padding: 12px 22px;
  border-radius: var(--radius-md, 8px);
  border: 1px solid transparent;
  cursor: pointer;
  text-decoration: none;
  transition: background var(--motion-duration-fast, 160ms),
    transform var(--motion-duration-fast, 160ms);
}
.btn:active { transform: translateY(1px); }
.btn:disabled { opacity: 0.45; cursor: not-allowed; transform: none; }
.btn-primary {
  background: var(--color-primary);
  color: var(--color-primary-text, #fff);
}
.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
  box-shadow: var(--shadow-sm);
}
.btn-secondary {
  background: var(--color-surface);
  color: var(--color-text);
  border-color: var(--color-border-strong);
}
.btn-secondary:hover:not(:disabled) { border-color: var(--color-text-faint); }
.btn-ghost { background: transparent; color: var(--color-primary); }
.btn-ghost:hover:not(:disabled) { background: var(--color-primary-subtle); }
.btn-danger { background: var(--color-danger); color: #fff; }
.btn-danger:hover:not(:disabled) { filter: brightness(0.94); }
.btn-sm { font-size: 13px; padding: 8px 16px; }
.btn-lg { font-size: 16px; padding: 14px 28px; }

/* ---------- Field (label + input/textarea/select) ---------- */
.field {
  display: grid;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
}
.field input,
.field textarea,
.field select {
  font-family: inherit;
  font-size: 15px;
  font-weight: 400;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md, 8px);
  padding: 11px 14px;
  width: 100%;
  transition: border-color var(--motion-duration-fast, 160ms),
    box-shadow var(--motion-duration-fast, 160ms);
}
.field input:focus,
.field textarea:focus,
.field select:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-subtle);
}
.field input::placeholder,
.field textarea::placeholder { color: var(--color-text-faint); }
.field .hint { font-size: 13px; font-weight: 400; color: var(--color-text-muted); }
.field .hint.error { color: var(--color-danger); }
.field input.invalid,
.field textarea.invalid,
.field select.invalid { border-color: var(--color-danger); }

/* ---------- Badge ---------- */
.badge {
  display: inline-flex;
  align-items: center;
  font-size: 12.5px;
  font-weight: 600;
  padding: 5px 12px;
  border-radius: var(--radius-full, 999px);
  line-height: 1.2;
  white-space: nowrap;
}
.badge-success {
  background: var(--color-success-subtle);
  color: var(--color-success);
}
.badge-warning {
  background: var(--color-warning-subtle);
  color: var(--color-warning);
}
.badge-danger {
  background: var(--color-danger-subtle);
  color: var(--color-danger);
}
.badge-info { background: var(--color-info-subtle); color: var(--color-info); }
.badge-muted {
  background: var(--color-surface-raised);
  color: var(--color-text-muted);
}

/* ---------- Alert ---------- */
.alert {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 14px 18px;
  border-radius: var(--radius-md, 8px);
  font-size: 14.5px;
  border: 1px solid;
  color: var(--color-text);
  background: var(--color-surface);
}
.alert strong { font-weight: 700; }
.alert-success {
  background: var(--color-success-subtle);
  border-color: var(--color-success);
}
.alert-warning {
  background: var(--color-warning-subtle);
  border-color: var(--color-warning);
}
.alert-danger {
  background: var(--color-danger-subtle);
  border-color: var(--color-danger);
}
.alert-info {
  background: var(--color-info-subtle);
  border-color: var(--color-info);
}

/* ---------- Card ---------- */
.card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg, 12px);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}
.card-body { padding: 22px; }
.card-title {
  font-family: var(--font-display, inherit);
  font-size: 19px;
  font-weight: 650;
  letter-spacing: -0.01em;
  margin: 0 0 8px;
}
.card-text {
  color: var(--color-text-muted);
  font-size: 14.5px;
  margin: 0 0 16px;
}
.card-actions { display: flex; gap: 10px; }

/* ---------- Avatar ---------- */
.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: var(--color-primary);
  color: var(--color-primary-text, #fff);
  font-size: 14px;
  font-weight: 700;
  flex: none;
}
.avatar-sm { width: 28px; height: 28px; font-size: 11px; }
.avatar-lg { width: 56px; height: 56px; font-size: 19px; }

/* ---------- Modal ---------- */
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  z-index: 100;
}
.modal {
  background: var(--color-surface);
  border-radius: var(--radius-lg, 12px);
  box-shadow: var(--shadow-xl);
  max-width: 480px;
  width: 100%;
  padding: 26px;
}
.modal-title {
  font-family: var(--font-display, inherit);
  font-size: 20px;
  font-weight: 700;
  margin: 0 0 8px;
}
.modal-text {
  color: var(--color-text-muted);
  font-size: 14.5px;
  margin: 0 0 20px;
}
.modal-actions { display: flex; gap: 10px; justify-content: flex-end; }

/* ---------- Navbar ---------- */
.navbar {
  display: flex;
  align-items: center;
  gap: 6px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg, 12px);
  padding: 10px 16px;
}
.navbar-brand {
  font-family: var(--font-display, inherit);
  font-weight: 800;
  margin-right: 16px;
}
.navbar a {
  padding: 9px 15px;
  border-radius: var(--radius-md, 8px);
  text-decoration: none;
  color: var(--color-text-muted);
  font-size: 14px;
  font-weight: 500;
}
.navbar a:hover {
  color: var(--color-text);
  background: var(--color-surface-raised);
}
.navbar a.active {
  color: var(--color-text);
  background: var(--color-surface-raised);
  font-weight: 600;
}
.navbar .spacer { margin-left: auto; }

/* ---------- Table ---------- */
.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg, 12px);
}
table.data {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
  background: var(--color-surface);
}
table.data th {
  text-align: left;
  padding: 13px 18px;
  font-size: 12px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--color-text-muted);
  font-weight: 600;
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface-raised);
}
table.data td {
  padding: 14px 18px;
  border-bottom: 1px solid var(--color-border);
}
table.data tr:last-child td { border-bottom: 0; }
table.data tbody tr:hover td {
  background: var(--color-surface-raised);
}
table.data td.num,
table.data th.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

/* ---------- Progress ---------- */
.progress {
  height: 8px;
  border-radius: 99px;
  background: var(--color-surface-raised);
  overflow: hidden;
}
.progress > span {
  display: block;
  height: 100%;
  background: var(--color-primary);
  border-radius: 99px;
}

/* ---------- Toggle (checkbox-based switch) ---------- */
.toggle {
  position: relative;
  display: inline-block;
  width: 46px;
  height: 26px;
  flex: none;
}
.toggle input { opacity: 0; width: 0; height: 0; }
.toggle .track {
  position: absolute;
  inset: 0;
  border-radius: 99px;
  cursor: pointer;
  background: var(--color-border-strong);
  transition: background var(--motion-duration-fast, 160ms);
}
.toggle .track::before {
  content: "";
  position: absolute;
  width: 20px;
  height: 20px;
  left: 3px;
  top: 3px;
  border-radius: 50%;
  background: #fff;
  box-shadow: var(--shadow-sm);
  transition: transform var(--motion-duration-fast, 160ms);
}
.toggle input:checked + .track { background: var(--color-primary); }
.toggle input:checked + .track::before { transform: translateX(20px); }
.toggle input:focus-visible + .track {
  box-shadow: 0 0 0 3px var(--color-primary-subtle);
}
"""


def _html_css_files(name: str, tokens: dict[str, object]) -> dict[str, str]:
    files: dict[str, str] = {}
    files["tokens.css"] = (
        f"/* Design tokens for {_comment_name(name)} — generated by Baton Design Studio.\n"
        '   Override with [data-theme="dark"] on <html> for dark mode. */\n' + css_variables(tokens)
    )
    files["components.css"] = _COMPONENTS_CSS_TEMPLATE.replace("__NAME__", _comment_name(name))
    files["index.html"] = _html_demo_page(name)
    files["README.md"] = (
        f"# {_text_name(name)} — reusable elements (HTML + CSS)\n\n"
        "1. Copy `tokens.css` and `components.css` into your project.\n"
        "2. Link both stylesheets (tokens first).\n"
        "3. Use the classes documented in `index.html`.\n\n"
        'Dark mode: set `data-theme="dark"` on the `<html>` element.\n'
    )
    return files


def _html_demo_page(name: str) -> str:
    safe = _html_name(name)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe} — components demo</title>
<link rel="stylesheet" href="tokens.css">
<link rel="stylesheet" href="components.css">
<style>
  body {{
    font-family: var(--font-body);
    background: var(--color-background);
    color: var(--color-text);
    margin: 0;
    padding: 40px 24px;
  }}
  .demo {{
    max-width: 880px;
    margin: 0 auto;
    display: grid;
    gap: 36px;
  }}
  .demo h2 {{
    font-family: var(--font-display);
    font-size: 22px;
    margin: 0 0 14px;
  }}
  .row {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }}
</style>
</head>
<body>
<main class="demo">
  <h1 style="font-family: var(--font-display)">{safe} components</h1>

  <section><h2>Buttons</h2><div class="row">
    <button class="btn btn-primary">Primary</button>
    <button class="btn btn-secondary">Secondary</button>
    <button class="btn btn-ghost">Ghost</button>
    <button class="btn btn-danger">Danger</button>
    <button class="btn btn-primary" disabled>Disabled</button>
  </div></section>

  <section><h2>Field</h2>
    <label class="field">Email
      <input type="email" placeholder="you@example.com">
      <span class="hint">We&apos;ll never share it.</span>
    </label>
  </section>

  <section><h2>Badges</h2><div class="row">
    <span class="badge badge-success">Success</span>
    <span class="badge badge-warning">Warning</span>
    <span class="badge badge-danger">Danger</span>
    <span class="badge badge-info">Info</span>
    <span class="badge badge-muted">Muted</span>
  </div></section>

  <section><h2>Alert</h2>
    <div class="alert alert-success">
      <strong>Done.</strong> Your changes were saved.
    </div>
  </section>

  <section><h2>Card</h2>
    <article class="card"><div class="card-body">
      <h3 class="card-title">Card title</h3>
      <p class="card-text">Supporting copy goes here.</p>
      <div class="card-actions">
        <button class="btn btn-primary btn-sm">Action</button>
        <button class="btn btn-ghost btn-sm">Cancel</button>
      </div>
    </div></article>
  </section>

  <section><h2>Navbar</h2>
    <nav class="navbar">
      <span class="navbar-brand">{safe}</span>
      <a href="#" class="active">Overview</a>
      <a href="#">Projects</a>
      <a href="#">Settings</a>
      <span class="spacer"></span>
      <span class="avatar avatar-sm">AK</span>
    </nav>
  </section>

  <section><h2>Table</h2>
    <div class="table-wrap"><table class="data">
      <thead><tr>
        <th>Project</th><th>Status</th><th class="num">Budget</th>
      </tr></thead>
      <tbody>
        <tr>
          <td>Atlas</td>
          <td><span class="badge badge-success">Live</span></td>
          <td class="num">$48,200</td>
        </tr>
        <tr>
          <td>Beacon</td>
          <td><span class="badge badge-warning">Review</span></td>
          <td class="num">$12,750</td>
        </tr>
      </tbody>
    </table></div>
  </section>

  <section><h2>Progress &amp; toggle</h2><div class="row">
    <div class="progress" style="width: 200px">
      <span style="width: 64%"></span>
    </div>
    <label class="toggle">
      <input type="checkbox" checked><span class="track"></span>
    </label>
  </div></section>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_files(target: str, name: str, tokens: dict[str, object]) -> dict[str, str]:
    """All generated files for `target`: filename -> content."""
    from jev_agent.core.design_codegen_react import _react_tailwind_files

    target = validate_target(target)
    if target == "react-tailwind":
        return _react_tailwind_files(name, tokens)
    return _html_css_files(name, tokens)


def bundle_zip(
    target: str,
    name: str,
    tokens: dict[str, object],
    business_elements: list[BusinessElement] | None = None,
) -> bytes:
    """A zip of every generated file, for the "download all" button.

    The zip prefix is sanitized (`_zip_safe_name`): a traversal-style name
    can never produce `../` entries on extraction. When `business_elements`
    is given, the business files for the target are included too.
    """
    files = generate_files(target, name, tokens)
    if business_elements:
        files.update(business_files(target, name, tokens, business_elements))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, content in files.items():
            prefix = _zip_safe_name(name)
            archive.writestr(f"{prefix}-{validate_target(target)}/{filename}", content)
    return buffer.getvalue()


_COMPONENT_CATALOG: dict[str, list[dict[str, str]]] = {
    "html-css": [
        {
            "filename": "tokens.css",
            "title": "Design tokens",
            "description": (
                "CSS custom properties — colors, type, spacing, radii, "
                "shadows, motion. Light + dark."
            ),
        },
        {
            "filename": "components.css",
            "title": "Component library",
            "description": (
                "Buttons, fields, badges, alerts, cards, avatars, modals, "
                "navbar, tables, progress, toggles."
            ),
        },
        {
            "filename": "index.html",
            "title": "Demo page",
            "description": "A working demo wiring every component together.",
        },
        {
            "filename": "README.md",
            "title": "Setup guide",
            "description": "Three-step integration instructions.",
        },
    ],
    "react-tailwind": [
        {
            "filename": "tokens.css",
            "title": "Design tokens",
            "description": ("CSS custom properties backing the Tailwind theme. Import once."),
        },
        {
            "filename": "tailwind.config.js",
            "title": "Tailwind theme",
            "description": ("theme.extend block mapping utilities onto the token variables."),
        },
        {
            "filename": "components.jsx",
            "title": "React components",
            "description": (
                "Button, Field/Input/Textarea/Select, Badge, Alert, Card, "
                "Avatar, Modal, Navbar, Table, Progress, Toggle — "
                "documented props."
            ),
        },
        {
            "filename": "README.md",
            "title": "Setup guide",
            "description": "Integration steps and dark-mode notes.",
        },
    ],
}


def component_catalog(target: str) -> list[dict[str, str]]:
    """The file list for the code page: filename, title, description."""
    return _COMPONENT_CATALOG[validate_target(target)]


def codegen_summary(tokens: dict[str, object]) -> dict[str, int]:
    """Counts for the code page header: variables, components, files.

    `variables` counts the distinct custom properties actually emitted by
    `css_variables` (light + dark blocks), not just the light palette size —
    the old count ignored spacing/radii/shadows/motion/typography vars.
    """
    emitted = set(re.findall(r"--([a-z0-9-]+)\s*:", css_variables(tokens)))
    return {
        "variables": len(emitted),
        "components": 11,
        "files": 4,
    }


# ---------------------------------------------------------------------------
# Business elements (issue #283)
# ---------------------------------------------------------------------------

# Extra classes the business snippets need beyond the base components
# (`.card`, `.btn`, `.badge`, `.field`, `.table`, `.progress`).
_BUSINESS_CSS = """\
/* Business elements — extra classes beyond components.css. */
.business-element { margin-bottom: 36px; }
.business-why {
  color: var(--color-text-muted);
  font-size: 13px;
  margin: 10px 2px 0;
}
.price { margin: 0 0 16px; }
.price-value {
  font-family: var(--font-display, inherit);
  font-size: 30px;
  font-weight: 700;
  letter-spacing: -0.02em;
}
.price-label { color: var(--color-text-muted); font-size: 14px; }
.checklist { list-style: none; margin: 0 0 16px; padding: 0; display: grid; gap: 8px; }
.checklist li { font-size: 14.5px; padding-left: 24px; position: relative; }
.checklist li::before {
  content: "\\2713";
  position: absolute;
  left: 0;
  color: var(--color-success);
  font-weight: 700;
}
.business-grid {
  display: grid; gap: 16px;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}
.business-grid .card-title { font-size: 17px; }
.stat-number {
  font-family: var(--font-display, inherit);
  font-size: 30px;
  font-weight: 700;
  letter-spacing: -0.02em;
  margin: 0;
}
.stat-change { margin: 8px 0 14px; }
.business-quote { margin: 0; }
.business-quote p {
  font-family: var(--font-display, inherit);
  font-size: 18px;
  line-height: 1.5;
  margin: 0 0 14px;
}
.business-quote footer { font-size: 14px; }
.business-quote footer span { color: var(--color-text-muted); }
.business-form { display: grid; gap: 16px; max-width: 480px; }
"""


def _esc(value: object) -> str:
    """Fixed catalog copy safe as HTML text."""
    return html.escape(str(value), quote=False)


def _esc_attr(value: object) -> str:
    """Fixed catalog copy safe inside a double-quoted HTML attribute."""
    return html.escape(str(value), quote=True)


def _business_section(element: BusinessElement) -> str:
    """One demo `<section>` for an element, composed of the base primitives."""
    data = element.data
    title = _esc(element.title)
    desc = _esc(element.description)
    why = _esc(element.why)
    head = f'  <section class="business-element">\n    <h2>{title}</h2>\n'
    foot = f'    <p class="business-why">{why}</p>\n  </section>\n'
    if element.layout == "card":
        points = "".join(
            f"        <li>{_esc(point)}</li>\n" for point in _data_strs(data, "points")
        )
        body = (
            '    <article class="card"><div class="card-body">\n'
            f'      <h3 class="card-title">{title}</h3>\n'
            f'      <p class="card-text">{desc}</p>\n'
            '      <p class="price">'
            f'<span class="price-value">{_esc(_coerce_str(data.get("price")))}</span> '
            f'<span class="price-label">{_esc(_coerce_str(data.get("price_label")))}</span></p>\n'
            f'      <ul class="checklist">\n{points}      </ul>\n'
            '      <div class="card-actions">\n'
            f'        <button class="btn btn-primary">{_esc(_coerce_str(data.get("cta")))}'
            "</button>\n"
            "      </div>\n"
            "    </div></article>\n"
        )
    elif element.layout == "form":
        fields = ""
        for field in _data_dicts(data, "fields"):
            ftype = _esc_attr(_coerce_str(field.get("type"), "text"))
            fname = _esc_attr(_coerce_str(field.get("name")))
            flabel = _esc(_coerce_str(field.get("label")))
            placeholder = _esc_attr(_coerce_str(field.get("placeholder")))
            fields += (
                f'      <label class="field">{flabel}\n'
                f'        <input type="{ftype}" name="{fname}" placeholder="{placeholder}">\n'
                "      </label>\n"
            )
        body = (
            '    <form class="business-form">\n'
            f"{fields}"
            '      <button class="btn btn-primary" type="submit">'
            f"{_esc(_coerce_str(data.get('submit')))}</button>\n"
            "    </form>\n"
        )
    elif element.layout == "table":
        columns = _data_strs(data, "columns")
        header = "".join(f"<th>{_esc(col)}</th>" for col in columns)
        rows_html = ""
        raw_rows = data.get("rows")
        if isinstance(raw_rows, list):
            for row in raw_rows:
                if isinstance(row, list):
                    cells = "".join(f"<td>{_esc(cell)}</td>" for cell in row)
                    rows_html += f"        <tr>{cells}</tr>\n"
        body = (
            '    <div class="table-wrap"><table class="data">\n'
            f"      <thead><tr>{header}</tr></thead>\n"
            "      <tbody>\n"
            f"{rows_html}"
            "      </tbody>\n"
            "    </table></div>\n"
        )
    elif element.layout == "stat":
        progress = max(0, min(100, _coerce_int(data.get("progress"), 0)))
        body = (
            '    <article class="card"><div class="card-body">\n'
            f'      <h3 class="card-title">{title}</h3>\n'
            f'      <p class="stat-number">{_esc(_coerce_str(data.get("value")))}</p>\n'
            f'      <p class="card-text">{_esc(_coerce_str(data.get("label")))}</p>\n'
            '      <p class="stat-change">\n'
            f'        <span class="badge badge-success">{_esc(_coerce_str(data.get("change")))}'
            "</span>\n"
            "      </p>\n"
            '      <div class="progress">\n'
            f'        <span style="width: {progress}%"></span>\n'
            "      </div>\n"
            "    </div></article>\n"
        )
    elif element.layout == "grid":
        items = "".join(
            '      <article class="card"><div class="card-body">\n'
            f'        <h3 class="card-title">{_esc(_coerce_str(item.get("title")))}</h3>\n'
            f'        <p class="card-text">{_esc(_coerce_str(item.get("text")))}</p>\n'
            "      </div></article>\n"
            for item in _data_dicts(data, "items")
        )
        body = f'    <div class="business-grid">\n{items}    </div>\n'
    elif element.layout == "quote":
        body = (
            '    <article class="card"><div class="card-body">\n'
            '      <blockquote class="business-quote">\n'
            f"        <p>&ldquo;{_esc(_coerce_str(data.get('text')))}&rdquo;</p>\n"
            "        <footer><strong>"
            f"{_esc(_coerce_str(data.get('author')))}</strong> — "
            f"<span>{_esc(_coerce_str(data.get('role')))}</span></footer>\n"
            "      </blockquote>\n"
            "    </div></article>\n"
        )
    elif element.layout == "checklist":
        items = "".join(f"        <li>{_esc(item)}</li>\n" for item in _data_strs(data, "items"))
        body = (
            '    <article class="card"><div class="card-body">\n'
            f'      <h3 class="card-title">{title}</h3>\n'
            f'      <p class="card-text">{desc}</p>\n'
            f'      <ul class="checklist">\n{items}      </ul>\n'
            "    </div></article>\n"
        )
    else:  # unknown layout: title + description only, never a crash
        body = f"    <p>{desc}</p>\n"
    return head + body + foot


def _business_html_page(name: str, elements: list[BusinessElement]) -> str:
    safe = _html_name(name)
    sections = "".join(_business_section(element) for element in elements)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe} — business elements demo</title>
<link rel="stylesheet" href="tokens.css">
<link rel="stylesheet" href="components.css">
<link rel="stylesheet" href="business-elements.css">
<style>
  body {{
    font-family: var(--font-body);
    background: var(--color-background);
    color: var(--color-text);
    margin: 0;
    padding: 40px 24px;
  }}
  .demo {{ max-width: 880px; margin: 0 auto; }}
  .demo h2 {{
    font-family: var(--font-display);
    font-size: 22px;
    margin: 0 0 14px;
  }}
</style>
</head>
<body>
<main class="demo">
  <h1 style="font-family: var(--font-display)">{safe} business elements</h1>
  <p style="color: var(--color-text-muted)">Demo sections for the elements
  proposed from the business context. Copy the markup into your pages.</p>

{sections}</main>
</body>
</html>
"""


def business_files(
    target: str,
    name: str,
    tokens: dict[str, object],
    elements: list[BusinessElement],
) -> dict[str, str]:
    """Generated files for the enabled business elements: filename -> content.

    `html-css` gets `business-elements.html` (one demo section per element)
    plus `business-elements.css` (the few extra classes the snippets need
    beyond the base components). `react-tailwind` gets a single
    `business-components.jsx` (one exported component per element, built on
    the base components). Empty `elements` yields {}. Business context text
    itself is never rendered — only the catalog's fixed element content.
    """
    if not elements:
        return {}
    target = validate_target(target)
    if target == "react-tailwind":
        from jev_agent.core.design_codegen_react import _business_components_jsx

        return {"business-components.jsx": _business_components_jsx(name, elements)}
    return {
        "business-elements.html": _business_html_page(name, elements),
        "business-elements.css": (
            f"/* Business elements for {_comment_name(name)} — generated by "
            "Baton Design Studio.\n"
            "   Load after components.css. */\n" + _BUSINESS_CSS
        ),
    }


_BUSINESS_CATALOG: dict[str, list[dict[str, str]]] = {
    "html-css": [
        {
            "filename": "business-elements.html",
            "title": "Business elements demo",
            "description": "Demo sections for the business elements, one per element.",
        },
        {
            "filename": "business-elements.css",
            "title": "Business element styles",
            "description": "The extra classes the business snippets need beyond the base.",
        },
    ],
    "react-tailwind": [
        {
            "filename": "business-components.jsx",
            "title": "Business components",
            "description": "One exported React component per business element.",
        },
    ],
}


def business_catalog(target: str, elements: list[BusinessElement]) -> list[dict[str, str]]:
    """The business file list for the code page: filename, title, description.

    Entries match `business_files` keys; [] when `elements` is empty.
    """
    if not elements:
        return []
    return list(_BUSINESS_CATALOG[validate_target(target)])
