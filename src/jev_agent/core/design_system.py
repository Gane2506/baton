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
import shutil
import sys
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
    """
    d = design_dir(repo)
    try:
        meta = json.loads((d / META_JSON).read_text(encoding="utf-8"))
        markdown = (d / SYSTEM_MD).read_text(encoding="utf-8")
        tokens = json.loads((d / TOKENS_JSON).read_text(encoding="utf-8"))
        return DesignSystem(
            version=int(meta["version"]),
            updated_at=str(meta["updated_at"]),
            markdown=markdown,
            tokens=tokens,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        if any((d / name).exists() for name in (META_JSON, SYSTEM_MD, TOKENS_JSON)):
            _warn(f"design system in {d} is unreadable and will be ignored ({exc})")
        return None


def save_design_system(repo: Path, markdown: str, tokens: dict[str, object]) -> int:
    """Write a new design system, keeping the old one as `design-system.v<n>.md` /
    `tokens.v<n>.json`. Returns the new version number (starts at 1)."""
    d = design_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    current = get_design_system(repo)
    version = (current.version + 1) if current is not None else 1
    if current is not None:
        for name, vname in (
            (SYSTEM_MD, f"design-system.v{current.version}.md"),
            (TOKENS_JSON, f"tokens.v{current.version}.json"),
        ):
            src = d / name
            if src.exists():
                shutil.copy2(src, d / vname)
    updated_at = datetime.now(UTC).isoformat(timespec="seconds")
    (d / SYSTEM_MD).write_text(markdown, encoding="utf-8")
    (d / TOKENS_JSON).write_text(json.dumps(tokens, indent=2) + "\n", encoding="utf-8")
    (d / META_JSON).write_text(
        json.dumps({"version": version, "updated_at": updated_at}, indent=2) + "\n",
        encoding="utf-8",
    )
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
        lines.append("Colors: " + ", ".join(f"{k} {v}" for k, v in colors.items()))
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
