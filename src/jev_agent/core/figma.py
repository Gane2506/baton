"""Figma: read Figma files over the REST API, and generate the Plugin API
code the official Figma MCP server runs through its `use_figma` tool.

The honest split of what Figma allows (see `docs/figma.md`):

- **REST (`https://api.figma.com`) is largely read-only.** It reads file
  JSON, components, styles, images and comments, and validates a token via
  `GET /v1/me`. It cannot create files, frames, nodes or components, and
  `file_variables` is Enterprise-only. This module's `FigmaClient` is that
  read path, plus the token check.
- **`use_figma` is the write path.** The official Figma MCP server runs
  Plugin API JavaScript inside the file, and the Plugin API can create,
  edit and delete any object — variables (no Enterprise gate there),
  styles, components, variants, auto-layout, text, images — and prototype
  reactions (`setReactionsAsync`, including SMART_ANIMATE navigation).
  Baton cannot call the MCP server itself, so this module *generates* the
  exact Plugin API code: the dashboard shows it (copy button) and Baton's
  agents run it through `use_figma` when the Figma MCP server is
  connected. Nothing here pretends the REST API can write to the canvas.

Two-way sync: `file_to_tokens` reads a Figma file into Baton token shape,
`reconcile_tokens` diffs it against the system's current tokens, and the
dashboard applies the diff as a new library version — so a file changed
outside Baton flows back in, and a Baton change flows out as generated
`use_figma` code. `render_use_figma_edit` is the granular surface: one
small, inspect-first change per call (one variable, one node property,
one reaction), following Figma's own guidance — inspect first, change in
small steps, verify with `get_screenshot` after.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from jev_agent.app.errors import JevError
from jev_agent.core.design_library import _flat_colors
from jev_agent.core.design_system import STARTER_TOKENS

API_BASE = "https://api.figma.com"
MCP_URL = "https://mcp.figma.com/mcp"
MCP_DESKTOP_URL = "http://127.0.0.1:3845/mcp"
TOKEN_ENV = "FIGMA_API_TOKEN"
MCP_SERVER_NAME = "figma"
TOKEN_PREFIX = "figd_"

#: Sections a Baton token set carries; `file_to_tokens` always returns all four.
TOKEN_SECTIONS = ("colors", "type_scale", "spacing", "radii")

_HEX6 = re.compile(r"^#[0-9a-fA-F]{6}$")
_NODE_ID = re.compile(r"^\d+:\d+$")
_NAME_BAD = re.compile(r"[^a-z0-9]+")
_STYLE_NAME_BAD = re.compile(r"[^A-Za-z0-9]+")
_FIGMA_HOSTS = ("www.figma.com", "figma.com")


class FigmaError(JevError):
    """A Figma API or Figma input problem, in plain language."""


@dataclass(frozen=True)
class FigmaFileRef:
    """A Figma file identified from a URL: its key, plus a node when given."""

    file_key: str
    node_id: str | None = None

    @property
    def url(self) -> str:
        """The canonical file URL (with the node when one was given)."""
        base = f"https://www.figma.com/design/{self.file_key}/"
        if self.node_id:
            return f"{base}?node-id={self.node_id.replace(':', '-')}"
        return base


def parse_figma_url(url: str) -> FigmaFileRef:
    """Parse a Figma file URL into its file key (and node id, when present).

    Accepts `/design/`, `/file/` and `/proto/` URLs; `?node-id=12-34` (or the
    percent-encoded `12%3A34`) becomes node id `12:34`. Anything else raises
    `FigmaError` with what was wrong — a pasted non-Figma URL is a user error,
    not a crash.
    """
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise FigmaError(f"not a Figma file URL: {raw!r}") from exc
    if parts.scheme not in ("http", "https") or parts.hostname not in _FIGMA_HOSTS:
        raise FigmaError(f"not a Figma file URL: {raw!r} (expected a figma.com link)")
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) < 2 or segments[0] not in ("design", "file", "proto"):
        raise FigmaError(
            f"not a Figma file URL: {raw!r} (expected "
            "figma.com/design/<key>/…, /file/<key>/… or /proto/<key>/…)"
        )
    file_key = segments[1]
    if not file_key or len(file_key) > 128:
        raise FigmaError(f"not a Figma file URL: {raw!r} (no file key found)")
    node_id: str | None = None
    query = parse_qs(parts.query)
    raw_node = query.get("node-id", [None])[0]
    if raw_node:
        candidate = unquote(raw_node).replace("-", ":")
        if not _NODE_ID.match(candidate):
            raise FigmaError(f"not a Figma node id: {raw_node!r} (expected like 12-34)")
        node_id = candidate
    return FigmaFileRef(file_key=file_key, node_id=node_id)


def suggest_library_name(file_name: str) -> str:
    """A library-safe name from a Figma file's name (lowercased, runs of
    non-alphanumerics become `-`, capped at 64 chars) — always passes
    `validate_library_name`, falling back to "figma-import"."""
    candidate = _NAME_BAD.sub("-", (file_name or "").lower()).strip("-")[:64]
    return candidate or "figma-import"


def rgba_to_hex(color: object) -> str | None:
    """A Figma `{r, g, b, a}` color (0..1 floats) as `#rrggbb`, or None.

    Alpha is ignored: Baton color tokens carry no opacity. Out-of-range or
    missing channels are not a color at all, so they return None instead of
    inventing one.
    """
    if not isinstance(color, dict):
        return None
    try:
        channels = [float(color[channel]) for channel in ("r", "g", "b")]
    except (KeyError, TypeError, ValueError):
        return None
    if any(not math.isfinite(value) or value < 0 or value > 1 for value in channels):
        return None
    return "#" + "".join(f"{round(value * 255):02x}" for value in channels)


def rgb_to_figma(hex_color: str) -> dict[str, float]:
    """A `#rrggbb` color as Figma `{r, g, b}` floats (0..1)."""
    value = hex_color.lstrip("#")
    return {
        "r": int(value[0:2], 16) / 255,
        "g": int(value[2:4], 16) / 255,
        "b": int(value[4:6], 16) / 255,
    }


def _token_name(raw: str) -> str:
    """A style/role name made safe for a Baton token key: the last path segment,
    lowercased, runs of non-alphanumerics collapsed to `_`."""
    last = (raw or "").strip().split("/")[-1].strip()
    name = _NAME_BAD.sub("_", last.lower()).strip("_")
    return name or "unnamed"


def _is_hex6(value: object) -> bool:
    return isinstance(value, str) and _HEX6.match(value) is not None


# ---- REST client ----------------------------------------------------------------------------


class FigmaClient:
    """The Figma REST API (read-only, plus the token check).

    `transport` is the `httpx.MockTransport` seam tests use; production passes
    none. The token is never logged — only the `X-Figma-Token` header carries
    it, and `FigmaError` messages never include it.
    """

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None) -> None:
        if not token or not token.strip():
            raise FigmaError("no Figma token given — add one on Tools → APIs first")
        self._client = httpx.Client(
            base_url=API_BASE,
            headers={"X-Figma-Token": token.strip()},
            timeout=15.0,
            transport=transport,
        )

    def close(self) -> None:
        """Close the underlying HTTP client (tests and callers that keep one)."""
        self._client.close()

    def __enter__(self) -> FigmaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str) -> dict[str, object]:
        try:
            response = self._client.get(path)
        except httpx.TimeoutException as exc:
            raise FigmaError(f"Figma did not answer in time ({exc}) — try again") from exc
        except httpx.HTTPError as exc:
            raise FigmaError(f"could not reach Figma: {exc}") from exc
        if response.status_code == 200:
            data = response.json()
            if not isinstance(data, dict):
                raise FigmaError(f"Figma answered {path} with an unexpected response")
            return data
        if response.status_code == 401:
            raise FigmaError(
                "Figma rejected the token (401) — check the Personal Access Token "
                "on Tools → APIs (Settings → Security in Figma, it starts with figd_)"
            )
        if response.status_code == 403:
            raise FigmaError(
                "Figma refused (403) — the token cannot read this file. "
                "Check the file is shared with the token's account."
            )
        if response.status_code == 404:
            raise FigmaError("Figma has no such file (404) — check the file key")
        if response.status_code == 429:
            raise FigmaError("Figma rate-limited the request (429) — wait a minute and retry")
        raise FigmaError(f"Figma answered HTTP {response.status_code} for {path}")

    def me(self) -> dict[str, object]:
        """`GET /v1/me`: who the token belongs to. The token check — a 401 here
        means the stored token is wrong, before any file is touched."""
        return self._get("/v1/me")

    def validate_token(self) -> str:
        """The token check as a display name (`handle`, else `email`, else `?`):
        the dashboard flashes "Connected as …" without ever showing the token."""
        data = self.me()
        handle = data.get("handle")
        if isinstance(handle, str) and handle.strip():
            return handle.strip()
        email = data.get("email")
        if isinstance(email, str) and email.strip():
            return email.strip()
        return "?"

    def get_file(self, file_key: str) -> dict[str, object]:
        """`GET /v1/files/:key`: the file's document tree, styles and components."""
        return self._get(f"/v1/files/{file_key}")

    def get_file_nodes(self, file_key: str, node_ids: list[str]) -> dict[str, object]:
        """`GET /v1/files/:key/nodes?ids=…`: the named nodes only."""
        ids = ",".join(node_ids)
        return self._get(f"/v1/files/{file_key}/nodes?ids={ids}")

    def get_styles(self, file_key: str) -> dict[str, object]:
        """`GET /v1/files/:key/styles`: the file's published styles."""
        return self._get(f"/v1/files/{file_key}/styles")

    def get_components(self, file_key: str) -> dict[str, object]:
        """`GET /v1/files/:key/components`: the file's components."""
        return self._get(f"/v1/files/{file_key}/components")

    def get_local_variables(self, file_key: str) -> dict[str, object]:
        """`GET /v1/files/:key/variables/local` — Enterprise-only over REST.

        A 403 here is not a broken token: Figma gates REST variables to
        Enterprise plans. The Plugin API path (`use_figma`) has no such gate,
        so the error says exactly that instead of reporting a failure.
        """
        try:
            return self._get(f"/v1/files/{file_key}/variables/local")
        except FigmaError as exc:
            if "403" in str(exc):
                raise FigmaError(
                    "Figma refused the variables read (403) — REST variables are "
                    "Enterprise-only. Variables still work through the Figma MCP "
                    "server (`use_figma` runs the Plugin API, which has no such gate)."
                ) from exc
            raise


