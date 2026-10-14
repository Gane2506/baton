"""Edit jev.toml (repo) and the user config from the web UI (issue #9).

Schema-driven: the fields come from the pydantic models in `config.schema`, so a new config
key appears in the editor without code here. Writes go through tomlkit, so comments and
ordering survive; every change is validated against `JevConfig` before it is written, and a
repo write that touches a `GUARDED_KEYS` path needs the repo trusted (docs/security.md).

Nothing here prints or exits; errors are strings for the caller to show.
"""

from __future__ import annotations

import re
import tomllib
import types
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

import tomlkit
from pydantic import BaseModel, ValidationError

from jev_agent.config.defaults import CONFIG_FILENAME
from jev_agent.config.loader import (
    changed_guarded_keys,
    deep_merge,
    default_user_config_path,
    trusts,
)
from jev_agent.config.schema import JevConfig

# The sections the editor shows, in order. The path names a table; the model behind it comes
# from a default `JevConfig` instance, so nothing here names a config class.
TOP_SECTIONS: tuple[tuple[str, str], ...] = (
    ("dispatcher", "Dispatcher"),
    ("dispatcher.jev", "Dispatcher: Jev"),
    ("dispatcher.laya", "Dispatcher: Laya (the decision matrix)"),
    ("budget", "Budget and limits"),
    ("workers.claude_code", "Worker: Claude Code"),
    ("workers.codex", "Worker: Codex"),
    ("workers.api", "Worker: API (OpenRouter)"),
    ("roles.planner", "Role: planner"),
    ("roles.validator", "Role: validator"),
    ("roles.coder", "Role: coder"),
    ("roles.reviewer", "Role: reviewer"),
    ("roles.code_reviewer", "Role: code_reviewer"),
    ("roles.learner", "Role: learner"),
    ("quality", "Quality gate"),
    ("learning", "Learning"),
    ("ship", "Ship (GitHub)"),
    ("models", "Models"),
    ("integration", "Integration"),
    ("workspace", "Workspace"),
    ("approval", "Approval"),
    ("review", "Review"),
    ("browser", "Browser"),
    ("trust", "Trust (user config only)"),
)

USER_ONLY = frozenset({"trust"})
_SECTION_PATHS = frozenset(path for path, _ in TOP_SECTIONS)

# Entity tables: name -> table of settings. `tools.mcp`/`tools.skills`/`tools.plugins` are the
# agent tools (`mcp = ["github"]` under a role picks one); servers and profiles are per repo.
ENTITY_SECTIONS: tuple[tuple[str, str], ...] = (
    ("tools.mcp", "Tools: MCP servers"),
    ("tools.skills", "Tools: skills"),
    ("tools.plugins", "Tools: plugins (Claude Code only)"),
    ("servers", "Servers (SSH)"),
    ("profiles", "Workspace profiles"),
)

# The dashboard pages the editor is split across, grouped by what you are doing rather than by
# which table a key lives in: agents do the work, tools are what they get, connections are what
# they reach, settings is how the project behaves (plus the read-only health panels).
AGENT_SECTIONS: tuple[str, ...] = (
    "dispatcher",
    "dispatcher.jev",
    "workers.claude_code",
    "workers.codex",
    "workers.api",
    "roles.planner",
    "roles.validator",
    "roles.coder",
    "roles.reviewer",
    "roles.code_reviewer",
    "roles.learner",
)
CONNECTION_SECTIONS: tuple[str, ...] = ("browser", "trust")
TOOL_ENTITIES: tuple[str, ...] = ("tools.mcp", "tools.skills", "tools.plugins")
CONNECTION_ENTITIES: tuple[str, ...] = ("servers", "profiles")

