"""A user-level design system library: named, versioned, reusable design systems.

Stored under `~/.jev/design-systems/<name>/`:

- `design-system.md`: the human-readable system (tokens, components, voice and patterns)
- `tokens.json`: the structured tokens (colors, type_scale, spacing, radii)
- `meta.json`: `{"version", "updated_at", ...}` plus whatever `meta_extra` carried
  (e.g. name, app_type, stack, source)
- `design-system.v<n>.md` / `tokens.v<n>.json`: snapshots kept on every regenerate

The layout mirrors a repo's design system (`core/design_system.py`) so a library
system can be installed into any repo with `jev design use <name>`, and a repo's
system can be exported back with plain file copies. The atomic-write and
never-raises-on-corrupt conventions are the same as `core/design_system.py`.

Token export helpers (`export_tokens_w3c` / `export_tokens_tailwind` /
`export_tokens_css`) render the structured tokens for other toolchains; each format
round-trips the token values exactly (numbers keep their value with a `px` suffix
in the text formats, kept verbatim as `$value` in the W3C JSON).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from jev_agent.core.design_system import (
    META_JSON,
    SYSTEM_MD,
    TOKENS_JSON,
    DesignSystem,
    _stage_temp,
    _warn,
    snapshot_corrupt_files,
)

LIBRARY_DIRNAME = "design-systems"
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PERMS = 0o700


def library_dir() -> Path:
    """`~/.jev/design-systems/`, created with mode 0700 on demand."""
    d = Path.home() / ".jev" / LIBRARY_DIRNAME
    d.mkdir(parents=True, exist_ok=True, mode=_PERMS)
    # mkdir's mode is umask-filtered, so enforce 0700 explicitly.
    os.chmod(d, _PERMS)
    return d


def validate_library_name(name: str) -> str:
    """The name must be one path segment: alphanumerics, `_` and `-`, 1-64 chars.

    Returns the name unchanged; raises ValueError otherwise. The first character
    must be alphanumeric, so `.`, `..`, `/`, leading dashes and empty names are
    all rejected — a validated name can never escape the library directory.
    """
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise ValueError(
            f"invalid library design system name {name!r}: use 1-64 characters of "
            "letters, digits, `_` and `-`, starting with a letter or digit"
        )
    return name


def library_system_path(name: str) -> Path:
    """`<library_dir>/<name>/`; the name is validated first."""
    return library_dir() / validate_library_name(name)


def list_library() -> list[str]:
    """Sorted names of the library's design systems (dirs that contain meta.json).

    Never raises: an unreadable library directory reads as empty.
    """
    try:
        entries = list(library_dir().iterdir())
    except OSError:
        return []
    return sorted(
        entry.name for entry in entries if entry.is_dir() and (entry / META_JSON).exists()
    )


def load_library_system(name: str) -> DesignSystem | None:
    """The library's current design system for `name`, or None when there is none
    (or it is corrupt). Never raises on corrupt files: warns and returns None,
    the same convention as `core/design_system.get_design_system`."""
    d = library_system_path(name)
    try:
        meta = json.loads((d / META_JSON).read_text(encoding="utf-8"))
        markdown = (d / SYSTEM_MD).read_text(encoding="utf-8")
        tokens = json.loads((d / TOKENS_JSON).read_text(encoding="utf-8"))
        version = meta["version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError(f"meta.json version must be an integer, got {version!r}")
        return DesignSystem(
            version=version,
            updated_at=str(meta["updated_at"]),
            markdown=markdown,
            tokens=tokens,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        if any((d / filename).exists() for filename in (META_JSON, SYSTEM_MD, TOKENS_JSON)):
            _warn(f"library design system {name!r} is unreadable and will be ignored ({exc})")
        return None


def save_library_system(
    name: str,
    markdown: str,
    tokens: dict[str, object],
    meta_extra: dict[str, object] | None = None,
) -> int:
    """Write a library design system, keeping the old one as
    `design-system.v<n>.md` / `tokens.v<n>.json`. Returns the new version number
    (starts at 1).

    `meta.json` holds `{"version", "updated_at"}` plus `meta_extra` (e.g. name,
    app_type, stack, source); `version`/`updated_at` always win so `meta_extra`
    cannot corrupt the versioning. Writes are staged to temp files and renamed
    into place together, the same atomic pattern as `core/design_system.py`.
    """
    d = library_system_path(name)
    d.mkdir(parents=True, exist_ok=True, mode=_PERMS)
    os.chmod(d, _PERMS)
    current = load_library_system(name)
    version = (current.version + 1) if current is not None else 1
    if current is None and any(
        (d / filename).exists() for filename in (META_JSON, SYSTEM_MD, TOKENS_JSON)
    ):
        # Same rule as core/design_system: an unreadable system is preserved
        # before it is overwritten, never silently restarted at version 1.
        snapshots = snapshot_corrupt_files(d)
        if snapshots:
            _warn(
                f"existing library design system {name!r} is unreadable; preserved as "
                + ", ".join(snapshots)
                + " before overwriting"
            )
    if current is not None:
        for filename, snapshot in (
            (SYSTEM_MD, f"design-system.v{current.version}.md"),
            (TOKENS_JSON, f"tokens.v{current.version}.json"),
        ):
            src = d / filename
            if src.exists():
                tmp = _stage_temp(d / snapshot, src.read_text(encoding="utf-8"))
                try:
                    os.replace(tmp, d / snapshot)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
    updated_at = datetime.now(UTC).isoformat(timespec="seconds")
    meta: dict[str, object] = dict(meta_extra or {})
    meta["version"] = version
    meta["updated_at"] = updated_at
    pending = {
        SYSTEM_MD: markdown,
        TOKENS_JSON: json.dumps(tokens, indent=2) + "\n",
        META_JSON: json.dumps(meta, indent=2) + "\n",
    }
    temps: dict[str, Path] = {}
    try:
        for filename, text in pending.items():
            temps[filename] = _stage_temp(d / filename, text)
        # All three staged: commit them together. A crash in this loop is the only
        # window where files can mix versions, and it holds no writes — just renames.
        for filename, tmp in temps.items():
            os.replace(tmp, d / filename)
    except BaseException:
        for tmp in temps.values():
            tmp.unlink(missing_ok=True)
        raise
    return version


def update_library_meta(name: str, patch: dict[str, object]) -> dict[str, object] | None:
    """Atomically patch a library system's `meta.json` (issue #283).

    Patch values overwrite; a patch value of None deletes the key. This is
    metadata, not a new system version: no version bump, no `updated_at`
    change, no snapshots. Returns the new meta dict, or None when the system
    does not exist (or its meta is unreadable). Raises ValueError on an
    invalid name. The write is staged to a temp file and renamed into place
    with `os.replace`, reusing `_stage_temp`.
    """
    d = library_system_path(name)
    meta_path = d / META_JSON
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    new_meta: dict[str, object] = {str(key): value for key, value in meta.items()}
    for key, value in patch.items():
        if value is None:
            new_meta.pop(str(key), None)
        else:
            new_meta[str(key)] = value
    tmp = _stage_temp(meta_path, json.dumps(new_meta, indent=2) + "\n")
    try:
        os.replace(tmp, meta_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return new_meta


def delete_library_system(name: str) -> bool:
    """Delete a library design system. False when it does not exist; warns and
    returns False when the deletion itself fails."""
    d = library_system_path(name)
    if not d.is_dir():
        return False
    try:
        shutil.rmtree(d)
    except OSError as exc:
        _warn(f"could not delete library design system {name!r} ({exc})")
        return False
    return True


# ---- token export -----------------------------------------------------------------------------

# (tokens.json key, W3C group name, W3C $type)
_TOKEN_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("colors", "color", "color"),
    ("type_scale", "fontSize", "dimension"),
    ("spacing", "spacing", "dimension"),
    ("radii", "borderRadius", "borderRadius"),
)


def _section(tokens: dict[str, object], key: str) -> dict[str, object]:
    section = tokens.get(key)
    return {str(k): v for k, v in section.items()} if isinstance(section, dict) else {}


def _flat_colors(tokens: dict[str, object]) -> tuple[dict[str, object], dict[str, object] | None]:
    """(light, dark-or-None) color palettes from a token set.

    Theme-"both" systems nest colors as `{"light": {...}, "dark": {...}}`;
    flat systems keep colors as a plain role -> value map. Returning the
    unwrapped palettes (instead of the raw nested dict) is what keeps the
    exporters from stringifying dicts into CSS/JS output.
    """
    colors = _section(tokens, "colors")
    light_raw = colors.get("light")
    dark_raw = colors.get("dark")
    if isinstance(light_raw, dict) or isinstance(dark_raw, dict):
        light = {str(k): v for k, v in light_raw.items()} if isinstance(light_raw, dict) else {}
        dark = {str(k): v for k, v in dark_raw.items()} if isinstance(dark_raw, dict) else None
        return light, dark
    return colors, None


def _css_key(key: str) -> str:
    """A token key made safe for a CSS custom-property name: kebab-case, and
    anything outside `[a-z0-9-]` collapsed to `-` (a key carrying whitespace,
    newlines or `;`/`}` can no longer break out of its declaration)."""
    return re.sub(r"[^a-z0-9-]+", "-", _kebab(key)).strip("-")


def _css_value(value: object) -> str:
    """A token value made safe for a CSS declaration: control characters and
    `;`/`{`/`}` (declaration/ block terminators) are stripped so a hostile or
    malformed token value cannot inject extra declarations."""
    return re.sub(r"[\x00-\x1f\x7f;{}]", "", _px(value)).strip()


def _px(value: object) -> str:
    """A length for the text formats: numbers keep their exact value with a `px`
    suffix (15 -> "15px"); everything else is stringified unchanged."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value}px"
    return str(value)