# ---- Figma file -> Baton tokens ---------------------------------------------------------------


@dataclass
class FigmaImport:
    """What one Figma file maps to: Baton tokens, markdown, and the evidence."""

    tokens: dict[str, object]
    markdown: str
    file_name: str
    component_names: list[str] = field(default_factory=list)
    style_names: list[str] = field(default_factory=list)
    fonts: list[str] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)


def _walk_nodes(node: dict[str, object]) -> Any:
    """Every node in a Figma document tree, depth-first (the node itself first)."""
    yield node
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                yield from _walk_nodes(child)


def _style_names(styles: dict[str, object]) -> dict[str, str]:
    """`{style key: style name}` for a file's `styles` map (any style type)."""
    out: dict[str, str] = {}
    for key, value in styles.items():
        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str) and name:
                out[key] = name
    return out


def _most_used(values: list[float], limit: int) -> list[float]:
    """The `limit` most common values, ascending — the scale a file actually uses."""
    counts: dict[float, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    ranked = sorted(counts, key=lambda v: (-counts[v], v))
    return sorted(ranked[:limit])


_SCALE_NAMES = ("xs", "sm", "md", "lg", "xl", "2xl")


def _starter_section(key: str) -> dict[str, object]:
    """One section of the starter tokens as a plain dict (or empty)."""
    section = STARTER_TOKENS.get(key)
    return dict(section) if isinstance(section, dict) else {}


def _scale_from_values(values: list[float], fallback: dict[str, object]) -> dict[str, object]:
    """Map observed layout numbers onto `xs…2xl` (ascending); the starter scale
    when the file declares none — a Baton system needs the section either way."""
    picked = _most_used(values, len(_SCALE_NAMES))
    if not picked:
        return dict(fallback)
    return {name: _number(value) for name, value in zip(_SCALE_NAMES, picked, strict=False)}


def _number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def file_to_tokens(file_json: dict[str, object]) -> FigmaImport:
    """Map a `GET /v1/files/:key` response onto Baton tokens + system markdown.

    Colors come from named paint styles first (the style name is the token
    name), then from the most-used unstyled solid fills. Type sizes come from
    named text styles, then from observed `fontSize` values. Spacing comes
    from auto-layout gaps/padding, radii from `cornerRadius` — both fall back
    to the starter scale when the file declares none, and the markdown says
    so rather than pretending the file declared them. Components and fonts
    are recorded as evidence, not tokens.
    """
    document = file_json.get("document")
    nodes = list(_walk_nodes(document)) if isinstance(document, dict) else []
    styles = file_json.get("styles")
    style_names = _style_names(styles) if isinstance(styles, dict) else {}
    file_name = str(file_json.get("name") or "Figma file")

    color_counts: dict[str, int] = {}
    color_values: dict[str, str] = {}
    unstyled: dict[str, int] = {}
    type_sizes: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    fonts: list[str] = []
    spacings: list[float] = []
    radii: list[float] = []
    for node in nodes:
        node_styles = node.get("styles")
        style_map = node_styles if isinstance(node_styles, dict) else {}
        fills = node.get("fills")
        if isinstance(fills, list):
            for fill in fills:
                if not isinstance(fill, dict) or fill.get("type") != "SOLID":
                    continue
                if fill.get("visible") is False:
                    continue
                hex_color = rgba_to_hex(fill.get("color"))
                if not isinstance(hex_color, str):
                    continue
                fill_style = style_map.get("fill")
                if isinstance(fill_style, str) and fill_style in style_names:
                    name = _token_name(style_names[fill_style])
                    color_counts[name] = color_counts.get(name, 0) + 1
                    if name not in color_values:
                        color_values[name] = hex_color
                else:
                    unstyled[hex_color] = unstyled.get(hex_color, 0) + 1
        node_style = node.get("style")
        if isinstance(node_style, dict):
            font_size = node_style.get("fontSize")
            if isinstance(font_size, (int, float)) and font_size > 0:
                text_style = style_map.get("text")
                name = (
                    _token_name(style_names[text_style])
                    if isinstance(text_style, str) and text_style in style_names
                    else f"text_{int(font_size)}"
                )
                type_sizes[name] = int(font_size)
                type_counts[name] = type_counts.get(name, 0) + 1
            font_family = node_style.get("fontFamily")
            if isinstance(font_family, str) and font_family and font_family not in fonts:
                fonts.append(font_family)
        for key in ("itemSpacing", "paddingLeft", "paddingRight", "paddingTop", "paddingBottom"):
            value = node.get(key)
            if isinstance(value, (int, float)) and value > 0:
                spacings.append(float(value))
        radius = node.get("cornerRadius")
        if isinstance(radius, (int, float)) and radius > 0:
            radii.append(float(radius))

    # Most-used named color wins when one name carries several values; unstyled
    # fills join by frequency, capped so one file cannot flood the palette.
    colors: dict[str, object] = {}
    for name in sorted(color_counts, key=lambda n: -color_counts[n]):
        colors[name] = color_values[name]
    for hex_color in sorted(unstyled, key=lambda h: -unstyled[h]):
        if len(colors) >= 16:
            break
        name = f"color_{len(colors) + 1}"
        colors[name] = hex_color
    if not colors:
        colors = _starter_section("colors")
    type_scale: dict[str, object] = {
        name: type_sizes[name] for name in sorted(type_counts, key=lambda n: -type_counts[n])
    }
    if not type_scale:
        type_scale = _starter_section("type_scale")

    spacing = _scale_from_values(spacings, _starter_section("spacing"))
    radii_tokens = _scale_from_values(radii, _starter_section("radii"))
    fallbacks = []
    if not spacings:
        fallbacks.append("spacing: the file declares no auto-layout gaps or padding")
    if not radii:
        fallbacks.append("radii: the file declares no corner radii")
    if not color_counts and unstyled:
        fallbacks.append("colors: no named paint styles — names are color_1, color_2, …")
    if not type_counts:
        fallbacks.append("type_scale: no named text styles — sizes are text_<px>")

    components = file_json.get("components")
    component_names: list[str] = []
    if isinstance(components, dict):
        for value in components.values():
            if isinstance(value, dict):
                component_name = value.get("name")
                if isinstance(component_name, str) and component_name:
                    component_names.append(component_name)
    component_names.sort()

    tokens: dict[str, object] = {
        "colors": colors,
        "type_scale": type_scale,
        "spacing": spacing,
        "radii": radii_tokens,
    }
    markdown = _import_markdown(file_name, tokens, component_names, fonts, fallbacks)
    return FigmaImport(
        tokens=tokens,
        markdown=markdown,
        file_name=file_name,
        component_names=component_names,
        style_names=sorted(style_names.values()),
        fonts=fonts,
        fallbacks=fallbacks,
    )


def _import_markdown(
    file_name: str,
    tokens: dict[str, object],
    component_names: list[str],
    fonts: list[str],
    fallbacks: list[str],
) -> str:
    """The system markdown for a Figma import: what came from the file, and
    what fell back to defaults — never presented as if the file declared it."""
    lines = [
        f"# Design System — imported from Figma: {file_name}",
        "",
        "Reversed from a Figma file over the Figma REST API. Colors come from",
        "the file's paint styles (then its most-used solid fills); type sizes",
        "from its text styles; spacing from auto-layout gaps and padding;",
        "radii from corner radii.",
        "",
        "## Tokens",
        "",
        "The structured values live in `tokens.json` (colors, type_scale,",
        "spacing, radii).",
        "",
    ]
    colors = tokens.get("colors")
    if isinstance(colors, dict) and colors:
        highlights = ", ".join(f"{k} `{v}`" for k, v in list(colors.items())[:6])
        lines.append(f"Highlights: {highlights}.")
        lines.append("")
    if component_names:
        lines.append("## Components")
        lines.append("")
        lines.append("Components found in the Figma file (imported as evidence —")
        lines.append("recreate them as Baton components from the tokens above):")
        lines.append("")
        lines.extend(f"- **{name}**" for name in component_names[:20])
        lines.append("")
    if fonts:
        lines.append("## Typography")
        lines.append("")
        lines.append("Font families observed in the file: " + ", ".join(fonts[:8]) + ".")
        lines.append("")
    if fallbacks:
        lines.append("## Notes")
        lines.append("")
        lines.append("Defaults used where the file declares nothing:")
        lines.append("")
        lines.extend(f"- {note}" for note in fallbacks)
        lines.append("")
    lines.append("## Sync")
    lines.append("")
    lines.append("This system is linked to its Figma file: use Sync from Figma on the")
    lines.append("system page to pull later file changes back in, and Export to Figma")
    lines.append("to push Baton changes out through the Figma MCP server.")
    lines.append("")
    return "\n".join(lines)


# ---- two-way sync -----------------------------------------------------------------------------


@dataclass(frozen=True)
class FigmaSyncDiff:
    """What changed between the Baton tokens and the Figma file's tokens.

    `removed` is reported but never applied: Baton keeps local extras, and
    deleting a token is a deliberate edit, not a side effect of syncing.
    """

    added: dict[str, dict[str, object]] = field(default_factory=dict)
    changed: dict[str, dict[str, tuple[object, object]]] = field(default_factory=dict)
    removed: dict[str, dict[str, object]] = field(default_factory=dict)

    def empty(self) -> bool:
        """True when the two sides already agree (nothing to apply)."""
        return not self.added and not self.changed and not self.removed

    def summary(self) -> str:
        """One line for the flash + the sync record: "2 added, 1 changed"."""
        bits = []
        total_added = sum(len(section) for section in self.added.values())
        total_changed = sum(len(section) for section in self.changed.values())
        total_removed = sum(len(section) for section in self.removed.values())
        if total_added:
            bits.append(f"{total_added} added")
        if total_changed:
            bits.append(f"{total_changed} changed")
        if total_removed:
            bits.append(f"{total_removed} removed in Figma (kept in Baton)")
        return ", ".join(bits) if bits else "already in sync"


def reconcile_tokens(current: dict[str, object], incoming: dict[str, object]) -> FigmaSyncDiff:
    """Diff Baton tokens (`current`) against Figma tokens (`incoming`), per
    section and token name. Only the four token sections are compared; anything
    else in either side is out of scope for sync and ignored."""
    added: dict[str, dict[str, object]] = {}
    changed: dict[str, dict[str, tuple[object, object]]] = {}
    removed: dict[str, dict[str, object]] = {}
    for section in TOKEN_SECTIONS:
        old = current.get(section)
        new = incoming.get(section)
        old_map = old if isinstance(old, dict) else {}
        new_map = new if isinstance(new, dict) else {}
        for name, value in new_map.items():
            if name not in old_map:
                added.setdefault(section, {})[name] = value
            elif old_map[name] != value:
                changed.setdefault(section, {})[name] = (old_map[name], value)
        for name, value in old_map.items():
            if name not in new_map:
                removed.setdefault(section, {})[name] = value
    return FigmaSyncDiff(added=added, changed=changed, removed=removed)


def apply_sync(current: dict[str, object], diff: FigmaSyncDiff) -> dict[str, object]:
    """The merged tokens after a sync: added + changed applied, removed kept.

    The input is never mutated — the caller saves the returned dict as a new
    library version, so a bad sync can be rolled back to the previous version.
    """
    merged = copy.deepcopy(current)
    for section, added_names in diff.added.items():
        added_target = merged.setdefault(section, {})
        if isinstance(added_target, dict):
            added_target.update(added_names)
    for section, changed_names in diff.changed.items():
        changed_target = merged.setdefault(section, {})
        if isinstance(changed_target, dict):
            for name, (_old, new) in changed_names.items():
                changed_target[name] = new
    return merged


# ---- `use_figma` code generation ---------------------------------------------------------------

#: How many of each token kind the generated script carries (a file with more
#: still exports — the script is the whole system, this cap is only the
#: component/frame part, which stays small on purpose).
_MAX_SCRIPT_COLORS = 24


def _js(value: object) -> str:
    """A value as a JS literal, safely quoted (`json.dumps` — never f-string
    interpolation of user text into code)."""
    return json.dumps(value)


def _figma_color(value: object) -> dict[str, float] | None:
    """A `#rrggbb` token as a Plugin API `{r, g, b}` color, or None."""
    if not _is_hex6(value):
        return None
    return rgb_to_figma(str(value))


def render_use_figma_script(
    system_name: str, tokens: dict[str, object], *, file_key: str | None = None
) -> str:
    """The Plugin API script that builds this system in Figma — the export.

    Run it with the Figma MCP server's `use_figma` tool (an agent does this
    when the server is connected), or paste it into a Figma plugin console.
    It works incrementally on purpose: variables and styles first (so a
    failure later still leaves the tokens), then a Button + Card component,
    then a two-frame Home → Detail prototype whose button navigates with
    SMART_ANIMATE. Light and dark are variable *modes* on one collection,
    not two collections. The REST API cannot run this — it is Plugin API
    JavaScript, and the header says so.
    """
    light, dark = _flat_colors(tokens)
    colors = {k: v for k, v in light.items() if _is_hex6(v)}
    dark_colors = {k: v for k, v in (dark or {}).items() if _is_hex6(v)}
    type_scale = tokens.get("type_scale")
    type_map = type_scale if isinstance(type_scale, dict) else {}
    spacing = tokens.get("spacing")
    spacing_map = spacing if isinstance(spacing, dict) else {}
    radii = tokens.get("radii")
    radii_map = radii if isinstance(radii, dict) else {}

    primary = colors.get("primary") or next(iter(colors.values()), "#2563EB")
    surface = colors.get("surface") or colors.get("background") or "#F8FAFC"
    background = colors.get("background") or "#FFFFFF"
    radius_md = radii_map.get("md") or radii_map.get("sm") or 10
    # The header comment is raw text, not a JS literal: collapse whitespace so
    # a newline in the system name cannot end the comment and inject code.
    safe_name = " ".join(system_name.split())
    notify_text = "Baton: " + system_name + " built in Figma — check the Home and Detail frames"

    lines: list[str] = [
        "// Generated by Baton Design Studio — run with the Figma MCP server's",
        "// `use_figma` tool (Plugin API JavaScript). The REST API cannot run",
        "// this: only the Plugin API creates variables, styles and components.",
        f"// System: {safe_name}",
    ]
    if file_key:
        lines.append(f"// Figma file: https://www.figma.com/design/{file_key}/")
    lines += [
        "// Verify after running: use `get_screenshot` on the new frames.",
        "",
        "async function main() {",
        (
            "  const collection = figma.variables.createVariableCollection("
            f"{_js('Baton / ' + system_name)});"
        ),
        "  const lightMode = collection.modes[0];",
        "  collection.renameMode(lightMode.modeId, 'Light');",
        "  const darkMode = collection.addMode('Dark');",
        "",
        "  // Variables: colors (light + dark modes), then numbers.",
    ]
    for name in sorted(colors)[:_MAX_SCRIPT_COLORS]:
        value = colors[name]
        figma_value = _figma_color(value)
        if figma_value is None:
            continue
        dark_value = _figma_color(dark_colors.get(name)) if name in dark_colors else None
        var = f"v_{_token_name(name)}"
        lines.append(
            f"  const {var} = figma.variables.createVariable("
            f"{_js('color/' + name)}, collection, 'COLOR');"
        )
        lines.append(f"  {var}.setValueForMode(lightMode.modeId, {_js(figma_value)});")
        if dark_value is not None:
            lines.append(f"  {var}.setValueForMode(darkMode.modeId, {_js(dark_value)});")
    for group, values in (
        ("font-size", type_map),
        ("spacing", spacing_map),
        ("radius", radii_map),
    ):
        for name in sorted(values):
            value = values[name]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            var = f"n_{group.replace('-', '_')}_{_token_name(name)}"
            lines.append(
                f"  const {var} = figma.variables.createVariable("
                f"{_js(group + '/' + name)}, collection, 'FLOAT');"
            )
            lines.append(f"  {var}.setValueForMode(lightMode.modeId, {_js(value)});")
            lines.append(f"  {var}.setValueForMode(darkMode.modeId, {_js(value)});")
    lines += [
        "",
        "  // Styles: one paint style per color, one text style per type size.",
    ]
    for name in sorted(colors)[:_MAX_SCRIPT_COLORS]:
        figma_value = _figma_color(colors[name])
        if figma_value is None:
            continue
        lines.append(f"  const s_{_token_name(name)} = figma.createPaintStyle();")
        lines.append(f"  s_{_token_name(name)}.name = {_js('Baton/' + name)};")
        lines.append(
            f"  s_{_token_name(name)}.paints = [{{ type: 'SOLID', color: {_js(figma_value)} }}];"
        )
    for name in sorted(type_map):
        value = type_map[name]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        lines.append(f"  const t_{_token_name(name)} = figma.createTextStyle();")
        lines.append(f"  t_{_token_name(name)}.name = {_js('Baton/' + name)};")
        lines.append(f"  t_{_token_name(name)}.fontSize = {_js(value)};")
    lines += [
        "",
        "  // Components + prototype: Button and Card, then Home -> Detail.",
        "  // Text needs a loaded font; a missing font skips the label, never",
        "  // the tokens above.",
        "  try { await figma.loadFontAsync({ family: 'Inter', style: 'Regular' }); } catch (e) {}",
        "  try { await figma.loadFontAsync({ family: 'Inter', style: 'Bold' }); } catch (e) {}",
        "  const button = figma.createComponent();",
        f"  button.name = {_js(system_name + ' / Button')};",
        "  button.resize(160, 44);",
        f"  button.cornerRadius = {_js(radius_md)};",
        "  const buttonBg = figma.createRectangle();",
        "  buttonBg.resize(160, 44);",
        f"  buttonBg.fills = [{{ type: 'SOLID', color: {_js(_figma_color(primary))} }}];",
        f"  buttonBg.cornerRadius = {_js(radius_md)};",
        "  button.appendChild(buttonBg);",
        "  const card = figma.createComponent();",
        f"  card.name = {_js(system_name + ' / Card')};",
        "  card.resize(320, 180);",
        f"  card.cornerRadius = {_js(radius_md)};",
        "  const cardBg = figma.createRectangle();",
        "  cardBg.resize(320, 180);",
        f"  cardBg.fills = [{{ type: 'SOLID', color: {_js(_figma_color(surface))} }}];",
        f"  cardBg.cornerRadius = {_js(radius_md)};",
        "  card.appendChild(cardBg);",
        "",
        "  const home = figma.createFrame();",
        f"  home.name = {_js(system_name + ' — Home')};",
        "  home.resize(1440, 900);",
        f"  home.fills = [{{ type: 'SOLID', color: {_js(_figma_color(background))} }}];",
        "  const homeButton = button.createInstance();",
        "  homeButton.x = 64; homeButton.y = 64;",
        "  home.appendChild(homeButton);",
        "  const detail = figma.createFrame();",
        f"  detail.name = {_js(system_name + ' — Detail')};",
        "  detail.resize(1440, 900);",
        f"  detail.fills = [{{ type: 'SOLID', color: {_js(_figma_color(background))} }}];",
        "  const detailCard = card.createInstance();",
        "  detailCard.x = 64; detailCard.y = 64;",
        "  detail.appendChild(detailCard);",
        "  const backButton = button.createInstance();",
        "  backButton.x = 64; backButton.y = 280;",
        "  detail.appendChild(backButton);",
        "",
        "  // Prototype: Home button -> Detail, back button -> Home, both",
        "  // SMART_ANIMATE. Reactions are set on the node, one small step.",
        (
            "  await homeButton.setReactionsAsync([{ trigger: { type: 'ON_CLICK' }, "
            "actions: [{ type: 'NODE', destinationId: detail.id, navigation: 'NAVIGATE', "
            "transition: { type: 'SMART_ANIMATE', duration: 0.3, "
            "easing: { type: 'EASE_IN_OUT' } } }] }]);"
        ),
        (
            "  await backButton.setReactionsAsync([{ trigger: { type: 'ON_CLICK' }, "
            "actions: [{ type: 'NODE', destinationId: home.id, navigation: 'NAVIGATE', "
            "transition: { type: 'SMART_ANIMATE', duration: 0.3, "
            "easing: { type: 'EASE_IN_OUT' } } }] }]);"
        ),
        f"  figma.notify({_js(notify_text)});",
        "}",
        "",
        "await main();",
        "",
    ]
    return "\n".join(lines)


# ---- granular edits ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FigmaEdit:
    """One small, inspect-first change, for `render_use_figma_edit`.

    `kind` decides which fields matter: `variable` needs `collection`,
    `name`, `mode` and `value`; `node-fill`/`node-text`/`node-size`/
    `corner-radius` need `node_id` plus their value; `reaction` needs
    `node_id`, `destination_id`, `trigger` and `transition`. Anything else
    is rejected before any code is generated — a bad edit never reaches
    Figma as a half-formed script.
    """

    kind: str
    collection: str = ""
    name: str = ""
    mode: str = "Light"
    value: str = ""
    node_id: str = ""
    destination_id: str = ""
    trigger: str = "ON_CLICK"
    transition: str = "SMART_ANIMATE"
    width: float = 0
    height: float = 0


_EDIT_KINDS = ("variable", "node-fill", "node-text", "node-size", "corner-radius", "reaction")
_TRIGGERS = ("ON_CLICK", "ON_HOVER", "ON_PRESS", "ON_DRAG", "AFTER_TIMEOUT", "ON_KEY_DOWN")
_TRANSITIONS = (
    "SMART_ANIMATE",
    "DISSOLVE",
    "MOVE_IN",
    "MOVE_OUT",
    "PUSH",
    "SLIDE_IN",
    "SLIDE_OUT",
    "INSTANT",
)


def validate_figma_edit(edit: FigmaEdit) -> FigmaEdit:
    """Check one granular edit; raises `FigmaError` naming the bad field."""
    if edit.kind not in _EDIT_KINDS:
        raise FigmaError(
            f"unknown Figma edit kind {edit.kind!r} (choose one of {', '.join(_EDIT_KINDS)})"
        )
    if edit.kind == "variable":
        if not edit.collection.strip():
            raise FigmaError("a variable edit needs the variable collection name")
        if not edit.name.strip():
            raise FigmaError("a variable edit needs the variable name (e.g. color/primary)")
        if edit.mode not in ("Light", "Dark"):
            raise FigmaError(f"unknown variable mode {edit.mode!r} (Light or Dark)")
        if not _is_hex6(edit.value) and not _is_number_text(edit.value):
            raise FigmaError(f"bad variable value {edit.value!r} (a #rrggbb color or a number)")
    elif edit.kind in ("node-fill", "node-text", "corner-radius"):
        if not _NODE_ID.match(edit.node_id or ""):
            raise FigmaError(f"bad Figma node id {edit.node_id!r} (expected like 12:34)")
        if edit.kind == "node-fill" and not _is_hex6(edit.value):
            raise FigmaError(f"bad fill color {edit.value!r} (expected #rrggbb)")
        if edit.kind == "node-text" and not edit.value.strip():
            raise FigmaError("a text edit needs the new text")
        if edit.kind == "corner-radius" and not _is_number_text(edit.value):
            raise FigmaError(f"bad corner radius {edit.value!r} (expected a number)")
    elif edit.kind == "node-size":
        if not _NODE_ID.match(edit.node_id or ""):
            raise FigmaError(f"bad Figma node id {edit.node_id!r} (expected like 12:34)")
        if edit.width <= 0 or edit.height <= 0:
            raise FigmaError("a size edit needs a width and height above 0")
    elif edit.kind == "reaction":
        if not _NODE_ID.match(edit.node_id or ""):
            raise FigmaError(f"bad Figma node id {edit.node_id!r} (expected like 12:34)")
        if not _NODE_ID.match(edit.destination_id or ""):
            raise FigmaError(
                f"bad Figma destination node id {edit.destination_id!r} (expected like 12:34)"
            )
        if edit.trigger not in _TRIGGERS:
            raise FigmaError(f"unknown trigger {edit.trigger!r} (e.g. ON_CLICK, ON_HOVER)")
        if edit.transition not in _TRANSITIONS:
            raise FigmaError(
                f"unknown transition {edit.transition!r} (e.g. SMART_ANIMATE, DISSOLVE)"
            )
    return edit


def _is_number_text(value: str) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0


def render_use_figma_edit(edit: FigmaEdit) -> str:
    """The Plugin API snippet for one granular edit — run via `use_figma`.

    Every snippet looks the node/variable up first and throws when it is not
    there, so a stale id fails loudly instead of editing the wrong thing.
    After running, verify with `get_screenshot` — Figma's own guidance for
    agent writes is inspect, change in small steps, verify.
    """
    edit = validate_figma_edit(edit)
    header = [
        "// Generated by Baton Design Studio — one small Figma change.",
        "// Run with the Figma MCP server's `use_figma` tool, then verify",
        "// with `get_screenshot`. Inspect first, change one thing, verify.",
        "",
        "async function main() {",
    ]
    if edit.kind == "variable":
        value_js = _js(_figma_color(edit.value)) if _is_hex6(edit.value) else _js(float(edit.value))
        body = [
            (
                "  const collection = figma.variables.getLocalVariableCollections()"
                f".find((c) => c.name === {_js(edit.collection)});"
            ),
            "  if (!collection) throw new Error('no variable collection named ' + "
            + _js(edit.collection)
            + ");",
            f"  const mode = collection.modes.find((m) => m.name === {_js(edit.mode)});",
            "  if (!mode) throw new Error('no mode named ' + " + _js(edit.mode) + ");",
            "  const variable = figma.variables.getLocalVariables().find((v) => v.name === "
            + _js(edit.name)
            + " && v.variableCollectionId === collection.id);",
            "  if (!variable) throw new Error('no variable named ' + " + _js(edit.name) + ");",
            f"  variable.setValueForMode(mode.modeId, {value_js});",
            f"  figma.notify({_js('Baton: set ' + edit.name + ' (' + edit.mode + ')')});",
        ]
    elif edit.kind == "node-fill":
        body = [
            f"  const node = await figma.getNodeByIdAsync({_js(edit.node_id)});",
            "  if (!node) throw new Error('no node with id ' + " + _js(edit.node_id) + ");",
            "  if (!('fills' in node)) throw new Error('node ' + "
            + _js(edit.node_id)
            + " + ' has no fills');",
            f"  node.fills = [{{ type: 'SOLID', color: {_js(_figma_color(edit.value))} }}];",
            f"  figma.notify({_js('Baton: set fill on ' + edit.node_id)});",
        ]
    elif edit.kind == "node-text":
        body = [
            f"  const node = await figma.getNodeByIdAsync({_js(edit.node_id)});",
            "  if (!node) throw new Error('no node with id ' + " + _js(edit.node_id) + ");",
            "  if (node.type !== 'TEXT') throw new Error('node ' + "
            + _js(edit.node_id)
            + " + ' is not text');",
            "  await figma.loadFontAsync(node.fontName);",
            f"  node.characters = {_js(edit.value)};",
            f"  figma.notify({_js('Baton: set text on ' + edit.node_id)});",
        ]
    elif edit.kind == "node-size":
        body = [
            f"  const node = await figma.getNodeByIdAsync({_js(edit.node_id)});",
            "  if (!node) throw new Error('no node with id ' + " + _js(edit.node_id) + ");",
            "  if (typeof node.resize !== 'function') throw new Error('node ' + "
            + _js(edit.node_id)
            + " + ' cannot be resized');",
            f"  node.resize({_js(edit.width)}, {_js(edit.height)});",
            f"  figma.notify({_js('Baton: resized ' + edit.node_id)});",
        ]
    elif edit.kind == "corner-radius":
        body = [
            f"  const node = await figma.getNodeByIdAsync({_js(edit.node_id)});",
            "  if (!node) throw new Error('no node with id ' + " + _js(edit.node_id) + ");",
            "  if (!('cornerRadius' in node)) throw new Error('node ' + "
            + _js(edit.node_id)
            + " + ' has no corner radius');",
            f"  node.cornerRadius = {_js(float(edit.value))};",
            f"  figma.notify({_js('Baton: set corner radius on ' + edit.node_id)});",
        ]
    else:  # reaction
        reaction_js = (
            "  await node.setReactionsAsync([{ trigger: { type: "
            + _js(edit.trigger)
            + " }, actions: [{ type: 'NODE', destinationId: destination.id, "
            "navigation: 'NAVIGATE', transition: { type: "
            + _js(edit.transition)
            + ", duration: 0.3, easing: { type: 'EASE_IN_OUT' } } }] }]);"
        )
        linked_text = "Baton: linked " + edit.node_id + " → " + edit.destination_id
        body = [
            f"  const node = await figma.getNodeByIdAsync({_js(edit.node_id)});",
            "  if (!node) throw new Error('no node with id ' + " + _js(edit.node_id) + ");",
            "  const destination = await figma.getNodeByIdAsync(" + _js(edit.destination_id) + ");",
            "  if (!destination) throw new Error('no node with id ' + "
            + _js(edit.destination_id)
            + ");",
            reaction_js,
            f"  figma.notify({_js(linked_text)});",
        ]
    return "\n".join([*header, *body, "}", "", "await main();", ""])


def edit_from_form(kind: str, form: dict[str, str]) -> FigmaEdit:
    """A dashboard form's fields as a `FigmaEdit` (validated on render).

    The form carries `collection`, `name`, `mode`, `value`, `node_id`,
    `destination_id`, `trigger`, `transition`, `width`, `height` — only the
    kind's fields are read, the rest are ignored.
    """
    try:
        width = float(form.get("width") or 0)
        height = float(form.get("height") or 0)
    except ValueError as exc:
        raise FigmaError("width and height must be numbers") from exc
    return FigmaEdit(
        kind=kind,
        collection=form.get("collection", "").strip(),
        name=form.get("name", "").strip(),
        mode=form.get("mode", "Light").strip() or "Light",
        value=form.get("value", "").strip(),
        node_id=form.get("node_id", "").strip(),
        destination_id=form.get("destination_id", "").strip(),
        trigger=form.get("trigger", "ON_CLICK").strip() or "ON_CLICK",
        transition=form.get("transition", "SMART_ANIMATE").strip() or "SMART_ANIMATE",
        width=width,
        height=height,
    )