PAGES: dict[str, tuple[str, str, tuple[str, ...], tuple[str, ...]]] = {
    "agents": (
        "Agents",
        "Who does the work: Claude Code, Codex and the API worker, the roles they serve, and "
        "Jev, who dispatches.",
        AGENT_SECTIONS,
        (),
    ),
    "tools": (
        "Tools",
        "What agents get: MCP servers, skills and plugins. Roles pick them by name on the "
        "Agents page (mcp = [...], skills = [...], plugins = [...]).",
        (),
        TOOL_ENTITIES,
    ),
    "connections": (
        "Connections",
        "What jev reaches: SSH servers, workspace profiles, the browser, and which repos you "
        "trust to change them. API key names are set on the worker rows (Agents); whether a key "
        "is set is listed under Settings.",
        CONNECTION_SECTIONS,
        CONNECTION_ENTITIES,
    ),
    "settings": (
        "Settings",
        "How the project behaves: budget, quality, learning, ship and the rest, plus the "
        "effective config, environment and doctor readouts.",
        (),
        (),
    ),
}


def page_section_paths(page: str) -> tuple[frozenset[str], frozenset[str]]:
    """(section paths, entity paths) shown on `page`; Settings takes whatever is unassigned."""
    claimed_sections: set[str] = set()
    claimed_entities: set[str] = set()
    for name, (_, _, secs, ents) in PAGES.items():
        if name != "settings":
            claimed_sections.update(secs)
            claimed_entities.update(ents)
    _, _, secs, ents = PAGES[page]
    if page == "settings":
        secs = tuple(path for path, _ in TOP_SECTIONS if path not in claimed_sections)
        ents = tuple(path for path, _ in ENTITY_SECTIONS if path not in claimed_entities)
    return frozenset(secs), frozenset(ents)


def page_view(
    config: JevConfig, repo_raw: dict[str, Any], user_raw: dict[str, Any], page: str
) -> tuple[str, str, list[Section], list[EntitySection]]:
    """(title, lede, the sections, the entity tables) for one of the pages."""
    title, lede, _, _ = PAGES[page]
    section_paths, entity_paths = page_section_paths(page)
    shown_sections = [s for s in sections(config, repo_raw, user_raw) if s.path in section_paths]
    shown_entities = [
        e for e in entity_sections(config, repo_raw, user_raw) if e.path in entity_paths
    ]
    return title, lede, shown_sections, shown_entities


def page_of(dotted: str) -> str:
    """The page a dotted config path is edited on (for redirects after a save)."""
    for prefix in AGENT_SECTIONS:
        if dotted == prefix or dotted.startswith(prefix + "."):
            return "agents"
    for prefix in TOOL_ENTITIES:
        if dotted == prefix or dotted.startswith(prefix + "."):
            return "tools"
    for prefix in CONNECTION_SECTIONS + CONNECTION_ENTITIES:
        if dotted == prefix or dotted.startswith(prefix + "."):
            return "connections"
    return "settings"


def anchor_of(dotted: str) -> str:
    """The id of the section that holds a dotted path (the editor's per-section anchors)."""
    for path, _ in TOP_SECTIONS + ENTITY_SECTIONS:
        if dotted == path or dotted.startswith(path + "."):
            return "s-" + path.replace(".", "-")
    return ""


# A form value that means "remove the key from this layer" (falls back to the default).
UNSET = object()

_ENTITY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def check_entity_name(name: str) -> str | None:
    """A name for a new table entry, or the reason it cannot be one."""
    if not _ENTITY_NAME.match(name):
        return "a name must start with a letter or digit and use letters, digits, _ or -"
    return None


@dataclass(frozen=True)
class Field:
    path: str  # "workers.claude_code.timeout_s"
    key: str  # "timeout_s"
    kind: str  # bool | tristate | int | float | str | env | enum | list | weights | skip
    choices: tuple[str, ...]
    text: str  # the current value, formatted for the input
    source: str | None  # "repo" | "user" | None (default)
    help: str
    parts: tuple[tuple[str, str], ...] = ()  # weights: one (criterion, value) per knob


@dataclass(frozen=True)
class Section:
    path: str
    title: str
    fields: list[Field]
    note: str = ""


