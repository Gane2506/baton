"""A repo's design system: Claude-Styles-like buildable, storable, reusable design tokens.

Stored under `<repo>/.jev/design/`:

- `design-system.md`: the human-readable system (tokens, components, voice and patterns)
- `tokens.json`: the structured tokens (colors, type_scale, spacing, radii)
- `meta.json`: `{"version": <int>, "updated_at": <iso>}` for the current pair
- `design-system.v<n>.md` / `tokens.v<n>.json`: snapshots kept on every regenerate

Everything here is stdlib-only, and nothing raises on corrupt files: `get_design_system`
returns None and `list_versions` skips what it cannot read, warning on stderr.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import warnings
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from jev_agent.core.models import DesignDoc

DESIGN_DIRNAME = "design"  # under `<repo>/.jev/`
SYSTEM_MD = "design-system.md"
TOKENS_JSON = "tokens.json"
META_JSON = "meta.json"
# Cap for how much of the markdown goes into an agent prompt: prompts need the compact
# picture, not the whole document.
PROMPT_MD_LIMIT = 2000


class DesignSystem(BaseModel):
    """The current design system of one repo."""

    version: int = Field(ge=1)
    updated_at: str  # ISO timestamp
    markdown: str
    tokens: dict[str, object] = Field(default_factory=dict)


def design_dir(repo: Path) -> Path:
    """`<repo>/.jev/design/`; the repo's state dir is created on demand by the store."""
    return repo / ".jev" / DESIGN_DIRNAME


def _warn(message: str) -> None:
    warnings.warn(message, stacklevel=3)
    print(f"warning: {message}", file=sys.stderr)


def get_design_system(repo: Path) -> DesignSystem | None:
    """The repo's current design system, or None when it has none (or it is corrupt).

    Never raises: a corrupt or partially written system warns on stderr and returns None.
    A non-integer `version` in meta.json counts as corrupt (a float version is
    not silently truncated to an int).
    """
    d = design_dir(repo)
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
        if any((d / name).exists() for name in (META_JSON, SYSTEM_MD, TOKENS_JSON)):
            _warn(f"design system in {d} is unreadable and will be ignored ({exc})")
        return None


def _stage_temp(path: Path, text: str) -> Path:
    """Write `text` to a temp file next to `path` (fsynced); the caller renames it
    into place with `os.replace`. The temp is removed when the write fails."""
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return Path(tmp_name)