def _kebab(text: str) -> str:
    return text.replace("_", "-").lower()


def export_tokens_w3c(tokens: dict[str, object]) -> dict[str, object]:
    """W3C Design Tokens-ish JSON: `{group: {key: {"$value": v, "$type": t}}}`.

    `$value` keeps the token value verbatim (colors as strings, sizes as numbers),
    so the export round-trips the tokens exactly. A theme-"both" system exports
    its light palette as `color` and its dark palette as `colorDark`, rather
    than nesting a dict inside a `$value`.
    """
    out: dict[str, object] = {}
    light, dark = _flat_colors(tokens)
    out["color"] = {key: {"$value": value, "$type": "color"} for key, value in light.items()}
    if dark is not None:
        out["colorDark"] = {key: {"$value": value, "$type": "color"} for key, value in dark.items()}
    for source_key, group, value_type in _TOKEN_SECTIONS[1:]:
        out[group] = {
            key: {"$value": value, "$type": value_type}
            for key, value in _section(tokens, source_key).items()
        }
    return out


def export_tokens_tailwind(tokens: dict[str, object]) -> str:
    """A Tailwind config extending the theme with the tokens (colors verbatim,
    sizes as `px` strings).

    For a theme-"both" system the light palette is the Tailwind `colors`
    value: a nested `{light, dark}` object is not a usable color value, so
    the dark palette is not emitted here (use the CSS export for dark mode).
    """
    light, _dark = _flat_colors(tokens)
    theme = {
        "colors": light,
        "fontSize": {k: _px(v) for k, v in _section(tokens, "type_scale").items()},
        "spacing": {k: _px(v) for k, v in _section(tokens, "spacing").items()},
        "borderRadius": {k: _px(v) for k, v in _section(tokens, "radii").items()},
    }
    lines = [
        "// Exported by `jev design export --format tailwind`: theme.extend tokens.",
        "module.exports = {",
        "  theme: {",
        "    extend: {",
    ]
    items = list(theme.items())
    for index, (key, values) in enumerate(items):
        rendered = json.dumps(values, indent=6)
        comma = "," if index < len(items) - 1 else ""
        lines.append(f"      {key}: {rendered}{comma}")
    lines.extend(["    }", "  }", "};"])
    return "\n".join(lines) + "\n"