@dataclass(frozen=True)
class EntityField:
    key: str
    kind: str
    choices: tuple[str, ...]
    text: str
    help: str


@dataclass(frozen=True)
class Entity:
    name: str
    source: str
    fields: list[EntityField]


@dataclass(frozen=True)
class EntitySection:
    path: str
    title: str
    entities: list[Entity]
    add_fields: list[EntityField]
    note: str = ""


# ---- layers and paths ---------------------------------------------------------------------


def read_layers(root: Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """(repo jev.toml, user config, the user config's path); a missing file reads as {}."""
    user_path = default_user_config_path()
    return _load(root / CONFIG_FILENAME), _load(user_path), user_path


def _load(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def target_file(root: Path, target: str) -> Path:
    """The file a change writes to; `ValueError` for anything but repo/user."""
    if target == "repo":
        return root / CONFIG_FILENAME
    if target == "user":
        return default_user_config_path()
    raise ValueError("target must be 'repo' or 'user'")


def set_path(raw: dict[str, Any], path: str, value: Any) -> None:
    """Set `path` in a plain dict, creating tables on the way."""
    parts = path.split(".")
    node = raw
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def remove_path(raw: dict[str, Any], path: str) -> None:
    """Remove `path` from a plain dict; missing parts are ignored."""
    parts = path.split(".")
    node: Any = raw
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def _at(raw: dict[str, Any], path: str) -> tuple[bool, Any]:
    node: Any = raw
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _attr_at(config: BaseModel, path: str) -> Any:
    node: Any = config
    for part in path.split("."):
        node = getattr(node, part)
    return node


# ---- tomlkit writes ------------------------------------------------------------------------


def _parse_file(path: Path) -> Any:
    if path.exists():
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    return tomlkit.document()


def _table(doc: Any, parts: list[str]) -> Any:
    node = doc
    for part in parts:
        child = node.get(part)
        if not hasattr(child, "get"):  # not a table: replace whatever is there
            child = tomlkit.table()
            node[part] = child
        node = child
    return node


def _write(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def write_value(path: Path, dotted: str, value: Any) -> None:
    """Set one key in the TOML file, keeping comments and other ordering."""
    doc = _parse_file(path)
    parts = dotted.split(".")
    table = _table(doc, parts[:-1])
    table[parts[-1]] = value
    _write(path, doc)


def unset_value(path: Path, dotted: str) -> None:
    """Remove one key from the TOML file (missing is fine)."""
    doc = _parse_file(path)
    parts = dotted.split(".")
    table = _table(doc, parts[:-1])
    if parts[-1] in table:
        del table[parts[-1]]
    _write(path, doc)


def add_entity(path: Path, section: str, name: str, values: dict[str, Any]) -> None:
    """Add `[section.name]` with `values` (a section of plain strings is stored as the value)."""
    doc = _parse_file(path)
    table = _table(doc, section.split("."))
    if name in table:
        raise ValueError(f"{section}.{name} already exists")
    node = tomlkit.table()
    for key, value in values.items():
        node[key] = value
    table[name] = node
    _write(path, doc)


def remove_entity(path: Path, section: str, name: str) -> None:
    doc = _parse_file(path)
    table = _table(doc, section.split("."))
    if name not in table:
        raise ValueError(f"{section}.{name} does not exist")
    del table[name]
    _write(path, doc)


# ---- validation ----------------------------------------------------------------------------


def save(
    root: Path,
    target: str,
    mutate: Callable[[dict[str, Any]], None],
    write: Callable[[], None],
) -> str | None:
    """Check a change, then write it. Returns an error message, or None on success.

    `mutate` edits a copy of the chosen layer; the merged result must validate, and a repo
    change to a guarded key needs the repo trusted (then the message says how).
    """
    try:
        target_file(root, target)  # ValueError for anything but repo/user
    except ValueError as exc:
        return str(exc)
    repo_raw, user_raw, user_path = read_layers(root)
    before = _validate_or_none(deep_merge(user_raw, repo_raw))
    layer = dict(repo_raw) if target == "repo" else dict(user_raw)
    mutate(layer)
    repo_new, user_new = (layer, user_raw) if target == "repo" else (repo_raw, layer)
    merged = deep_merge(user_new, repo_new)
    try:
        after = JevConfig.model_validate(merged)
    except ValidationError as exc:
        return _error_line(exc)
    if target == "repo" and before is not None:
        changed = changed_guarded_keys(before, after)
        if changed and not trusts(JevConfig.model_validate(user_new), root):
            listed = ", ".join(sorted(changed))
            return (
                f"{listed}: these keys change a repository's jev.toml only when you trust it "
                f"(they decide where API keys go, which commands run and which tools agents "
                f"get). Add the repo under Trust below, or save this to the user config "
                f"({user_path}) instead."
            )
    write()
    return None


def _validate_or_none(merged: dict[str, Any]) -> JevConfig | None:
    try:
        return JevConfig.model_validate(merged)
    except ValidationError:
        return None


def _error_line(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ()))
    return f"{where}: {first.get('msg', 'is not valid')}" if where else str(first.get("msg"))


# ---- kinds and fields ----------------------------------------------------------------------


def _base(annotation: Any) -> tuple[Any, bool]:
    """The annotation without `| None`, and whether it may be None."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = get_args(annotation)
        rest = [a for a in args if a is not type(None)]
        if len(rest) == 1:
            return rest[0], type(None) in args
    return annotation, False


def _kind_of(name: str, annotation: Any) -> tuple[str, tuple[str, ...]]:
    base, optional = _base(annotation)
    origin = get_origin(base)
    if origin is Literal:
        return "enum", tuple(str(a) for a in get_args(base))
    if isinstance(base, type) and issubclass(base, StrEnum):
        return "enum", tuple(member.value for member in base)
    if base is bool:
        return ("tristate" if optional else "bool"), ()
    if base is int:
        return "int", ()
    if base is float:
        return "float", ()
    if base is str or base is Path:
        return ("env" if "env" in name or "token" in name else "str"), ()
    if origin is list:
        return "list", ()
    if origin is dict:
        args = get_args(base)
        value_type, _ = _base(args[1]) if len(args) == 2 else (None, False)
        if value_type in (int, float):
            return "weights", ()
        return "skip", ()
    if isinstance(base, type) and issubclass(base, BaseModel):
        return "skip", ()  # its own table, edited as a section
    return "skip", ()


def _fmt(kind: str, value: Any) -> str:
    if value is None:
        return ""
    if kind == "list" and isinstance(value, list):
        return "\n".join(str(item) for item in value)
    if kind == "bool":
        return "true" if value else "false"
    if kind in ("int", "float", "str", "env", "enum", "tristate"):
        return str(value)
    return ""


def _help(info: Any) -> str:
    text = getattr(info, "description", None) or ""
    return " ".join(str(text).split())


def sections(
    config: JevConfig, repo_raw: dict[str, Any], user_raw: dict[str, Any]
) -> list[Section]:
    """Every editable field of every top-level section, with its source and current value."""
    out: list[Section] = []
    for path, title in TOP_SECTIONS:
        model = _attr_at(config, path)
        fields: list[Field] = []
        notes: list[str] = []
        for name, info in type(model).model_fields.items():
            kind, choices = _kind_of(name, info.annotation)
            dotted = f"{path}.{name}"
            if kind == "skip":
                if dotted not in _SECTION_PATHS:
                    notes.append(f"`{dotted}` is a table; edit it in jev.toml")
                continue
            value = getattr(model, name)
            parts: tuple[tuple[str, str], ...] = ()
            if kind == "weights" and isinstance(value, dict):
                choices = tuple(value)
                parts = tuple((key, _fmt("float", number)) for key, number in value.items())
            in_repo, _ = _at(repo_raw, dotted)
            in_user, _ = _at(user_raw, dotted)
            fields.append(
                Field(
                    path=dotted,
                    key=name,
                    kind=kind,
                    choices=choices,
                    text=_fmt(kind, value),
                    source="repo" if in_repo else ("user" if in_user else None),
                    help=_help(info),
                    parts=parts,
                )
            )
        out.append(Section(path, title, fields, "\n".join(notes)))
    return out


# ---- entities ------------------------------------------------------------------------------


def entity_sections(
    config: JevConfig, repo_raw: dict[str, Any], user_raw: dict[str, Any]
) -> list[EntitySection]:
    """The tables you add entries to, with their current entries and an add-form shape."""
    out: list[EntitySection] = []
    for path, title in ENTITY_SECTIONS:
        current = _attr_at(config, path)
        entries: list[Entity] = []
        for name, item in sorted(current.items()):
            in_repo, _ = _at(repo_raw, f"{path}.{name}")
            fields = _entity_fields(item)
            entries.append(Entity(name=name, source="repo" if in_repo else "user", fields=fields))
        model = _dict_value_model(path)
        if model is None:
            add_fields = [EntityField("path", "str", (), "", "a folder with a SKILL.md")]
        else:
            add_fields = []
            for name, info in model.model_fields.items():
                kind, choices = _kind_of(name, info.annotation)
                if kind == "skip":
                    continue
                default = (
                    None if info.is_required() else info.get_default(call_default_factory=True)
                )
                add_fields.append(
                    EntityField(name, kind, choices, _fmt(kind, default), _help(info))
                )
        out.append(EntitySection(path, title, entries, add_fields))
    return out


def _dict_value_model(path: str) -> type[BaseModel] | None:
    """The model behind a dict-typed table (`tools.mcp` -> the MCP server model), or None."""
    node: Any = JevConfig
    parts = path.split(".")
    for index, part in enumerate(parts):
        info = node.model_fields[part]
        base, _ = _base(info.annotation)
        last = index == len(parts) - 1
        if last:
            if get_origin(base) is not dict:
                return None
            args = get_args(base)
            value_type, _ = _base(args[1]) if len(args) == 2 else (None, False)
            if isinstance(value_type, type) and issubclass(value_type, BaseModel):
                return value_type
            return None
        if not (isinstance(base, type) and issubclass(base, BaseModel)):
            return None
        node = base
    return None


def _entity_fields(item: Any) -> list[EntityField]:
    if isinstance(item, str):  # tools.skills: name -> path
        return [EntityField("path", "str", (), item, "a folder with a SKILL.md")]
    fields: list[EntityField] = []
    for name, info in type(item).model_fields.items():
        kind, choices = _kind_of(name, info.annotation)
        if kind == "skip":
            continue
        fields.append(
            EntityField(name, kind, choices, _fmt(kind, getattr(item, name)), _help(info))
        )
    return fields


# ---- form values ---------------------------------------------------------------------------


def parse_value(kind: str, raw: str, form: dict[str, str]) -> Any:
    """A posted value as the TOML type; `UNSET` for an empty one; `ValueError` for a bad one."""
    text = raw.strip()
    if kind == "weights":
        return {name: float(form[f"w_{name}"].strip()) for name in _weight_names(form)}
    if not text:
        return UNSET
    if kind == "bool":
        return text.lower() in ("true", "on", "1", "yes")
    if kind == "tristate":
        if text in ("true", "false"):
            return text == "true"
        raise ValueError("pick true, false or unset")
    if kind == "int":
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{text!r} is not a whole number") from None
    if kind == "float":
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"{text!r} is not a number") from None
    if kind == "list":
        return [line.strip() for line in raw.splitlines() if line.strip()]
    return text


def _weight_names(form: dict[str, str]) -> list[str]:
    return sorted(key[2:] for key in form if key.startswith("w_"))