def _write_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` atomically: temp file in the same directory (so the
    rename cannot cross filesystems), fsynced, then `os.replace()`d into place.
    A crash leaves the old file or the new one, never a torn write."""
    tmp = _stage_temp(path, text)
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def snapshot_corrupt_files(d: Path) -> list[str]:
    """Preserve whatever current files exist in `d` under `*.corrupt-<ts>` names.

    Used by the save functions when the current system is unreadable: saving
    over a corrupt system must not silently destroy it. Returns the snapshot
    file names created (empty when no current files exist). Never raises on a
    partially readable directory — a failed copy warns and is skipped.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    snapshots: list[str] = []
    for current_name, snapshot_name in (
        (SYSTEM_MD, f"design-system.corrupt-{stamp}.md"),
        (TOKENS_JSON, f"tokens.corrupt-{stamp}.json"),
        (META_JSON, f"meta.corrupt-{stamp}.json"),
    ):
        src = d / current_name
        if not src.exists():
            continue
        try:
            _write_atomic(d / snapshot_name, src.read_text(encoding="utf-8"))
        except OSError as exc:
            _warn(f"could not snapshot corrupt {current_name} ({exc})")
            continue
        snapshots.append(snapshot_name)
    return snapshots


def save_design_system(
    repo: Path,
    markdown: str,
    tokens: dict[str, object],
    meta_extra: dict[str, object] | None = None,
) -> int:
    """Write a new design system, keeping the old one as `design-system.v<n>.md` /
    `tokens.v<n>.json`. Returns the new version number (starts at 1).

    `meta_extra` (e.g. `{"source": "library:<name>", "app_type": ..., "stack": ...}`)
    is merged into `meta.json` next to `version`/`updated_at`; those two always win
    so extras cannot corrupt the versioning.

    The three current files (`design-system.md`, `tokens.json`, `meta.json`) are
    written to temp names and renamed into place together: a crash before the
    renames leaves the old version intact (temps are cleaned up), never a mix of
    old and new files that `get_design_system` would silently accept.
    """
    d = design_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    current = get_design_system(repo)
    version = (current.version + 1) if current is not None else 1
    if current is None and any((d / name).exists() for name in (SYSTEM_MD, TOKENS_JSON, META_JSON)):
        # Unreadable current files are not "no system": snapshot them first so
        # the overwrite below cannot silently restart versioning at 1.
        snapshots = snapshot_corrupt_files(d)
        if snapshots:
            _warn(
                "existing design system is unreadable; preserved as "
                + ", ".join(snapshots)
                + " before overwriting"
            )
    if current is not None:
        for name, vname in (
            (SYSTEM_MD, f"design-system.v{current.version}.md"),
            (TOKENS_JSON, f"tokens.v{current.version}.json"),
        ):
            src = d / name
            if src.exists():
                _write_atomic(d / vname, src.read_text(encoding="utf-8"))
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
        for name, text in pending.items():
            temps[name] = _stage_temp(d / name, text)
        # All three staged: commit them together. A crash in this loop is the only
        # window where files can mix versions, and it holds no writes — just renames.
        for name, tmp in temps.items():
            os.replace(tmp, d / name)
    except BaseException:
        for tmp in temps.values():
            tmp.unlink(missing_ok=True)
        raise
    return version


def list_versions(repo: Path) -> list[int]:
    """Every stored version, oldest first: snapshots plus the current one.

    Never raises: unreadable snapshots are skipped (with a warning), and a repo with no
    design system gives an empty list.
    """
    d = design_dir(repo)
    try:
        names = [p.name for p in d.iterdir()]
    except OSError:
        return []
    versions: set[int] = set()
    for name in names:
        if name.startswith("design-system.v") and name.endswith(".md"):
            try:
                versions.add(int(name[len("design-system.v") : -len(".md")]))
            except ValueError:
                _warn(f"ignoring odd design snapshot name: {name}")
    current = get_design_system(repo)
    if current is not None:
        versions.add(current.version)
    return sorted(versions)


def render_for_prompt(system: DesignSystem | None) -> str:
    """The system as compact text for injection into an agent prompt.

    Empty when there is no system: the caller passes that straight through so the role
    prompt can branch on it.
    """
    if system is None:
        return ""
    lines = [f"Design system v{system.version} (updated {system.updated_at}):"]
    tokens = system.tokens
    colors = tokens.get("colors")
    if isinstance(colors, dict) and colors:
        light = colors.get("light")
        dark = colors.get("dark")
        if isinstance(light, dict) and isinstance(dark, dict):
            # Theme-"both" systems nest colors as {light, dark}: unwrap them
            # instead of stringifying the dicts into the prompt.
            for label, palette in (("light", light), ("dark", dark)):
                flat = ", ".join(f"{k} {v}" for k, v in palette.items() if not isinstance(v, dict))
                if flat:
                    lines.append(f"Colors ({label}): {flat}")
        else:
            flat = ", ".join(f"{k} {v}" for k, v in colors.items() if not isinstance(v, dict))
            if flat:
                lines.append("Colors: " + flat)
    for key in ("type_scale", "spacing", "radii"):
        value = tokens.get(key)
        if isinstance(value, dict) and value:
            lines.append(f"{key}: " + ", ".join(f"{k} {v}" for k, v in value.items()))
    body = system.markdown.strip()
    if len(body) > PROMPT_MD_LIMIT:
        body = body[:PROMPT_MD_LIMIT].rstrip() + "\n…"
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def design_context_for_coder(run_design: DesignDoc | None) -> str:
    """The run's `DesignDoc` as prompt context for the coder (empty when there is none).

    Screens and components, not prose: the coder needs the structure the UI is built from.
    """
    if run_design is None:
        return ""
    lines = [f"## UI design (v{run_design.version}): {run_design.title}"]
    if run_design.summary:
        lines.append(run_design.summary)
    for screen in run_design.screens:
        lines.append(f"### Screen: {screen.name}")
        if screen.description:
            lines.append(screen.description)
        if screen.components:
            lines.append("Components: " + ", ".join(screen.components))
    if run_design.notes:
        lines.append("Design notes:")
        lines.extend(f"- {note}" for note in run_design.notes)
    return "\n".join(lines)


STARTER_TOKENS: dict[str, object] = {
    "colors": {
        "primary": "#2563EB",
        "primary_hover": "#1D4ED8",
        "background": "#FFFFFF",
        "surface": "#F8FAFC",
        "text": "#0F172A",
        "text_muted": "#64748B",
        "border": "#E2E8F0",
        "success": "#16A34A",
        "warning": "#D97706",
        "danger": "#DC2626",
    },
    "type_scale": {"xs": 12, "sm": 13, "base": 15, "lg": 18, "xl": 22, "2xl": 28},
    "spacing": {"xs": 4, "sm": 8, "md": 16, "lg": 24, "xl": 32, "2xl": 48},
    "radii": {"sm": 6, "md": 10, "lg": 14, "full": 9999},
}

STARTER_MARKDOWN = """# Design System

A starter system. Regenerate it with `jev design generate` once the app has a shape,
or edit this file and `tokens.json` directly — both are versioned on every save.

## Tokens

The structured values live in `tokens.json` (colors, type_scale, spacing, radii).
Highlights: primary `#2563EB`, text `#0F172A` on `#FFFFFF`, muted text `#64748B`,
borders `#E2E8F0`. Base type is 15px; spacing steps 4/8/16/24/32/48; radii 6/10/14/full.

## Components

- **Button** (primary / secondary / ghost): full-width on mobile, medium radius, clear
  focus ring. Destructive actions use the danger color and ask for confirmation.
- **Input / Select / Textarea**: one shared field style — same border, padding and
  radius everywhere. Labels in Title Case; errors name the field and say how to fix it.
- **Card**: surface background, medium radius, one border. One idea per card.
- **Badge**: status colors only (success / warning / danger / muted); never for decoration.
- **Modal**: one question, one primary action, Esc dismisses.
- **Tabs**: one row, the active tab underlined in the primary color.

## Voice and patterns

- Plain-language labels and buttons ("Save", not "Persist changes").
- Errors explain what happened and what to do next.
- Confirm destructive actions; never surprise the user.
- Loading states name what is loading; empty states say what to do first.

## Screen specs (generated)

None yet — `jev design generate` specs the screens here.
"""