_CSS_PREFIXES: tuple[tuple[str, str], ...] = (
    ("colors", "color"),
    ("type_scale", "font-size"),
    ("spacing", "spacing"),
    ("radii", "radius"),
)


def export_tokens_css(tokens: dict[str, object]) -> str:
    """CSS custom properties under `:root` (kebab-case, `px` units for sizes).

    A theme-"both" system emits its light palette under `:root` and its dark
    palette under `[data-theme="dark"]`; token keys are sanitized and values
    stripped of declaration terminators, so the output is always a parseable
    stylesheet.
    """
    lines = [":root {"]
    light, dark = _flat_colors(tokens)
    for key, value in light.items():
        css_key = _css_key(key)
        if css_key:
            lines.append(f"  --color-{css_key}: {_css_value(value)};")
    for source_key, prefix in _CSS_PREFIXES[1:]:
        for key, value in _section(tokens, source_key).items():
            css_key = _css_key(key)
            if css_key:
                lines.append(f"  --{prefix}-{css_key}: {_css_value(value)};")
    lines.append("}")
    if dark is not None and dark != light:
        lines.append("")
        lines.append('[data-theme="dark"] {')
        for key, value in dark.items():
            css_key = _css_key(key)
            if css_key:
                lines.append(f"  --color-{css_key}: {_css_value(value)};")
        lines.append("}")
    return "\n".join(lines) + "\n"
